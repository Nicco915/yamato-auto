"""Service 层：LangGraph 跑图逻辑集中在这里，路由层保持极薄。

【Celery 迁移预留接缝】
当前为同步实现（由路由层用 asyncio.to_thread 包裹调用，避免阻塞事件循环）。
日后迁移 Celery+Redis 时：
1. 把 run_until_interrupt() 整体下沉为 Celery task（worker 内直接调用即可，
   函数签名不变）；
2. 路由改为 task.delay(...) 返回 task_id，再加一个轮询接口查 AsyncResult；
3. resume_order() 因只做写 Excel+落库，耗时短，可保持同步接口不变。
"""
from typing import Any

from langgraph.types import Command

from app.graph import get_graph


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def run_until_interrupt(
    thread_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
) -> dict[str, Any]:
    """启动流程，执行 Node1-4 直到 Node5 的 interrupt 挂起（或全程无挂起跑完）。

    返回：
      - {"status": "pending_human_review", "review_data": payload}
      - {"status": "completed", "final_state": ...}
    """
    graph = get_graph()
    initial_state: dict[str, Any] = {}
    if downstream_file_path:
        initial_state["downstream_file_path"] = downstream_file_path
    if upstream_root:
        initial_state["upstream_root"] = upstream_root
    if factory_filter:
        initial_state["factory_filter"] = factory_filter

    for event in graph.stream(initial_state, _config(thread_id), stream_mode="updates"):
        if "__interrupt__" in event:
            payload = event["__interrupt__"][0].value
            return {
                "status": "pending_human_review",
                "thread_id": thread_id,
                "review_data": payload,
            }

    final = graph.get_state(_config(thread_id))
    return {
        "status": "completed",
        "thread_id": thread_id,
        "final_output_path": final.values.get("final_output_path"),
    }


def resume_order(thread_id: str, resume_data: dict) -> dict[str, Any]:
    """用人工反馈数据唤醒挂起的图，继续执行 Node6/7（写 Excel + 落库）。

    resume_data 结构见 nodes/human_review.py 的 docstring。
    """
    graph = get_graph()
    state = graph.get_state(_config(thread_id))
    if not state.next:
        raise ValueError("该任务没有处于等待审核状态，或已完成。")

    # 恢复执行，直到下一个 interrupt（多工厂循环时）或 END
    for event in graph.stream(
        Command(resume=resume_data), _config(thread_id), stream_mode="updates"
    ):
        if "__interrupt__" in event:
            payload = event["__interrupt__"][0].value
            return {
                "status": "pending_human_review",
                "thread_id": thread_id,
                "review_data": payload,
            }

    final = graph.get_state(_config(thread_id))
    return {
        "status": "success",
        "message": "数据已成功落库并写入下游表格",
        "final_validation_status": final.values.get("validation_status"),
        "final_output_path": final.values.get("final_output_path"),
    }


def rerun_with_paths(
    thread_id: str,
    upstream_root: str | None = None,
    downstream_file_path: str | None = None,
) -> dict[str, Any]:
    """对话改路径后的当前批次重跑：带新路径从 Node1 重新执行到 Node5 挂起。

    机制（langgraph 1.2.9 实测）：挂起线程有未完成的 interrupt 任务，
    Command(goto=...) 输入会被旧任务卡死；必须先 update_state(as_node=START)
    写入新路径并作废旧现场（下一个 checkpoint 从 Node1 重新开始），
    再 invoke(None) 触发 Node1→Node5 全链重跑，Node5 产生新 interrupt payload。

    仅当 thread 处于挂起等待状态时允许重跑；已完成的批次抛错
    （路径修改已写 .env，对后续批次生效）。
    """
    from langgraph.graph import START

    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")
    if not snap.next:
        raise ValueError(
            f"thread {thread_id} 已完成，无法重跑（路径修改已对后续批次生效）"
        )

    update: dict[str, Any] = {}
    if upstream_root:
        update["upstream_root"] = upstream_root
    if downstream_file_path:
        update["downstream_file_path"] = downstream_file_path

    graph.update_state(cfg, update, as_node=START)
    for event in graph.stream(None, cfg, stream_mode="updates"):
        if "__interrupt__" in event:
            return {
                "status": "pending_human_review",
                "thread_id": thread_id,
                "review_data": event["__interrupt__"][0].value,
            }

    final = graph.get_state(cfg)
    return {
        "status": "completed",
        "thread_id": thread_id,
        "final_output_path": final.values.get("final_output_path"),
    }


def get_order_state(thread_id: str) -> dict[str, Any]:
    """查询指定 thread_id 的当前状态（前端轮询/调试入口）。"""
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    return {
        "thread_id": thread_id,
        "exists": bool(snap.values),
        "next_nodes": list(snap.next),
        "values": snap.values,
    }


def get_review_payload(thread_id: str) -> dict[str, Any] | None:
    """从 checkpoint 读取当前挂起的 interrupt payload（审核界面刷新后恢复现场用）。

    注意：payload 不在 state.values 里，而在 tasks[].interrupts 中。
    未挂起或 thread_id 不存在时返回 None。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    for task in snap.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None
