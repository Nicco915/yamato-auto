"""调度 Agent 的 ReAct 引擎（langgraph.prebuilt.create_react_agent 版）。

设计要点：

1. 双引擎并存期：DISPATCHER_ENGINE=react 时 handle_message 走本引擎，
   legacy（缺省）仍走 loop.run_dispatch 手写循环，行为零变化。迁移验证
   完成后 legacy 路径才退役。

2. 引擎本体极薄：只做 prompt 组装（react_prompt + L2 操作记忆上下文）、
   消息互转（session.history 的 {"role","content"} dict → HumanMessage/
   AIMessage）、一次 agent.invoke。全部工具语义——影子写工具确认门硬停、
   request_clarification 黄灯反问、ask_guide 引用收集——都在 lc_tools
   的闭包里（见 lc_tools.py 模块 docstring），引擎只读 collector 与
   session 状态，把图执行结果映射成对外返回契约。

3. 结果映射优先级（与 legacy 循环的返回形态一一对应）：
   soft_confirm（黄灯反问已布防）→ pending（影子写确认卡已存）→
   clarify（preview blocked 业务硬校验拦截）→ 正常终复（末条消息
   content——硬停时是 ToolMessage、正常结束是 AIMessage，都取 .content）。
   每条分支 record_turn 写对话历史 + on_progress final 事件，与 legacy
   循环同一口径，前端/SSE 无感知。

4. 超轮兜底：recursion_limit=12 打满（模型反复调工具不收敛）时返回
   拆分提示文案，与 legacy 超 MAX_ROUNDS 的兜底一致。注意 v2 的
   create_react_agent 用 remaining_steps 截断——不抛 GraphRecursionError，
   而是以一条内容为 _RECURSION_SENTINEL 的 AIMessage 终止图
   （chat_agent_executor._are_more_steps_needed 分支），两条路径都要识别。
"""
from __future__ import annotations

import logging
from typing import Callable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent

from app.dispatcher import debug_log, lc_llm, lc_tools, prompts, sessions
from app.dispatcher.sessions import DispatcherSession

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 12  # react 图最大步数（对应 legacy 的 MAX_ROUNDS 防死循环）

# 超轮兜底文案（与 loop.py 超 MAX_ROUNDS 的兜底一致）
_FALLBACK_TEXT = "处理步骤过多，请把问题拆小一点再问。"

# v2 create_react_agent 的 remaining_steps 截断哨兵：remaining_steps < 2 且
# 模型仍要调工具时，图以一条该内容的 AIMessage 正常终止（不抛
# GraphRecursionError），引擎须识别并映射为中文兜底文案
_RECURSION_SENTINEL = "Sorry, need more steps to process this request."


def _history_to_messages(history: list[dict]) -> list[BaseMessage]:
    """session.history 的 {"role","content"} dict → LangChain 消息对象。

    历史里只有 user/assistant 两种 role（record_turn 契约），assistant
    统一转 AIMessage，其余一律按 user 文本处理（不丢上下文）。
    """
    out: list[BaseMessage] = []
    for m in history:
        if m.get("role") == "assistant":
            out.append(AIMessage(content=m.get("content") or ""))
        else:
            out.append(HumanMessage(content=str(m.get("content") or "")))
    return out


def _emit_final(on_progress: Callable[[dict], None] | None, text: str) -> None:
    """统一发 final 进度事件（各返回分支同一口径）。"""
    if on_progress:
        on_progress({"type": "final", "message": text})


def run_dispatch_react(message: str, session: DispatcherSession, *,
                       phase: int = 2, session_id: str | None = None,
                       on_progress: Callable[[dict], None] | None = None
                       ) -> dict:
    """ReAct 调度主入口：组装 prompt/工具/消息 → agent.invoke → 结果映射。

    返回形态（与 legacy run_dispatch 对齐）：
    - {"status": "ok", "message": 最终回复}（references 非空时附 references）
    - {"status": "ok", "intent": "soft_confirm", "message": 反问文案}
      （黄灯反问已布防，等操作员一轮内「是/算了」由入口短路消费）
    - {"status": "ok", "message": 文案, "clarify": True}
      （preview blocked 业务硬校验拦截）
    - {"status": "pending_confirmation", "action": 信封, "preview": [...],
       "message": 摘要+确认提示, "warnings": [...], "factory_scan": ...}
      （影子写工具已生成确认卡，等 execute_confirmed 走人工确认通道）
    - 超 recursion_limit 的兜底 {"status": "ok", "message": 拆分提示}
    """
    # system prompt：react 专用 + L2 操作记忆上下文（注入语义同 loop.py）
    sys_prompt = prompts.react_prompt(phase)
    if session_id:
        from app.dispatcher.memory import OperationMemory
        try:
            mem = OperationMemory(session_id)
            l2_context = mem.get_context_for_prompt()
            if l2_context:
                sys_prompt += f"\n\n【最近操作上下文】\n{l2_context}"
        except Exception:  # noqa: BLE001 L2 记忆加载失败不阻塞主流程
            pass

    model = lc_llm.QwenDispatcherChatModel(on_progress=on_progress,
                                           session_id=session_id)
    tools, collector = lc_tools.build_tools(session, session_id, phase,
                                            on_progress)
    agent = create_react_agent(model, tools, prompt=sys_prompt)

    messages = _history_to_messages(session.history) + [
        HumanMessage(content=message)]
    debug_log.log_event("user_message", session_id=session_id, phase=phase,
                        message=message)

    try:
        result = agent.invoke({"messages": messages},
                              config={"recursion_limit": RECURSION_LIMIT})
    except GraphRecursionError:
        # 超步兜底（v1 图 / 防御）：多半是模型反复调工具不收敛
        logger.warning("react 图超 recursion_limit=%d，兜底回复 | session=%s",
                       RECURSION_LIMIT, session_id)
        debug_log.log_event("recursion_fallback", session_id=session_id,
                            limit=RECURSION_LIMIT)
        _emit_final(on_progress, _FALLBACK_TEXT)
        return {"status": "ok", "message": _FALLBACK_TEXT}

    # v2 remaining_steps 截断：末条是哨兵 AIMessage（模型仍想调工具但步数
    # 耗尽），同样走超轮兜底（与 legacy 一致：不写对话历史）
    last = result["messages"][-1] if result.get("messages") else None
    last_content = getattr(last, "content", "") if last is not None else ""
    if not isinstance(last_content, str):
        last_content = str(last_content)
    if last_content == _RECURSION_SENTINEL:
        logger.warning("react 图 remaining_steps 耗尽（截断哨兵），兜底回复"
                       " | session=%s", session_id)
        debug_log.log_event("recursion_fallback", session_id=session_id,
                            limit=RECURSION_LIMIT, mode="remaining_steps")
        _emit_final(on_progress, _FALLBACK_TEXT)
        return {"status": "ok", "message": _FALLBACK_TEXT}

    # ---- 结果映射（优先级：soft_confirm → pending → clarify → 正常终复）----

    # 黄灯反问已布防：request_clarification 存了 soft_pending，
    # 等操作员一轮内答复（由入口短路消费，不进 LLM）
    if collector.soft_confirm_question:
        question = collector.soft_confirm_question
        sessions.record_turn(session, message, question)
        _emit_final(on_progress, question)
        return {"status": "ok", "intent": "soft_confirm", "message": question}

    # 影子写工具已生成确认卡（本轮建的 pending_action）：绝不执行，
    # 等 confirm 走人工确认通道
    action = session.pending_action
    if action:
        # 对话历史写入预览详情（LLM 后续轮次能回答追问，如"哪些存疑"）
        history_text = action["summary"]
        if action["preview_lines"]:
            history_text += "\n" + "\n".join(action["preview_lines"])
        history_text += "\n请确认是否执行（确认后生效）。"
        sessions.record_turn(session, message, history_text)
        # 前端确认卡消息（不含 preview_lines，前端已单独渲染）
        msg_text = action["summary"] + "\n请确认是否执行（确认后生效）。"
        _emit_final(on_progress, msg_text)
        return {"status": "pending_confirmation",
                "action": action,
                "preview": action["preview_lines"],
                "message": msg_text,
                "warnings": action["warnings"],
                "factory_scan": action.get("factory_scan")}

    # preview blocked（业务硬校验失败）：直接转 clarify 回复，不让 LLM 圆场
    if collector.clarify_text:
        text = collector.clarify_text
        sessions.record_turn(session, message, text)
        _emit_final(on_progress, text)
        return {"status": "ok", "message": text, "clarify": True}

    # 正常终复：末条消息即用户可见文本（硬停时是 ToolMessage、正常结束是
    # AIMessage，统一取 .content；上面已算出 last_content）
    final_text = last_content or "（无回复内容）"
    sessions.record_turn(session, message, final_text)
    _emit_final(on_progress, final_text)
    out: dict = {"status": "ok", "message": final_text}
    if collector.references:
        out["references"] = collector.references
    return out


__all__ = ["run_dispatch_react", "RECURSION_LIMIT"]
