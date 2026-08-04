"""调度 Agent（包入口）。

对外只暴露两个函数：
- handle_message：对话主入口（确认前），先走 triage.run_triage 结构化分诊，
  再按意图三分支路由：
  - intent == "qa"：操作指南类问题，直接走 guide.ask_guide 直答（清槽位），
    不进 loop；
  - intent == "clarify" 或目标工具缺必填参数：反问补齐——已提取参数以槽位
    形式暂存 session，多轮凑齐；target_tool 为空且已有槽位视为用户中止，
    清槽位；
  - intent == "action" 按置信度三级路由（代码侧映射，不动 TriageResult
    契约）：绿灯（>0.8）槽位落 session 后带 triage_hint 进
    loop.run_dispatch；黄灯（0.6–0.8 且目标是写工具）存 soft_pending
    软挂起 + 回复确认式反问，不进 loop——下一条消息"是"则按已存意图
    直接带 hint 进 loop（不再过 triage），"算了"则全清，其他消息清挂起
    后正常走新一轮分诊；黄灯豁免（0.6–0.8 但只读工具）与红灯（<0.6）
    不带 hint 走旧循环；
  triage 返回 None（mock 空剧本 / 开关关闭 / 校验失败）时降级旧 loop 循环，
  行为与重构前完全一致。
- confirm：确认执行入口，内部走 loop.execute_confirmed（优先服务端留存的
  pending_action，客户端 action 仅作降级通道）；终态清空 Triage 槽位。

双适配器设计意图见 loop.py 模块 docstring：qwen 原生 tool_calls 可靠性
未验证时，DISPATCHER_STEP_MODE=json 可零代码切换到 json_mode 备胎通道。
"""
from __future__ import annotations

from app.dispatcher import sessions as _sessions
from app.dispatcher import triage as _triage
from app.dispatcher.loop import execute_confirmed, run_dispatch
from app.dispatcher.tools import TOOLS as _TOOLS

# 缺参反问文案的参数中文名映射（未收录的参数名用"必要信息"兜底）
_PARAM_CN = {"thread_id": "批次号", "paths": "路径配置"}

# 写工具集合：从注册表派生（risk=="write"），不硬编码——新增写工具自动
# 纳入黄灯区拦截范围
_WRITE_TOOL_NAMES = frozenset(
    name for name, t in _TOOLS.items() if t.risk == "write")

# 黄灯区确认式反问的工具中文名映射（未收录的用"该操作"兜底）
_TOOL_CN = {
    "create_batch": "发起新批次",
    "rerun": "整批重跑",
    "retry_factory": "重试当前工厂",
    "submit_review": "提交审核",
    "set_paths": "修改路径配置",
    "curate_kb": "排查知识库",
}

# 软挂起一轮窗口的确认/否定判定：strip + 小写后全串精确匹配（确定性
# 模式匹配，不用 LLM——只覆盖最常见的短答，复杂表达落入"其他消息"
# 分支正常走新一轮分诊，不会误执行写操作）
_SOFT_CONFIRM_YES = frozenset(
    {"是", "对", "嗯", "好", "没错", "确认", "是的", "好的", "ok"})
_SOFT_CONFIRM_NO = frozenset({"不", "不是", "不对", "算了", "取消", "不用了"})


def _soft_confirm_question(tool: str, slots: dict) -> str:
    """黄灯区缺省确认式反问（triage 未给 reply_message 时代码拼装）。

    槽位摘要只翻译 thread_id（"批次 X"）/ factory（"工厂 X"）两键，
    其余不展开（Dispatcher 对外文案不得暴露内部参数名）。
    """
    cn = _TOOL_CN.get(tool, "该操作")
    parts = []
    if slots.get("thread_id"):
        parts.append(f"批次 {slots['thread_id']}")
    if slots.get("factory"):
        parts.append(f"工厂 {slots['factory']}")
    summary = f"（{'、'.join(parts)}）" if parts else ""
    return f"您是想要{cn}{summary}吗？回复“是”即可继续。"


def _consume_soft_pending(session: _sessions.DispatcherSession, message: str,
                          *, phase: int, session_id: str | None,
                          on_progress) -> dict | None:
    """黄灯区软挂起的一轮窗口消费（先于 run_triage 调用）。

    - 确认（是/对/好的/ok…）：按已存 target_tool+slots 直接走高置信
      action 路径（带 hint 进 loop），不再过 triage；
    - 否定/中止（不/算了/取消…）：软挂起与槽位全清，回复取消文案；
    - 其他消息：清软挂起，返回 None 继续正常 triage 流程（用户补充
      参数或换话题，新消息带 history 会被正常分诊合槽，不丢信息）。
    """
    pending = session.soft_pending
    if not pending:
        return None
    text = message.strip().lower()
    if text in _SOFT_CONFIRM_YES:
        tool = pending["target_tool"]
        slots = dict(pending["slots"])
        _sessions.clear_soft_pending(session)
        # 提升为高置信 action：槽位落 session，带 hint 进 loop（确认门
        # 语义不变——写工具仍会出预览卡等人工确认）
        _sessions.set_slots(session, tool, slots)
        hint = {"target_tool": tool, "args": slots}
        result = run_dispatch(message, session, phase=phase,
                              session_id=session_id, on_progress=on_progress,
                              triage_hint=hint)
        if (result.get("status") == "pending_confirmation"
                or result.get("clarify")):
            _sessions.clear_slots(session)
        return result
    if text in _SOFT_CONFIRM_NO:
        _sessions.clear_slots(session)  # 内含 soft_pending 清理
        reply = "好的，已取消。请告诉我您的实际需求。"
        _sessions.record_turn(session, message, reply)
        if on_progress:
            on_progress({"type": "final", "message": reply})
        return {"status": "ok", "message": reply, "intent": "soft_cancel"}
    # 其他消息：清软挂起后继续正常 triage
    _sessions.clear_soft_pending(session)
    return None


def _last_thread_id(session_id: str | None) -> str | None:
    """qa 直答缺 thread_id 时，从 L2 操作记忆兜底取 last_thread_id。"""
    if not session_id:
        return None
    try:
        from app.dispatcher.memory import OperationMemory
        return OperationMemory(session_id).load().get("last_thread_id")
    except Exception:  # noqa: BLE001 兜底失败不阻塞主流程
        return None


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

    # 黄灯区软挂起一轮窗口：先于 triage 消费（确认提升 / 否定取消 /
    # 其他消息清挂起后继续正常分诊）
    soft_result = _consume_soft_pending(session, message, phase=phase,
                                        session_id=session_id,
                                        on_progress=on_progress)
    if soft_result is not None:
        if session_id:
            soft_result["session_id"] = session_id
        return soft_result

    # Triage 结构化分诊：None = 降级（空剧本/开关关闭/校验失败），走旧循环
    triage_result = _triage.run_triage(message, session, phase=phase,
                                       session_id=session_id,
                                       on_progress=on_progress)

    if triage_result is None:
        # 降级：旧路行为与重构前完全一致
        result = run_dispatch(message, session, phase=phase,
                              session_id=session_id, on_progress=on_progress)
    elif triage_result.intent == "qa":
        # qa 直答：清槽位（换话题），不进 loop
        tid = (triage_result.extracted_args.get("thread_id")
               or _last_thread_id(session_id))
        _sessions.clear_slots(session)
        from app.dispatcher import guide
        guide_result = guide.ask_guide(question=message, thread_id=tid)
        answer = guide_result["answer"]
        _sessions.record_turn(session, message, answer)
        if on_progress:
            on_progress({"type": "final", "message": answer})
        result = {"status": "ok", "message": answer,
                  "references": guide_result.get("references", []),
                  "intent": "qa"}
    else:
        merged = _triage.merge_slots(session, triage_result)
        missing = _triage.missing_required(triage_result.target_tool, merged)
        if triage_result.intent == "clarify" or missing:
            # 反问补齐；target_tool 为空且已有槽位 = 用户中止，清槽位
            if triage_result.target_tool is None and session.current_slots:
                _sessions.clear_slots(session)
            else:
                _sessions.set_slots(session, triage_result.target_tool, merged)
            reply = triage_result.reply_message
            if not reply:
                names = "、".join(_PARAM_CN.get(p, "必要信息") for p in missing)
                reply = f"请提供{names}后再继续。"
            _sessions.record_turn(session, message, reply)
            if on_progress:
                on_progress({"type": "final", "message": reply})
            result = {"status": "ok", "message": reply, "intent": "clarify"}
        elif triage_result.confidence > 0.8:
            # 绿灯（高置信 action）：槽位落 session，带 hint 进 loop
            _sessions.set_slots(session, triage_result.target_tool, merged)
            hint = {"target_tool": triage_result.target_tool, "args": merged}
            result = run_dispatch(message, session, phase=phase,
                                  session_id=session_id, on_progress=on_progress,
                                  triage_hint=hint)
            if (result.get("status") == "pending_confirmation"
                    or result.get("clarify")):
                _sessions.clear_slots(session)
        elif (triage_result.confidence >= 0.6
              and triage_result.target_tool in _WRITE_TOOL_NAMES):
            # 黄灯区：0.6–0.8 置信的写工具请求——不让模型猜测的参数直接
            # 冲进确认卡，先存软挂起 + 回复确认式反问，等操作员一轮内
            # 确认（下一条消息由入口短路消费）；只读工具查错无代价，
            # 不拦截（落入下方旧循环）
            _sessions.set_slots(session, triage_result.target_tool, merged)
            reply = triage_result.reply_message or _soft_confirm_question(
                triage_result.target_tool, merged)
            _sessions.set_soft_pending(
                session, triage_result.target_tool, merged, reply)
            _sessions.record_turn(session, message, reply)
            if on_progress:
                on_progress({"type": "final", "message": reply})
            result = {"status": "ok", "message": reply,
                      "intent": "soft_confirm"}
        else:
            # 黄灯豁免（低置信只读）/ 红灯：不带 hint 走旧循环
            result = run_dispatch(message, session, phase=phase,
                                  session_id=session_id, on_progress=on_progress)

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

    # 终态清空 Triage 槽位（成功/失败/过期都算尘埃落定）
    if (result.get("status") in ("applied", "error", "expired")
            and session is not None):
        _sessions.clear_slots(session)

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
