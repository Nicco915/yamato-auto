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


def handle_message(message: str, session_id: str | None = None, *, phase: int = 2) -> dict:
    """对话主入口（确认前）。session_id 缺省为临时会话（不落进程内会话表）。"""
    if not message or not message.strip():
        return {"status": "error", "message": "message 不能为空"}
    session = (_sessions.get_session(session_id) if session_id
               else _sessions.DispatcherSession())
    result = run_dispatch(message, session, phase=phase)
    if session_id:
        result["session_id"] = session_id
    return result


def confirm(session_id: str | None, action: dict | None) -> dict:
    """确认执行入口：优先用服务端 session 留存的 pending_action。"""
    session = _sessions.get_session(session_id) if session_id else None
    return execute_confirmed(session, action)


__all__ = ["handle_message", "confirm"]
