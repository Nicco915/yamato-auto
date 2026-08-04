"""Node 4B: 失败工厂暂缓节点（Defer Node，W6a）。

提取失败（Node3 标记 extraction_ok=False）且后面还有工厂待处理时，
Node4 的条件边把流转引到这里：把当前工厂记入 deferred_factories 暂缓队列，
跳过 Node5/Node6 直接回 Node2 处理下一个工厂——占位数据绝不写回
Excel/主库，零容错红线不破。主队列走完后 Node2 弹出暂缓条目做二遍重试，
仍失败才最终挂起人工补录。
"""
import logging

from app.state import AgentState

logger = logging.getLogger(__name__)


def defer_node(state: AgentState) -> dict:
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name") or "未知工厂"
    reason = cur.get("failure_reason") or "unknown"

    deferred = list(state.get("deferred_factories") or [])
    deferred.append({"factory_name": factory, "failure_reason": reason})

    logger.warning("[Node4B] 工厂「%s」提取失败（%s），已暂缓，"
                   "将在其余工厂处理完后重试（当前暂缓 %d 个）",
                   factory, reason, len(deferred))

    # 不需要清 current_factory_data——Node2 下次弹出会整体覆写
    return {"deferred_factories": deferred}
