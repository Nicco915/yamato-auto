"""RAG experience 经验库（Release 4）。

经验是 retrospective 产出的可检索知识片段，按 namespace="experience" 存入向量库。
- 写入前做语义去重（content 自检索，阈值 0.85），命中则合并 count；
- 未命中则按 category + factory + content 生成稳定 ID 后 upsert；
- 全部写操作在本地 JSON 镜像留一份可随机访问的元数据，用于更新 count 等
  需要“读-改-写”的场景（RAG 后端未暴露按 ID fetch）。

复用 app.dispatcher.rag 的公开函数：embed_texts、query_namespace、upsert_entries。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.dispatcher.rag import (
    backend_enabled,
    embed_texts,
    query_namespace,
    upsert_entries,
)

logger = logging.getLogger(__name__)

NAMESPACE = "experience"
_STORE_FILE = "app/data/experience_entries.json"

# 默认字段
_DEFAULT_COUNT = 1
_DEFAULT_CONFIDENCE = 0.8
_DEFAULT_STATUS = "active"
_DEFAULT_CREATED_BY = "retrospective"
_MERGE_MIN_SCORE = 0.85


def _now_iso() -> str:
    """返回 UTC ISO 格式时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str | None) -> str:
    """规范化文本：去前后空白并小写。"""
    return (text or "").strip().lower()


def _stable_id(category: str, factory: str | None, content: str) -> str:
    """基于 category + factory + content 生成稳定 ID（SHA1 前 16 位）。"""
    key = f"{_normalize(category)}|{_normalize(factory)}|{_normalize(content)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _store_path() -> Path:
    """经验库本地镜像路径。"""
    return get_settings().resolve(_STORE_FILE)


def _load_store() -> dict[str, dict]:
    """加载本地镜像；不存在返回空字典。"""
    path = _store_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 experience 镜像失败: %s", exc)
    return {}


def _save_store(store: dict[str, dict]) -> bool:
    """保存本地镜像。"""
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 experience 镜像失败: %s", exc)
        return False


def _matches_filters(metadata: dict, filters: dict | None) -> bool:
    """判断 metadata 是否满足后过滤条件。"""
    if not filters:
        return True

    if filters.get("category") and metadata.get("category") != filters["category"]:
        return False

    factory_filter = filters.get("factory")
    if factory_filter is not None and metadata.get("factory") != factory_filter:
        return False

    if filters.get("status") and metadata.get("status") != filters["status"]:
        return False

    want_tags = filters.get("tags")
    if want_tags:
        if isinstance(want_tags, str):
            want_tags = [want_tags]
        have_tags = metadata.get("tags") or []
        if not any(tag in have_tags for tag in want_tags):
            return False

    return True


def _build_entry(entry: dict) -> dict:
    """把输入 entry 规范化为完整元数据结构。"""
    content = (entry.get("content") or "").strip()
    if not content:
        raise ValueError("content 不能为空")

    category = (entry.get("category") or "issue").strip()
    factory = entry.get("factory")
    if factory is not None:
        factory = factory.strip() or None
    tags = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]
    title = (entry.get("title") or content[:60]).strip()
    source_batch_id = entry.get("source_batch_id")
    if source_batch_id is not None:
        source_batch_id = str(source_batch_id).strip() or None

    count = int(entry.get("count", _DEFAULT_COUNT))
    confidence = float(entry.get("confidence", _DEFAULT_CONFIDENCE))
    status = (entry.get("status") or _DEFAULT_STATUS).strip()
    created_by = (entry.get("created_by") or _DEFAULT_CREATED_BY).strip()

    now = _now_iso()
    first_seen_at = entry.get("first_seen_at") or now
    last_seen_at = entry.get("last_seen_at") or now

    entry_id = _stable_id(category, factory, content)

    return {
        "id": entry_id,
        "category": category,
        "factory": factory,
        "tags": tags,
        "content": content,
        "title": title,
        "source_batch_id": source_batch_id,
        "count": count,
        "confidence": confidence,
        "status": status,
        "created_by": created_by,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    }


def search_experience(
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.75,
    filters: dict | None = None,
) -> list[dict]:
    """检索 experience namespace。

    返回 [{"id": str, "score": float, "metadata": dict}, ...]
    filters 后过滤：支持 category、factory、status、tags（含任意一个即可）。
    """
    raw = query_namespace(NAMESPACE, query, top_k=top_k, min_score=min_score)
    results = []
    for item in raw:
        metadata = item.get("metadata") or {}
        if not _matches_filters(metadata, filters):
            continue
        results.append({
            "id": item["id"],
            "score": item["score"],
            "metadata": metadata,
        })
    return results


def update_count(
    entry_id: str,
    *,
    delta: int = 1,
    source_batch_id: str | None = None,
) -> bool:
    """更新已有经验的 count、last_seen_at、source_batch_id。"""
    if not entry_id or delta < 0:
        return False

    store = _load_store()
    existing = store.get(entry_id)
    if existing is None:
        logger.warning("update_count 未找到 entry_id=%s 的本地镜像", entry_id)
        return False

    existing["count"] = int(existing.get("count", 0)) + delta
    existing["last_seen_at"] = _now_iso()
    if source_batch_id is not None:
        existing["source_batch_id"] = source_batch_id

    if not _save_store(store):
        return False

    ok = upsert_entries(NAMESPACE, [{
        "id": existing["id"],
        "text": existing["content"],
        "metadata": existing,
    }])
    if not ok:
        logger.warning("update_count upsert 失败: entry_id=%s", entry_id)
    return ok


def add_experience(entry: dict, *, merge: bool = True) -> dict:
    """写入一条经验。

    entry 字段：
    - category: str，如 "guide" | "issue" | "fix" | "workaround" | "best_practice"
    - factory: str | None
    - tags: list[str]
    - content: str，用于检索的主文本
    - title: str，人读标题
    - source_batch_id: str | None
    - count: int，默认 1
    - confidence: float，默认 0.8
    - status: str，默认 "active"

    返回 {"id": str, "action": "created" | "merged", "count": int}
    """
    try:
        full = _build_entry(entry)
    except ValueError as exc:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("构建 experience entry 失败: %s", exc)
        return {"id": None, "action": "error", "count": 0, "error": str(exc)}

    entry_id = full["id"]

    if merge:
        hits = search_experience(
            full["content"], top_k=1, min_score=_MERGE_MIN_SCORE,
            filters={"category": full["category"], "factory": full["factory"]},
        )
        if hits:
            hit = hits[0]
            hit_id = hit["id"]
            merged = update_count(
                hit_id,
                delta=1,
                source_batch_id=full.get("source_batch_id"),
            )
            if merged:
                return {
                    "id": hit_id,
                    "action": "merged",
                    "count": int(hit["metadata"].get("count", 0)) + 1,
                }

            # 本地镜像缺失或 upsert 失败：用命中元数据兜底合并
            logger.warning("update_count 失败，尝试直接 upsert 合并: %s", hit_id)
            merged_meta = dict(hit["metadata"])
            merged_meta["count"] = int(merged_meta.get("count", 0)) + 1
            merged_meta["last_seen_at"] = _now_iso()
            if full.get("source_batch_id") is not None:
                merged_meta["source_batch_id"] = full["source_batch_id"]
            store = _load_store()
            store[hit_id] = merged_meta
            _save_store(store)
            if upsert_entries(NAMESPACE, [{
                "id": hit_id,
                "text": merged_meta["content"],
                "metadata": merged_meta,
            }]):
                return {"id": hit_id, "action": "merged", "count": merged_meta["count"]}
            return {
                "id": hit_id,
                "action": "error",
                "count": 0,
                "error": "合并后 upsert 失败",
            }

    # 新增
    store = _load_store()
    store[entry_id] = full
    _save_store(store)

    ok = upsert_entries(NAMESPACE, [{
        "id": entry_id,
        "text": full["content"],
        "metadata": full,
    }])
    if not ok:
        logger.warning("add_experience upsert 失败: id=%s", entry_id)
        return {
            "id": entry_id,
            "action": "error",
            "count": 0,
            "error": "upsert 到向量库失败（可能 embedding 服务不可用）",
        }

    return {"id": entry_id, "action": "created", "count": full["count"]}


def ingest_retrospective(retrospective: dict) -> dict:
    """把 retrospective 输出的一组成经验写入经验库。

    retrospective 结构：
    {
      "thread_id": str,
      "factory": str | None,
      "learnings": [
        {"category": ..., "content": ..., "tags": [...], "confidence": ...}
      ]
    }
    返回 {"ingested": [add_experience 结果列表]}
    """
    thread_id = retrospective.get("thread_id")
    factory = retrospective.get("factory")
    learnings = retrospective.get("learnings") or []

    results = []
    for item in learnings:
        entry = {
            "category": item.get("category", "issue"),
            "factory": factory,
            "tags": item.get("tags", []),
            "content": item.get("content", ""),
            "title": item.get("title") or item.get("content", "")[:60],
            "source_batch_id": thread_id,
            "count": int(item.get("count", _DEFAULT_COUNT)),
            "confidence": float(item.get("confidence", _DEFAULT_CONFIDENCE)),
            "status": item.get("status", _DEFAULT_STATUS),
            "created_by": _DEFAULT_CREATED_BY,
        }
        try:
            results.append(add_experience(entry))
        except ValueError as exc:
            logger.warning("ingest_retrospective 跳过无效 learning: %s", exc)
            results.append({
                "id": None,
                "action": "error",
                "count": 0,
                "error": str(exc),
            })

    return {"ingested": results}
