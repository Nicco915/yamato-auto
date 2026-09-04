# -*- coding: utf-8 -*-
"""总控编排：根据 pipeline_state 自动推进端到端批次。

本模块只做决策编排与状态透传，不修改提取/分票图业务逻辑。
所有路径处理使用 pathlib.Path，保持跨平台兼容。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from langgraph.types import Command

from app.db import batch_store
from app.orchestrator.pipeline_state import _SPLIT_NODE_PHASES, get_pipeline_state
from app.split.graph import (
    NODE1_SPLIT,
    NODE2_SPLIT,
    NODE3_SPLIT,
    NODE4_SPLIT,
    NODE5_SPLIT,
    get_split_graph,
)

logger = logging.getLogger(__name__)

# 分票节点 → 人可读进度说明
_SPLIT_NODE_MESSAGES = {
    NODE1_SPLIT: "读取填充后的集装箱内容表",
    NODE2_SPLIT: "生成分票方案",
    NODE3_SPLIT: "等待分票审核",
    NODE4_SPLIT: "保存分票结果",
    NODE5_SPLIT: "生成报关单",
}

# 需要等待的运行中阶段（不含 export/export_done，因为 export_done 可直接启动分票）
_RUNNING_PHASES = {
    "parse_downstream",
    "folder_router",
    "extraction",
    "compute_align",
    "writer",
    "split_loading",
    "split_proposing",
}


def advance_batch(
    thread_id: str,
    action: dict | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """根据 pipeline_state 自动推进批次。

    返回字典至少包含：
    - status: "advanced" | "needs_human_action" | "completed" | "error"
    - phase: 当前阶段名
    - message: 给人读的状态说明
    - link: 当 status == "needs_human_action" 时附上前端跳转链接

    Args:
        thread_id: 批次线程 ID。
        action: 人工确认数据；分票 resume 时作为 Command(resume=...) 的参数。
        on_progress: 进度回调，收到 {"type": "advance_progress", "node": ..., "message": ...}。

    Raises:
        ValueError: 批次不存在。
        RuntimeError: 批次正在运行中或缺少必要前置产物。
    """
    state = get_pipeline_state(thread_id)
    if not state.get("exists"):
        raise ValueError(f"批次不存在: {thread_id}")

    phase = state.get("current_phase", "unknown")
    extract = state.get("extract") or {}
    split = state.get("split") or {}
    split_phase = split.get("phase") if split else None

    # ---- 提取阶段人工审核：只返回需要人工，不自动 resume ----
    if phase == "human_review":
        if extract.get("has_interrupt"):
            current_factory = extract.get("current_factory")
            message = (
                f"工厂「{current_factory}」等待人工审核"
                if current_factory
                else "当前批次等待人工审核"
            )
            return {
                "status": "needs_human_action",
                "phase": "human_review",
                "message": message,
                "link": f"/review?thread_id={thread_id}",
            }
        # pipeline_state 推导异常：阶段名是 human_review 但没有 interrupt
        raise RuntimeError(
            f"批次正在运行中，当前阶段: {phase}，请等待挂起后再推进"
        )

    # ---- 提取完成后自动启动分票图（仅当分票尚未启动）----
    if phase in ("export", "export_done") and not split:
        result = _start_split(thread_id, extract, on_progress)
        _sync_batch_status(thread_id, result)
        return result

    # ---- 分票审核：需要人工确认 ----
    if phase == "split_review" or split_phase == "split_review":
        if split.get("has_interrupt"):
            return {
                "status": "needs_human_action",
                "phase": "split_review",
                "message": "分票方案已生成，请人工审核确认",
                "link": f"/split?thread_id={thread_id}",
            }
        raise RuntimeError(
            f"批次正在运行中，当前阶段: {phase}，请等待挂起后再推进"
        )

    # ---- 分票恢复：用 action 作为 resume 数据推进到完成 ----
    if phase in ("split_persisting", "split_generating") or split_phase in (
        "split_persisting",
        "split_generating",
    ):
        result = _resume_split(thread_id, action, on_progress)
        _sync_batch_status(thread_id, result)
        return result

    # ---- 已完成 ----
    if phase in ("completed", "split_done") or split_phase == "split_done":
        return {
            "status": "completed",
            "phase": phase if phase in ("completed", "split_done") else "split_done",
            "message": "批次已完成",
        }

    # ---- 运行中阶段：不可推进 ----
    if phase in _RUNNING_PHASES or split_phase in ("split_loading", "split_proposing"):
        raise RuntimeError(
            f"批次正在运行中，当前阶段: {phase}，请等待挂起后再推进"
        )

    # ---- 未知阶段 ----
    raise RuntimeError(f"无法自动推进，未知阶段: {phase}")


def _start_split(
    thread_id: str,
    extract_state: dict[str, Any],
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """启动分票图，从提取产物运行到中断或完成。"""
    graph = get_split_graph()
    split_thread_id = f"split-{thread_id}"
    cfg = _split_config(split_thread_id)

    # 幂等：已有 checkpoint 直接返回当前状态
    snap = graph.get_state(cfg)
    if snap.values:
        logger.info(
            "分票图已存在，直接返回当前状态: split_thread_id=%s", split_thread_id
        )
        return _split_result_from_snap(thread_id, snap)

    source_path = extract_state.get("final_output_path")
    if not source_path:
        raise RuntimeError("提取阶段尚未生成最终输出文件，无法启动分票")

    initial_state = {
        "split_thread_id": split_thread_id,
        "batch_id": thread_id,
        "source_file_path": str(Path(source_path).resolve()),
    }

    logger.info(
        "启动分票图: thread_id=%s, split_thread_id=%s", thread_id, split_thread_id
    )
    return _stream_split(graph, cfg, thread_id, initial_state, on_progress)


def _resume_split(
    thread_id: str,
    action: dict | None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """以 action 作为 resume 数据，推进分票图到完成。"""
    graph = get_split_graph()
    split_thread_id = f"split-{thread_id}"
    cfg = _split_config(split_thread_id)

    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"分票任务不存在: {split_thread_id}")

    resume_data = action if action is not None else {}
    logger.info(
        "恢复分票图: split_thread_id=%s, action_keys=%s",
        split_thread_id,
        sorted(resume_data.keys()) if isinstance(resume_data, dict) else [],
    )
    return _stream_split(
        graph, cfg, thread_id, Command(resume=resume_data), on_progress
    )


def _stream_split(
    graph,
    cfg: dict,
    thread_id: str,
    input: Any,
    on_progress: Callable[[dict], None] | None,
) -> dict:
    """流式运行分票图，将 updates 事件转为 advance_progress 回调。

    遇到 interrupt 立即停流，返回 needs_human_action；
    否则跑完整个图，返回 completed。
    """
    try:
        for event in graph.stream(input, cfg, stream_mode="updates"):
            _emit_advance_progress(event, on_progress)
            if "__interrupt__" in event:
                break
    except Exception as exc:  # noqa: BLE001
        logger.exception("分票图运行异常: thread_id=%s", thread_id)
        raise RuntimeError(f"分票推进失败: {exc}") from exc

    snap = graph.get_state(cfg)
    return _split_result_from_snap(thread_id, snap)


def _split_result_from_snap(thread_id: str, snap) -> dict:
    """从分票图快照构造 advance_batch 返回字典。"""
    has_interrupt = any(t.interrupts for t in snap.tasks)
    next_nodes = list(snap.next or [])
    current_node = next_nodes[0] if next_nodes else None

    if has_interrupt and current_node == NODE3_SPLIT:
        return {
            "status": "needs_human_action",
            "phase": "split_review",
            "message": "分票方案已生成，请人工审核确认",
            "link": f"/split?thread_id={thread_id}",
        }

    if not next_nodes:
        return {
            "status": "completed",
            "phase": "split_done",
            "message": "分票已完成，报关单已生成",
        }

    phase = _SPLIT_NODE_PHASES.get(current_node, "unknown")
    return {
        "status": "advanced",
        "phase": phase,
        "message": f"分票已推进到 {phase}",
    }


def _emit_advance_progress(
    event: dict,
    on_progress: Callable[[dict], None] | None,
) -> None:
    """把 graph.stream 的 updates 事件转成 advance_progress 回调。"""
    if on_progress is None:
        return
    try:
        for node, _value in event.items():
            if node.startswith("__"):
                continue
            message = _SPLIT_NODE_MESSAGES.get(node, f"运行节点 {node}")
            on_progress(
                {
                    "type": "advance_progress",
                    "node": node,
                    "message": message,
                }
            )
    except Exception:  # noqa: BLE001
        logger.debug("advance_progress 回调异常已忽略", exc_info=True)


def _split_config(split_thread_id: str) -> dict:
    """构造分票图 config。"""
    return {"configurable": {"thread_id": split_thread_id}}


def _sync_batch_status(thread_id: str, result: dict) -> None:
    """根据推进结果同步 batch_store 的 status（失败只记日志，不阻塞返回）。"""
    status = result.get("status")
    if status == "needs_human_action" and result.get("phase") == "split_review":
        batch_store.update_status(thread_id, "split_review")
    elif status == "completed":
        batch_store.update_status(thread_id, "completed")
