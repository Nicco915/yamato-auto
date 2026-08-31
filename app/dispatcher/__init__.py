"""调度 Agent（包入口）。

对外只暴露两个函数：
- handle_message：对话主入口（确认前）。链路固定为：
  软挂起短路消费（黄灯区「是/算了」一轮窗口，不进 LLM）
  → fastpath 快路径（高频纯读查询规则命中时零 LLM 直接返回）
  → react_engine.run_dispatch_react（langgraph create_react_agent
  引擎：意图判别与多轮协商由单循环模型自主完成，影子写工具确认门、
  黄灯 request_clarification、L2 记忆注入等铁律语义不变）。
- confirm：确认执行入口，走 executor.execute_confirmed（优先服务端留存的
  pending_action，客户端 action 仅作降级通道）；终态清空软挂起。

legacy 引擎（triage 分诊 + loop 手写循环）已删除（2026-08-31），
react 为唯一引擎；确认门执行器独立为 executor.py（与引擎无关）。
"""
from __future__ import annotations

from app.dispatcher import sessions as _sessions
from app.dispatcher.executor import execute_confirmed

# 软挂起一轮窗口的确认/否定判定：strip + 小写后全串精确匹配（确定性
# 模式匹配，不用 LLM——只覆盖最常见的短答，复杂表达落入"其他消息"
# 分支继续走引擎，不会误执行写操作）
_SOFT_CONFIRM_YES = frozenset(
    {"是", "对", "嗯", "好", "没错", "确认", "是的", "好的", "ok"})
_SOFT_CONFIRM_NO = frozenset({"不", "不是", "不对", "算了", "取消", "不用了"})


def _handle_message_react(message: str, session: _sessions.DispatcherSession,
                          *, phase: int, session_id: str | None,
                          on_progress) -> dict:
    """主链路：软挂起短路消费 → fastpath 快路径 → react 引擎。

    黄灯区软挂起一轮窗口：
    - 确认（是/对/好的/ok…）：**不进 LLM**——soft_pending 的
      args 在布防时（request_clarification 工具内）已过 validate_args，
      直接走 lc_tools.build_pending_action 出预览确认卡；
    - 否定/中止（不/算了/取消…）：槽位与软挂起全清，回复取消文案；
    - 其他消息：清软挂起后继续走引擎（用户补充参数或换话题，新消息带
      history 进 react 循环，不丢信息）。
    """
    from app.dispatcher import lc_tools as _lc_tools
    from app.dispatcher import react_engine as _react_engine

    pending = session.soft_pending
    if pending:
        text = message.strip().lower()
        if text in _SOFT_CONFIRM_YES:
            outcome = _lc_tools.build_pending_action(
                pending["target_tool"], dict(pending["slots"]), session,
                on_progress=on_progress, session_id=session_id)
            _sessions.clear_soft_pending(session)
            if outcome["ok"] and outcome["clarify"]:
                # preview blocked（业务硬校验失败）：转 clarify 回复
                reply = outcome["msg_text"]
                _sessions.record_turn(session, message, reply)
                if on_progress:
                    on_progress({"type": "final", "message": reply})
                return {"status": "ok", "message": reply, "clarify": True}
            if outcome["ok"]:
                # 确认卡已存 session.pending_action（映射同 react_engine）
                action = outcome["action"]
                _sessions.record_turn(session, message,
                                      outcome["history_text"])
                if on_progress:
                    on_progress({"type": "final",
                                 "message": outcome["msg_text"]})
                return {"status": "pending_confirmation",
                        "action": action,
                        "preview": action["preview_lines"],
                        "message": outcome["msg_text"],
                        "warnings": action["warnings"],
                        "factory_scan": action.get("factory_scan")}
            # 布防参数失效（如已有其他待确认操作）：原样转述错误文案
            reply = outcome["msg_text"]
            _sessions.record_turn(session, message, reply)
            if on_progress:
                on_progress({"type": "final", "message": reply})
            return {"status": "ok", "message": reply}
        if text in _SOFT_CONFIRM_NO:
            _sessions.clear_soft_pending(session)
            reply = "好的，已取消。请告诉我您的实际需求。"
            _sessions.record_turn(session, message, reply)
            if on_progress:
                on_progress({"type": "final", "message": reply})
            return {"status": "ok", "message": reply, "intent": "soft_cancel"}
        # 其他消息：清软挂起后继续走引擎
        _sessions.clear_soft_pending(session)

    # 快路径：高频纯读查询（批次列表/批次状态/用量）规则命中时跳过 LLM
    # 直接调只读工具返回（毫秒级）；不命中返回 None 继续走 react 引擎。
    # 放在软挂起消费之后，避免旧软挂起被新问题搁置成 stale
    from app.dispatcher import debug_log as _debug_log
    from app.dispatcher import fastpath as _fastpath
    fp = _fastpath.try_fastpath(message)
    if fp is not None:
        _debug_log.log_event("fastpath_hit", session_id=session_id,
                             tool=fp["tool"], args=fp["args"])
        _sessions.record_turn(session, message, fp["message"])
        if on_progress:
            on_progress({"type": "final", "message": fp["message"]})
        return {"status": "ok", "message": fp["message"],
                "intent": "fastpath"}

    return _react_engine.run_dispatch_react(
        message, session, phase=phase, session_id=session_id,
        on_progress=on_progress)


def _handle_file_selection_response(file_path: str, session_id: str | None, *,
                                     phase: int, on_progress) -> dict:
    """处理用户在界面选择的文件/目录路径。

    注入为新用户消息让 Agent 看到，清除 pending_file_selection 状态。
    如果没有 pending 状态（超时或无效），返回错误。
    """
    session = (_sessions.get_session(session_id) if session_id
               else _sessions.DispatcherSession())
    pending = session.pending_file_selection

    if not pending:
        # 没有挂起的文件选择请求，忽略
        reply = "（未找到待处理的文件选择请求，可能已超时或已处理）"
        if on_progress:
            on_progress({"type": "final", "message": reply})
        return {"status": "ok", "message": reply}

    # 清除挂起状态
    _sessions.clear_file_selection_request(session)

    # 根据类型生成用户消息
    fs_type = pending.get("type", "路径")
    title = pending.get("title", "")
    user_msg = f"（用户通过文件浏览器选择了{title or '路径'}）：{file_path}"

    # 记录到历史并让 Agent 继续
    from app.dispatcher import react_engine as _react_engine
    result = _react_engine.run_dispatch_react(
        user_msg, session, phase=phase, session_id=session_id,
        on_progress=on_progress)

    if session_id:
        result["session_id"] = session_id
    return result


def handle_message(message: str, session_id: str | None = None, *, phase: int = 2,
                   on_progress=None, file_selection: str | None = None) -> dict:
    """对话主入口（确认前）。session_id 缺省为临时会话（不落进程内会话表）。

    session_id 提供时：
    - L1 会话记忆（进程内 dict）：多轮对话 history
    - L2 操作记忆（SQLite 持久化）：last_thread_id / recent_paths / 操作摘要
      注入 system prompt，让 agent 知道"刚才"在做什么

    on_progress 可选回调：fn({"type": "llm_thinking"|"tool_call"|"tool_result"|
    "tool_error"|"pending_confirmation"|"final", ...})，用于 SSE 流式推送。

    file_selection 用户通过界面选择的文件/目录路径（与 message 互斥，
    优先级更高）——request_file_selection 工具挂起后用户选择完成时走此分支，
    路径注入为新用户消息让 Agent 看到。
    """
    # ---- 文件选择回复（UI 交互结果注入为新消息）----
    if file_selection is not None:
        return _handle_file_selection_response(
            file_selection, session_id, phase=phase, on_progress=on_progress)

    if not message or not message.strip():
        return {"status": "error", "message": "message 不能为空"}
    session = (_sessions.get_session(session_id) if session_id
               else _sessions.DispatcherSession())

    result = _handle_message_react(message, session, phase=phase,
                                   session_id=session_id,
                                   on_progress=on_progress)
    if session_id:
        result["session_id"] = session_id
    return result


def confirm(session_id: str | None, action: dict | None,
            on_progress=None) -> dict:
    """确认执行入口：优先用服务端 session 留存的 pending_action。

    on_progress：节点级进度回调，透传 execute_confirmed → tool.execute
    （跑图类工具产生 exec_progress 事件，供 SSE 流式推送）。
    写操作成功后，自动更新 L2 操作记忆（last_thread_id / recent_paths / 操作摘要）。
    """
    session = _sessions.get_session(session_id) if session_id else None
    result = execute_confirmed(session, action, on_progress=on_progress,
                               session_id=session_id)

    # 终态清空软挂起（成功/失败/过期都算尘埃落定）
    if (result.get("status") in ("applied", "error", "expired")
            and session is not None):
        _sessions.clear_soft_pending(session)

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
