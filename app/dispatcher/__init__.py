"""调度 Agent（包入口）。

对外只暴露两个函数：
- handle_message：对话主入口（确认前），内部走 loop.run_dispatch 的
  tool-calling 循环；写工具在循环内被确认门拦截，返回 pending_confirmation；
- confirm：确认执行入口，内部走 loop.execute_confirmed（优先服务端留存的
  pending_action，客户端 action 仅作降级通道）。

双适配器设计意图见 loop.py 模块 docstring：qwen 原生 tool_calls 可靠性
未验证时，DISPATCHER_STEP_MODE=json 可零代码切换到 json_mode 备胎通道。
"""
from __future__ import annotations

from app.dispatcher import sessions as _sessions
from app.dispatcher.loop import execute_confirmed, run_dispatch


def handle_message(message: str, session_id: str | None = None, *, phase: int = 2,
                   on_progress=None) -> dict:
    """对话主入口（确认前）。session_id 缺省为临时会话（不落进程内会话表）。

    session_id 提供时：
    - L1 会话记忆（进程内 dict）：多轮对话 history
    - L2 操作记忆（SQLite 持久化）：last_thread_id / recent_paths / 操作摘要
      注入 system prompt，让 agent 知道"刚才"在做什么

    on_progress 可选回调：fn({"type": "llm_thinking"|"tool_call"|"tool_result"|
    "tool_error"|"pending_confirmation"|"final", ...})，用于 SSE 流式推送。
    """
    if not message or not message.strip():
        return {"status": "error", "message": "message 不能为空"}
    session = (_sessions.get_session(session_id) if session_id
               else _sessions.DispatcherSession())
    result = run_dispatch(message, session, phase=phase, session_id=session_id,
                          on_progress=on_progress)
    if session_id:
        result["session_id"] = session_id
    return result


def confirm(session_id: str | None, action: dict | None,
            on_progress=None) -> dict:
    """确认执行入口：优先用服务端 session 留存的 pending_action。

    on_progress（W4a）：节点级进度回调，透传 execute_confirmed → tool.execute
    （跑图类工具产生 exec_progress 事件，供 SSE 流式推送）。
    写操作成功后，自动更新 L2 操作记忆（last_thread_id / recent_paths / 操作摘要）。
    """
    session = _sessions.get_session(session_id) if session_id else None
    result = execute_confirmed(session, action, on_progress=on_progress,
                               session_id=session_id)

    # 写操作成功后，自动更新 L2 记忆
    if result.get("status") == "applied" and session_id:
        try:
            from app.dispatcher.memory import OperationMemory
            tool_name = result.get("tool", "")
            tool_args = result.get("args") or (action.get("args") if action else {}) or {}
            tool_result = result.get("result") or {}
            mem = OperationMemory(session_id)
            mem.auto_update_after_write(tool_name, tool_args, tool_result)
        except Exception:  # noqa: BLE001 L2 记忆更新失败不阻塞主流程
            pass

    return result


__all__ = ["handle_message", "confirm"]
