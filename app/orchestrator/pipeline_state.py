"""流水线状态推导服务。

从 LangGraph checkpoint、Batch 表、Declaration 表推导当前批次处于哪个阶段，
供前端 Agent 对话页顶部状态图使用。
"""
from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.db import batch_store
from app.graph import NODE1, NODE2, NODE3, NODE4, NODE5, NODE6, NODE7, get_graph
from app.split.graph import (
    NODE1_SPLIT,
    NODE2_SPLIT,
    NODE3_SPLIT,
    NODE4_SPLIT,
    NODE5_SPLIT,
)

logger = logging.getLogger(__name__)

# 提取图节点 → 阶段名
_EXTRACT_NODE_PHASES = {
    NODE1: "parse_downstream",
    NODE2: "folder_router",
    NODE3: "extraction",
    NODE4: "compute_align",
    NODE5: "human_review",
    NODE6: "writer",
    NODE7: "export",
}

# 分票图节点 → 阶段名
_SPLIT_NODE_PHASES = {
    NODE1_SPLIT: "split_loading",
    NODE2_SPLIT: "split_proposing",
    NODE3_SPLIT: "split_review",
    NODE4_SPLIT: "split_persisting",
    NODE5_SPLIT: "split_generating",
}

# 阶段展示配置：label、是否可能出现人工介入
_STAGES = [
    {"name": "parse_downstream", "label": "解析装箱单", "needs_action": False},
    {"name": "folder_router", "label": "匹配工厂", "needs_action": False},
    {"name": "extraction", "label": "单据提取", "needs_action": False},
    {"name": "compute_align", "label": "计算对齐", "needs_action": False},
    {"name": "human_review", "label": "人工审核", "needs_action": True, "link": "/review"},
    {"name": "writer", "label": "写回Excel", "needs_action": False},
    {"name": "export", "label": "导出总表", "needs_action": False},
    {"name": "split_review", "label": "分票审核", "needs_action": True, "link": "/split"},
    {"name": "split_generating", "label": "报关单生成", "needs_action": False},
    {"name": "completed", "label": "完成", "needs_action": False},
]


def _extract_phase(thread_id: str) -> dict[str, Any] | None:
    """读取提取图 checkpoint，返回阶段信息；无 checkpoint 返回 None。"""
    try:
        graph = get_graph()
        snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取提取图 checkpoint 失败 %s: %s", thread_id, exc)
        return None

    if not snap.values:
        return None

    has_interrupt = any(t.interrupts for t in snap.tasks)
    next_nodes = list(snap.next or [])
    current_node = next_nodes[0] if next_nodes else None

    if has_interrupt and current_node in _EXTRACT_NODE_PHASES:
        phase = _EXTRACT_NODE_PHASES[current_node]
    elif current_node in _EXTRACT_NODE_PHASES:
        phase = _EXTRACT_NODE_PHASES[current_node]
    elif not next_nodes:
        phase = "export_done"
    else:
        phase = "unknown"

    values = snap.values
    current_factory = (values.get("current_factory_data") or {}).get("factory_name")
    pending = list(values.get("pending_factories") or [])
    done = list((values.get("factory_outputs") or {}).keys())

    return {
        "phase": phase,
        "current_node": current_node,
        "has_interrupt": has_interrupt,
        "current_factory": current_factory,
        "pending_factories": pending,
        "done_factories": done,
        "final_output_path": values.get("final_output_path"),
    }


def _split_phase(batch_thread_id: str) -> dict[str, Any] | None:
    """读取分票图 checkpoint（split-{batch_thread_id}）。"""
    from app.split.graph import get_split_graph

    split_thread_id = f"split-{batch_thread_id}"
    try:
        graph = get_split_graph()
        snap = graph.get_state({"configurable": {"thread_id": split_thread_id}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取分票图 checkpoint 失败 %s: %s", split_thread_id, exc)
        return None

    if not snap.values:
        return None

    has_interrupt = any(t.interrupts for t in snap.tasks)
    next_nodes = list(snap.next or [])
    current_node = next_nodes[0] if next_nodes else None

    if has_interrupt and current_node in _SPLIT_NODE_PHASES:
        phase = _SPLIT_NODE_PHASES[current_node]
    elif current_node in _SPLIT_NODE_PHASES:
        phase = _SPLIT_NODE_PHASES[current_node]
    elif not next_nodes:
        phase = "split_done"
    else:
        phase = "unknown"

    values = snap.values
    # 报关单输出目录：{output}/{batch}/declarations/{split_thread_id}/
    # 供前端判定「打开报关单目录」按钮显隐（确认方案但未生成时目录不存在/为空）
    decl_dir = get_settings().batch_declarations_dir(batch_thread_id) / split_thread_id
    try:
        declarations_ready = decl_dir.is_dir() and any(decl_dir.iterdir())
    except OSError:
        declarations_ready = False

    return {
        "phase": phase,
        "split_thread_id": split_thread_id,
        "has_interrupt": has_interrupt,
        "proposal": bool(values.get("proposal")),
        "status": values.get("status"),
        "declaration_dir": str(decl_dir),
        "declarations_ready": declarations_ready,
    }


def get_pipeline_state(thread_id: str) -> dict[str, Any]:
    """返回给定 batch thread_id 的流水线状态。"""
    batch = batch_store.get_batch(thread_id)
    extract = _extract_phase(thread_id)
    split = _split_phase(thread_id)

    if extract is None and batch is None:
        return {"exists": False, "thread_id": thread_id}

    # 判定顶层阶段
    if split and split.get("phase") in ("split_review", "split_persisting", "split_generating", "split_done"):
        current_phase = split["phase"]
    elif extract:
        current_phase = extract["phase"]
    else:
        current_phase = batch.get("status") if batch else "unknown"

    # 如果提取已完成且存在分票线程但未到分票阶段，优先展示 split_review（因为分票图已启动）
    if current_phase == "export_done" and split:
        current_phase = split["phase"] if split["phase"] != "split_done" else "split_generating"

    stages = []
    for s in _STAGES:
        stage = dict(s)
        stage["status"] = "done" if _is_stage_done(stage["name"], current_phase) else (
            "active" if stage["name"] == current_phase else "pending"
        )
        if stage["status"] == "active" and stage.get("needs_action"):
            stage["link_href"] = f"{stage['link']}?thread_id={thread_id}"
        stages.append(stage)

    return {
        "exists": True,
        "thread_id": thread_id,
        "current_phase": current_phase,
        "stages": stages,
        "extract": extract,
        "split": split,
        "batch": batch,
    }


def _is_stage_done(stage_name: str, current_phase: str) -> bool:
    """粗略判定某阶段是否已完成（按阶段顺序）。"""
    order = [s["name"] for s in _STAGES]
    if stage_name not in order or current_phase not in order:
        return False
    return order.index(stage_name) < order.index(current_phase)
