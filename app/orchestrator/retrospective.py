"""批次复盘摘要生成服务。

从 batch_store、pipeline_state、批次状态文件等来源聚合一个 batch 的运行痕迹，
输出结构化原子 lesson 与自然语言摘要，供经验库沉淀与后续检索。

设计原则：
- 只读 & 防御：所有 IO/LLM 调用包在 try/except 中，失败只记录 warning，不抛异常。
- 不暴露 API key、内部路径：返回给调用方的摘要中不含具体文件路径或密钥。
- 跨平台：路径处理统一使用 pathlib.Path。
- UTC 时间：datetime.now(timezone.utc).isoformat()。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 可选导入：失败时降级为模板摘要，不阻塞主流程
try:
    from app.db import batch_store
except Exception as _exc:  # noqa: BLE001
    logger.warning("retrospective 无法导入 batch_store: %s", _exc)
    batch_store = None  # type: ignore[assignment]

try:
    from app.orchestrator import pipeline_state
except Exception as _exc:  # noqa: BLE001
    logger.warning("retrospective 无法导入 pipeline_state: %s", _exc)
    pipeline_state = None  # type: ignore[assignment]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_alias_entry(entry: str) -> tuple[str, str]:
    """把 'factory -> folder' 解析成 (factory, folder)。"""
    if "->" in entry:
        left, right = entry.split("->", 1)
        return left.strip(), right.strip()
    return entry.strip(), ""


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """安全多级取值；d 不是 dict 时返回 default。"""
    if not isinstance(d, dict):
        return default
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur


def _factory_for_folder(factory_setup: dict, folder_name: str) -> str:
    """根据创建的空文件夹名反查工厂名（从 resolved 映射）。"""
    resolved = factory_setup.get("resolved") or {}
    for factory, info in resolved.items():
        if isinstance(info, dict) and info.get("folder") == folder_name:
            return factory
    return folder_name


def _call_llm_summary(prompt: str) -> str:
    """调用 LLM 生成中文摘要；失败抛异常由调用方兜底。"""
    try:
        from app.extraction import llm_client
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法导入 llm_client: {exc}") from exc

    messages = [
        {
            "role": "system",
            "content": (
                "你是雅玛多单证自动化系统的复盘助手。请根据下方结构化运行痕迹，"
                "生成一段简洁、自然、面向操作人员的中文复盘摘要（200 字以内）。"
                "不要输出文件路径、API key、环境变量等技术细节。"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    # chat_completion_with_tools 支持空 tools，不强制 JSON，适合纯文本摘要
    resp = llm_client.chat_completion_with_tools(
        messages=messages,
        tools=[],
        source_file="retrospective",
        temperature=0.3,
        max_tokens=2048,
    )
    content = resp.get("content") if isinstance(resp, dict) else None
    if not content or not str(content).strip():
        raise RuntimeError("LLM 返回空摘要")
    return str(content).strip()


def _build_llm_prompt(batch_id: str, status: str, lessons: list[dict]) -> str:
    """为 LLM 组装输入 prompt。"""
    lines = [
        f"批次号：{batch_id}",
        f"当前状态：{status}",
        f"共提取 {len(lessons)} 条原子经验：",
    ]
    for i, lesson in enumerate(lessons, 1):
        category = lesson.get("category", "unknown")
        factory = lesson.get("factory_name") or "全局"
        decision = lesson.get("decision", "")
        outcome = lesson.get("outcome", {})
        tags = ", ".join(lesson.get("tags", [])) or "无"
        lines.append(
            f"{i}. [{category}] 工厂：{factory} | 决策：{decision} | "
            f"结果：{outcome} | 标签：{tags}"
        )
    lines.append("\n请生成中文复盘摘要，突出关键决策、异常与可沉淀经验。")
    return "\n".join(lines)


def atomic_lessons(batch_state: dict) -> list[dict]:
    """从聚合后的 batch 状态中提取原子 lesson 列表。

    batch_state 建议字段：
    - batch: dict (来自 batch_store.get_batch)
    - pipeline: dict (来自 pipeline_state.get_pipeline_state)
    - factory_setup: dict，含 created / alias_written / unmatched / resolved
    - downstream_diff: dict，含 recommendation / changes
    - review_audits: list[dict]
    - declarations: list[dict]

    返回 lesson 列表，每个元素含 lesson_id / batch_id / factory_name /
    category / context / decision / outcome / tags / created_at / source_event。
    """
    lessons: list[dict] = []
    if not isinstance(batch_state, dict):
        return lessons

    batch_id = _safe_get(batch_state, "batch", "thread_id", default="")
    if not batch_id:
        batch_id = _safe_get(batch_state, "batch_id", default="")
    factory_setup = _safe_get(batch_state, "factory_setup", default={}) or {}
    downstream_diff = _safe_get(batch_state, "downstream_diff", default={}) or {}
    pipeline = _safe_get(batch_state, "pipeline", default={}) or {}
    review_audits = batch_state.get("review_audits") or []
    declarations = batch_state.get("declarations") or []
    created_at = _now_iso()

    # 1. 别名学习
    alias_written = factory_setup.get("alias_written") or []
    for idx, entry in enumerate(alias_written):
        factory_name, alias = _parse_alias_entry(entry)
        lessons.append({
            "lesson_id": f"{batch_id}-alias-{idx}",
            "batch_id": batch_id,
            "factory_name": factory_name or "unknown",
            "category": "alias_learning",
            "context": {"alias": alias, "source": "factory_setup.alias_written"},
            "decision": f"自动写入别名：{factory_name} -> {alias}",
            "outcome": {"recorded": True},
            "tags": ["auto_alias"],
            "created_at": created_at,
            "source_event": "factory_setup.alias_written",
        })

    # 2. 空文件夹
    created_folders = factory_setup.get("created") or []
    if created_folders:
        factories = [_factory_for_folder(factory_setup, f) for f in created_folders]
        lessons.append({
            "lesson_id": f"{batch_id}-empty-folder",
            "batch_id": batch_id,
            "factory_name": factories[0] if len(factories) == 1 else "multiple",
            "category": "factory_match",
            "context": {
                "created_folders": created_folders,
                "matched_factories": factories,
            },
            "decision": "为无匹配工厂创建空文件夹",
            "outcome": {"created_count": len(created_folders)},
            "tags": ["empty_folder"],
            "created_at": created_at,
            "source_event": "factory_setup.created",
        })

    # 3. 下游差异
    recommendation = downstream_diff.get("recommendation")
    if recommendation in ("full", "diff"):
        tag = "full_refresh" if recommendation == "full" else "diff_refresh"
        lessons.append({
            "lesson_id": f"{batch_id}-downstream-{recommendation}",
            "batch_id": batch_id,
            "factory_name": "multiple" if downstream_diff.get("changed_factories") else "global",
            "category": "downstream_diff",
            "context": {
                "recommendation": recommendation,
                "reason": downstream_diff.get("reason"),
                "added_factories": downstream_diff.get("added_factories", []),
                "removed_factories": downstream_diff.get("removed_factories", []),
                "changed_factories": downstream_diff.get("changed_factories", []),
            },
            "decision": f"下游装箱单推荐策略：{recommendation}",
            "outcome": {"changes": downstream_diff.get("changes", {})},
            "tags": [tag],
            "created_at": created_at,
            "source_event": "downstream_diff.compare",
        })

    # 4. 审核修改
    for idx, audit in enumerate(review_audits):
        if not isinstance(audit, dict):
            continue
        edited_count = int(audit.get("edited_count") or 0)
        new_skus = audit.get("new_skus") or []
        if edited_count <= 0 and not new_skus:
            continue
        tags: list[str] = []
        if edited_count > 0:
            tags.append("manual_edit")
        if new_skus:
            tags.append("new_sku")
        lessons.append({
            "lesson_id": f"{batch_id}-review-{idx}",
            "batch_id": batch_id,
            "factory_name": audit.get("factory_name") or "unknown",
            "category": "review_audit",
            "context": {k: v for k, v in audit.items() if k not in ("batch_id",)},
            "decision": audit.get("result_status") or "人工审核提交",
            "outcome": {
                "edited_count": edited_count,
                "new_sku_count": len(new_skus),
            },
            "tags": tags,
            "created_at": created_at,
            "source_event": "review_audit.save",
        })

    # 5. 分票
    split = pipeline.get("split") or {}
    proposal = split.get("proposal")
    has_proposal = bool(proposal) and (not isinstance(proposal, list) or len(proposal) > 0)
    if has_proposal:
        force_confirmed = bool(split.get("force_confirmed"))
        status = split.get("status")
        tags = ["split"]
        if force_confirmed:
            tags.append("force_confirmed")
        lessons.append({
            "lesson_id": f"{batch_id}-split",
            "batch_id": batch_id,
            "factory_name": "multiple",
            "category": "split_rule",
            "context": {
                "proposal": bool(proposal),
                "force_confirmed": force_confirmed,
                "status": status,
            },
            "decision": "强制通过分票方案" if force_confirmed else "常规确认分票方案",
            "outcome": {"status": status},
            "tags": tags,
            "created_at": created_at,
            "source_event": "split.proposal",
        })

    # 6. 报关单生成：优先从 pipeline.split.declare_result 取，再取 declarations 聚合
    declare_result = _safe_get(pipeline, "split", "declare_result", default={}) or {}
    decl_count = declare_result.get("count")
    decl_warnings = declare_result.get("warnings") or []
    if decl_count is None and declarations:
        decl_count = len(declarations)
        decl_warnings = [
            w for d in declarations for w in (d.get("warnings") or [])
        ]
    if decl_count is not None:
        lessons.append({
            "lesson_id": f"{batch_id}-declaration",
            "batch_id": batch_id,
            "factory_name": "multiple",
            "category": "declaration_gen",
            "context": {
                "count": decl_count,
                "warnings": decl_warnings,
            },
            "decision": "生成报关单",
            "outcome": {
                "generated_count": decl_count,
                "warning_count": len(decl_warnings),
            },
            "tags": ["declaration"],
            "created_at": created_at,
            "source_event": "declaration.generate",
        })

    return lessons


def _default_nl_summary(batch_id: str, status: str, lessons: list[dict]) -> str:
    """LLM 不可用或 mock 时的规则模板兜底摘要。"""
    categories = sorted({lesson.get("category", "unknown") for lesson in lessons})
    categories_text = ", ".join(categories) if categories else "无"
    return f"批次 {batch_id} 当前状态为 {status}。共生成 {len(lessons)} 条经验，涉及 {categories_text}。"


def summarize_batch(batch_id: str) -> dict:
    """汇总一个 batch，输出结构化复盘。

    返回：
    {
      "batch_id": str,
      "status": str,
      "final_output_path": str | None,
      "atomic_lessons": list[dict],
      "nl_summary": str,
      "generated_at": str (ISO UTC),
    }
    """
    batch: dict[str, Any] | None = None
    pipeline: dict[str, Any] | None = None
    batch_state_file: dict[str, Any] | None = None

    # 1. 读取 batch 表
    if batch_store is not None:
        try:
            batch = batch_store.get_batch(batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 batch %s 失败: %s", batch_id, exc)

    # 2. 读取 pipeline 状态
    if pipeline_state is not None:
        try:
            pipeline = pipeline_state.get_pipeline_state(batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 pipeline_state %s 失败: %s", batch_id, exc)

    # 3. 可选读取批次状态 JSON 文件
    try:
        state_path = get_settings().batch_output_dir(batch_id) / "batch_state.json"
        if state_path.is_file():
            with state_path.open("r", encoding="utf-8") as f:
                batch_state_file = json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 batch_state.json %s 失败: %s", batch_id, exc)

    status = (
        _safe_get(batch, "status", default="")
        or _safe_get(pipeline, "current_phase", default="")
        or "unknown"
    )
    final_output_path = _safe_get(batch, "final_output_path", default=None)

    # 组装 batch_state：调用方传入字段 + 本函数自动补齐
    batch_state = {
        "batch_id": batch_id,
        "batch": batch or {},
        "pipeline": pipeline or {},
    }
    if isinstance(batch_state_file, dict):
        batch_state.setdefault("factory_setup", batch_state_file.get("factory_setup"))
        batch_state.setdefault("downstream_diff", batch_state_file.get("downstream_diff"))
        batch_state.setdefault("review_audits", batch_state_file.get("review_audits"))
        batch_state.setdefault("declarations", batch_state_file.get("declarations"))

    lessons = atomic_lessons(batch_state)

    # 4. 生成自然语言摘要
    nl_summary = _default_nl_summary(batch_id, status, lessons)
    if os.environ.get("RETROSPECTIVE_MOCK", "").strip() == "1":
        logger.info("RETROSPECTIVE_MOCK=1，使用规则模板摘要")
    else:
        try:
            prompt = _build_llm_prompt(batch_id, status, lessons)
            nl_summary = _call_llm_summary(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 摘要生成失败，使用模板兜底: %s", exc)

    return {
        "batch_id": batch_id,
        "status": status,
        "final_output_path": final_output_path,
        "atomic_lessons": lessons,
        "nl_summary": nl_summary,
        "generated_at": _now_iso(),
    }


def format_for_kb(lesson: dict) -> dict:
    """把原子 lesson 转换成经验库条目格式。

    返回适合传给 app.dispatcher.experience.add_experience 的字典：
    {
      "title": str,
      "content": str,
      "category": str,
      "factory": str | None,
      "tags": list[str],
      "source_batch_id": str,
      "confidence": float,
    }
    """
    if not isinstance(lesson, dict):
        raise TypeError("lesson 必须是 dict")

    category = lesson.get("category", "unknown")
    batch_id = lesson.get("batch_id", "")
    factory = lesson.get("factory_name") or None
    if factory in ("multiple", "global", "unknown"):
        factory = None
    tags = list(lesson.get("tags", []))
    context = lesson.get("context", {}) or {}
    decision = lesson.get("decision", "")
    outcome = lesson.get("outcome", {}) or {}

    title_map = {
        "alias_learning": f"别名学习：{batch_id}",
        "factory_match": f"工厂匹配：{batch_id}",
        "downstream_diff": f"下游差异：{batch_id}",
        "review_audit": f"人工审核：{batch_id}",
        "split_rule": f"分票规则：{batch_id}",
        "declaration_gen": f"报关单生成：{batch_id}",
    }
    title = title_map.get(category, f"复盘经验：{batch_id}")

    content_lines = [
        f"批次：{batch_id}",
        f"类别：{category}",
        f"决策：{decision}",
        f"结果：{outcome}",
    ]
    if context:
        content_lines.append(f"上下文：{context}")
    if tags:
        content_lines.append(f"标签：{', '.join(tags)}")
    content = "\n".join(content_lines)

    # 置信度启发式：人工审核最高，强制通过偏低
    confidence_map = {
        "review_audit": 0.95,
        "downstream_diff": 0.9,
        "declaration_gen": 0.9 if not outcome.get("warning_count") else 0.7,
        "alias_learning": 0.8,
        "factory_match": 0.7,
        "split_rule": 0.6 if "force_confirmed" in tags else 0.85,
    }
    confidence = float(confidence_map.get(category, 0.7))

    return {
        "title": title,
        "content": content,
        "category": category,
        "factory": factory,
        "tags": tags,
        "source_batch_id": batch_id,
        "confidence": confidence,
    }
