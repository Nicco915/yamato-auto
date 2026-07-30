"""调度 Agent RAG 检索后端（V2：Pinecone 向量检索）。

设计见 agent设计/rag设计.md。要点：
- 单 index + namespace 分桶（guide / issue），ID 稳定主键保证幂等 upsert；
- embedding 走硅基流动 Qwen3-Embedding（独立 EMBEDDING_API_KEY，与聊天模型
  的百炼 token-plan 代理不共用）；
- 查询侧 LRU 缓存问题向量，命中跳过 API；
- 全部公开函数失败只返回 None/[]/False 并记警告——检索是辅助设施，
  不能搞挂主流程，调用方负责回落关键词匹配。

未来换向量库（Zilliz / pgvector / sqlite-vec）只需改三个 Pinecone 耦合点：
_get_index / upsert_entries / query_namespace，对外签名不变。

RAG_MOCK=1 时走内存假索引 + 字符 bigram 哈希向量（共享子串多的文本
cosine 高，可测语义命中），无需任何 API key，供 validation 使用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from functools import lru_cache
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_MOCK = os.environ.get("RAG_MOCK", "0") == "1"

# embedding 批量上限（硅基流动单次 input 列表不宜过大）
_EMBED_BATCH = 32
# Pinecone upsert 批量上限
_UPSERT_BATCH = 100
# embedding 请求超时（秒）
_REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Mock 后端（RAG_MOCK=1）：内存索引 + bigram 哈希向量
# ---------------------------------------------------------------------------

class _MockIndex:
    """进程内假索引，模拟 Pinecone 的 namespace/upsert/query 语义。"""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict[str, dict]] = {}

    def upsert(self, namespace: str, vectors: list[dict]) -> None:
        ns = self.namespaces.setdefault(namespace, {})
        for v in vectors:
            ns[v["id"]] = {"values": v["values"], "metadata": v["metadata"]}

    def query(self, namespace: str, vector: list[float], top_k: int) -> list[dict]:
        ns = self.namespaces.get(namespace, {})
        scored = []
        for id_, rec in ns.items():
            score = _cosine(vector, rec["values"])
            scored.append({"id": id_, "score": score, "metadata": rec["metadata"]})
        scored.sort(key=lambda m: m["score"], reverse=True)
        return scored[:top_k]

    def delete_namespace(self, namespace: str) -> None:
        self.namespaces.pop(namespace, None)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


_MOCK_INDEX = _MockIndex()


def _mock_embed(text: str, dims: int) -> list[float]:
    """字符 bigram 哈希向量：共享子串多的文本 cosine 相似度高。

    不是真语义向量，但足以让"重跑批次怎么操作"命中"如何重跑批次"，
    供无 key 环境下验证检索链路。
    """
    vec = [0.0] * dims
    t = re.sub(r"\s+", "", text)
    grams = [t[i:i + 2] for i in range(len(t) - 1)] or [t]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# Embedding（硅基流动，OpenAI 兼容接口）
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_embed_client():
    """构造 embedding 专用 OpenAI 客户端。缺 key 返回 None（调用方回落）。"""
    try:
        from openai import OpenAI
        s = get_settings()
        if not s.embedding_api_key:
            return None
        return OpenAI(
            api_key=s.embedding_api_key,
            base_url=s.embedding_base_url,
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("构造 embedding 客户端失败: %s", exc)
        return None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量向量化。失败返回 None（不抛异常）。"""
    if not texts:
        return []
    dims = get_settings().embedding_dimensions
    if _MOCK:
        return [_mock_embed(t, dims) for t in texts]

    client = _get_embed_client()
    if client is None:
        logger.warning("EMBEDDING_API_KEY 未配置，跳过向量化")
        return None
    model = get_settings().embedding_model
    try:
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            chunk = texts[i:i + _EMBED_BATCH]
            resp = client.embeddings.create(model=model, input=chunk)
            # 按 index 对齐，兼容返回顺序不保证的供应商
            ordered = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in ordered)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding 调用失败: %s", exc)
        return None


@lru_cache(maxsize=256)
def _cached_query_embed(norm_text: str) -> tuple[float, ...] | None:
    vecs = embed_texts([norm_text])
    if not vecs:
        return None
    return tuple(vecs[0])


def embed_query(text: str) -> list[float] | None:
    """查询侧单向量，带 LRU 缓存（问题归一化后做 key，写入时机铁律③）。"""
    norm = re.sub(r"\s+", " ", text.strip())
    if not norm:
        return None
    cached = _cached_query_embed(norm)
    return list(cached) if cached else None


# ---------------------------------------------------------------------------
# Pinecone（三个耦合点：_get_index / upsert_entries / query_namespace）
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_index():
    """取 Pinecone 索引句柄（不存在则创建）。失败返回 None。"""
    if _MOCK:
        return _MOCK_INDEX
    try:
        from pinecone import Pinecone, ServerlessSpec
        s = get_settings()
        if not s.pinecone_api_key:
            return None
        pc = Pinecone(api_key=s.pinecone_api_key)
        if not pc.has_index(s.pinecone_index):
            pc.create_index(
                name=s.pinecone_index,
                dimension=s.embedding_dimensions,
                metric="cosine",
                spec=ServerlessSpec(cloud=s.pinecone_cloud, region=s.pinecone_region),
            )
        return pc.Index(s.pinecone_index)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pinecone 索引初始化失败: %s", exc)
        return None


def backend_enabled() -> bool:
    """当前是否应走 RAG 后端（开关 + 两端 key 齐备）。"""
    s = get_settings()
    if _MOCK:
        return True
    return (
        s.kb_backend == "pinecone"
        and bool(s.pinecone_api_key)
        and bool(s.embedding_api_key)
    )


def upsert_entries(namespace: str, entries: list[dict]) -> bool:
    """幂等灌库：embed 后按稳定 ID upsert。entries = [{id, text, metadata}]。

    供 scripts/sync_kb.py 冷启动使用；运行时不调用（铁律①知识库不自动生长）。
    """
    if not entries:
        return True
    try:
        index = _get_index()
        if index is None:
            logger.warning("向量索引不可用，upsert 跳过（namespace=%s）", namespace)
            return False
        vectors: list[dict] = []
        for i in range(0, len(entries), _EMBED_BATCH):
            chunk = entries[i:i + _EMBED_BATCH]
            vecs = embed_texts([e["text"] for e in chunk])
            if vecs is None:
                return False
            for e, v in zip(chunk, vecs):
                vectors.append({"id": e["id"], "values": v, "metadata": e["metadata"]})
        for i in range(0, len(vectors), _UPSERT_BATCH):
            index.upsert(
                vectors=vectors[i:i + _UPSERT_BATCH], namespace=namespace,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert namespace=%s 失败: %s", namespace, exc)
        return False


def query_namespace(
    namespace: str, text: str, *, top_k: int = 3,
    min_score: float | None = None,
) -> list[dict]:
    """向量检索：返回 [{id, score, metadata}]（score 降序，已按阈值过滤）。

    任何失败返回空列表——调用方按"未命中"处理并回落关键词匹配。
    """
    try:
        threshold = min_score if min_score is not None else get_settings().rag_min_score
        vec = embed_query(text)
        if vec is None:
            return []
        index = _get_index()
        if index is None:
            return []
        if _MOCK:
            matches = index.query(namespace, vec, top_k)
        else:
            resp = index.query(
                vector=vec, top_k=top_k, namespace=namespace,
                include_metadata=True,
            )
            matches = [
                {"id": m.id, "score": m.score, "metadata": dict(m.metadata or {})}
                for m in resp.matches
            ]
        return [m for m in matches if m["score"] >= threshold]
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量检索失败 namespace=%s: %s", namespace, exc)
        return []


# ---------------------------------------------------------------------------
# 待策展队列（铁律①增长飞轮：未命中问题人工确认后才进知识库）
# ---------------------------------------------------------------------------

def log_curation(question: str, source: str) -> None:
    """把未命中问题追加到 app/data/rag_curation_queue.jsonl，失败只记警告。"""
    try:
        path = get_settings().resolve("app/data/rag_curation_queue.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(), "source": source, "question": question,
            }, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录待策展问题失败: %s", exc)


def read_curation_queue(max_items: int = 50) -> list[dict]:
    """读待策展队列，返回最近 max_items 条（按时间倒序）。"""
    try:
        path = get_settings().resolve("app/data/rag_curation_queue.jsonl")
        if not path.exists():
            return []
        items: list[dict] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        items.sort(key=lambda x: float(x.get("ts", 0)), reverse=True)
        return items[:max_items]
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取待策展队列失败: %s", exc)
        return []


def remove_from_queue(questions: set[str]) -> None:
    """从队列中移除指定问题（重写 jsonl）。"""
    try:
        path = get_settings().resolve("app/data/rag_curation_queue.jsonl")
        if not path.exists():
            return
        kept: list[str] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if item.get("question", "") not in questions:
                    kept.append(line)
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理待策展队列失败: %s", exc)


def load_extension() -> dict:
    """读取 kb_extension.json（Agent 策展的增量条目）。不存在返回空结构。"""
    try:
        path = get_settings().resolve("app/data/kb_extension.json")
        if not path.exists():
            return {"guide": {}, "issue": {}}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"guide": {}, "issue": {}}
        return {
            "guide": data.get("guide") or {},
            "issue": data.get("issue") or {},
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 kb_extension.json 失败: %s", exc)
        return {"guide": {}, "issue": {}}


def save_extension(ext: dict) -> bool:
    """写入 kb_extension.json（merge 模式：保留已有条目，只追加新 key）。"""
    try:
        path = get_settings().resolve("app/data/kb_extension.json")
        existing = load_extension()
        for cat in ("guide", "issue"):
            for key, entry in (ext.get(cat) or {}).items():
                if key not in existing[cat]:
                    existing[cat][key] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入 kb_extension.json 失败: %s", exc)
        return False
