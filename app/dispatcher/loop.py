"""调度 Agent 的 tool-calling 循环（核心状态机）。

设计要点：

1. 双适配器（llm_step）：qwen 原生 tool_calls 的可靠性在生产上尚未验证，
   因此把「LLM 一步」抽象成统一返回结构
   {"final_text": str | None, "tool_calls": [{"id", "name", "args": dict}]}，
   由 DISPATCHER_STEP_MODE 选实现——
   - native（默认）：走 chat_completion_with_tools，模型直接吐 tool_calls；
   - json：走 chat_completion(json_mode)，system 侧附加一段协议说明，模型只输出
     {"tool_calls":[{"name","args":{...}}]} 或 {"final":"..."} 之一。
   两种模式切换零代码改动（只改环境变量），循环本体完全不感知差异。
   DISPATCHER_MOCK=1 时从模块级 _MOCK_SCRIPT 弹剧本，是确定性测试的关键口。

2. 确认门（铁律：LLM 只解析不做决策，写工具必须人工确认）：
   risk=="write" 的工具一律不执行——只调 preview 生成预览，存进
   session.pending_action 后循环立即终止，等 execute_confirmed 走人工确认通道。
   一次一确认：一条消息最多产出一个 pending action，同轮其余写调用直接忽略。
   confirm 防线：执行前再过一次 validate_args，且 action 有 ACTION_TTL_SEC 超时。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

from app.dispatcher import prompts, sessions
from app.dispatcher.sessions import DispatcherSession
from app.dispatcher.summarize import summarize_applied
from app.dispatcher.tools import TOOLS, openai_tool_defs, validate_args, visible_tools

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6          # 单次消息允许的最大 LLM 步数（防工具调用死循环）
TOOL_RESULT_CAP = 6000  # 工具结果 JSON 序列化后回喂 LLM 的字符上限
ACTION_TTL_SEC = 30 * 60  # pending action 有效期：超时须重新发起（防陈旧确认）

# json 模式协议说明：附加在 system prompt 之后（原生 tool_calls 不可信时的备胎通道）
_JSON_PROTOCOL = """
【输出协议】你没有原生工具调用能力。每一步只能输出以下两种 JSON 之一，不得输出其他内容：
1. 调用一个或多个工具：{"tool_calls": [{"name": "工具名", "args": {...}}]}
2. 给出最终回复：{"final": "回复内容（中文）"}
可用工具清单（name/description/parameters 即 JSON Schema）：
{tool_defs}
""".strip()

# DISPATCHER_MOCK=1 时的确定性剧本：元素即 llm_step 返回值
# （{"tool_calls": [...]} 或 {"final_text": "..."}），测试侧直接 append。
_MOCK_SCRIPT: list[dict] = []


# ---------------------------------------------------------------------------
# llm_step：统一接口 + 双适配器 + mock 剧本
# ---------------------------------------------------------------------------

def _step_mode() -> str:
    """读取适配器模式（每次调用现读环境变量，测试可随时切换）。"""
    mode = os.environ.get("DISPATCHER_STEP_MODE", "native").strip().lower()
    return mode if mode in ("native", "json") else "native"


def llm_step(messages: list[dict], *, phase: int) -> dict:
    """执行一步 LLM 推理，返回统一结构。

    返回 {"final_text": str | None, "tool_calls": [{"id", "name", "args": dict}]}：
    - args 已是解析后的 dict（native 模式 json.loads arguments）；解析失败返回
      {"name": "__parse_error__", "args": {"raw": ...}}，由循环回喂错误让模型重试；
    - final_text 与 tool_calls 互斥：有 tool_calls 时 final_text 为 None。
    """
    if os.environ.get("DISPATCHER_MOCK") == "1":
        if not _MOCK_SCRIPT:
            return {"final_text": "[mock] 剧本已用尽", "tool_calls": []}
        scripted = _MOCK_SCRIPT.pop(0)
        # 防御性归一：剧本允许只写其中一个 key
        return {"final_text": scripted.get("final_text"),
                "tool_calls": scripted.get("tool_calls") or []}

    if _step_mode() == "json":
        return _llm_step_json(messages, phase=phase)
    return _llm_step_native(messages, phase=phase)


def _llm_step_native(messages: list[dict], *, phase: int) -> dict:
    """native 适配器：OpenAI 原生 tool-calling。"""
    from app.extraction import llm_client  # 延迟 import：无 API key 时 mock 仍可用

    resp = llm_client.chat_completion_with_tools(
        messages,
        tools=openai_tool_defs(phase),
        source_file="dispatcher",
    )
    calls: list[dict] = []
    for tc in resp["tool_calls"]:
        try:
            args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            if not isinstance(args, dict):
                raise ValueError("arguments 不是 JSON 对象")
        except (json.JSONDecodeError, ValueError):
            # 参数 JSON 坏了：不丢，换成哨兵让循环回喂错误原文
            calls.append({"id": tc["id"], "name": "__parse_error__",
                          "args": {"raw": tc["arguments"]}})
        else:
            calls.append({"id": tc["id"], "name": tc["name"], "args": args})
    # 无 tool_calls 时 content 即最终回复
    final_text = resp["content"] if not calls else None
    return {"final_text": final_text, "tool_calls": calls}


def _llm_step_json(messages: list[dict], *, phase: int) -> dict:
    """json 适配器：json_mode + system 侧输出协议（零代码切换的备胎通道）。"""
    from app.extraction import llm_client  # 延迟 import：无 API key 时 mock 仍可用

    # 不改动调用方的 messages：拷贝后在 system 侧附加协议说明 + 工具清单
    tool_defs = json.dumps(openai_tool_defs(phase), ensure_ascii=False, indent=1)
    protocol = _JSON_PROTOCOL.replace("{tool_defs}", tool_defs)
    augmented = list(messages)
    if augmented and augmented[0].get("role") == "system":
        augmented[0] = {"role": "system",
                        "content": augmented[0]["content"] + "\n\n" + protocol}
    else:
        augmented.insert(0, {"role": "system", "content": protocol})

    raw = llm_client.chat_completion(
        augmented, json_mode=True, source_file="dispatcher", max_tokens=4096,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 解析失败：当普通回复原样返回（不视为工具调用）
        return {"final_text": raw, "tool_calls": []}
    if not isinstance(parsed, dict):
        return {"final_text": raw, "tool_calls": []}

    if isinstance(parsed.get("tool_calls"), list):
        calls = []
        for i, c in enumerate(parsed["tool_calls"]):
            if not isinstance(c, dict):
                continue
            args = c.get("args")
            calls.append({"id": f"jsoncall_{i}",
                          "name": str(c.get("name") or ""),
                          "args": args if isinstance(args, dict) else {}})
        if calls:
            return {"final_text": None, "tool_calls": calls}
    if parsed.get("final") is not None:
        return {"final_text": str(parsed["final"]), "tool_calls": []}
    # 结构不认识：同样当普通回复兜底
    return {"final_text": raw, "tool_calls": []}


# ---------------------------------------------------------------------------
# 调度循环
# ---------------------------------------------------------------------------

def _args_summary(args: dict) -> str:
    """工具参数的紧凑摘要（写 tool_history 用，截断防爆）。"""
    return json.dumps(args, ensure_ascii=False, default=str)[:300]


def _result_summary(result: dict) -> str:
    """工具结果的紧凑摘要（写 tool_history 用，截断防爆）。"""
    return json.dumps(result, ensure_ascii=False, default=str)[:300]


# 日志专用摘要上限（比 tool_history 的 300 更紧，单行可读）
_LOG_SUMMARY_CAP = 200
# 敏感参数键：命中即脱敏，绝不进日志（api_key/token/secret/password 类）
_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password", "passwd")


def _redact_args(args: dict) -> dict:
    """参数脱敏拷贝：敏感键值替换为 ***（日志专用，不影响原对象）。"""
    safe: dict = {}
    for k, v in args.items():
        if any(part in str(k).lower() for part in _SENSITIVE_KEY_PARTS):
            safe[k] = "***"
        else:
            safe[k] = v
    return safe


def _log_args_summary(args: dict) -> str:
    """日志用的参数摘要：先脱敏再截断，绝不泄露 api_key 类敏感值。"""
    try:
        return json.dumps(_redact_args(args), ensure_ascii=False, default=str)[:_LOG_SUMMARY_CAP]
    except Exception:  # noqa: BLE001 日志绝不抛异常
        return "<参数摘要序列化失败>"


def _log_result_summary(result: dict) -> str:
    """日志用的结果摘要（截断防爆）。"""
    try:
        return json.dumps(result, ensure_ascii=False, default=str)[:_LOG_SUMMARY_CAP]
    except Exception:  # noqa: BLE001 日志绝不抛异常
        return "<结果摘要序列化失败>"


def _assistant_message(step: dict) -> dict:
    """把一步 tool_calls 以 assistant 消息形态回写进 messages。

    native 模式用 OpenAI 标准 tool_calls 结构；json 模式回写模型自己的输出
    协议格式，保持上下文自洽。__parse_error__ 时原样回带 raw 参数。
    """
    calls = step["tool_calls"]
    if _step_mode() == "json":
        content = json.dumps(
            {"tool_calls": [{"name": c["name"], "args": c["args"]} for c in calls]},
            ensure_ascii=False, default=str)
        return {"role": "assistant", "content": content}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"],
                          "arguments": (c["args"].get("raw", "")
                                        if c["name"] == "__parse_error__"
                                        else json.dumps(c["args"], ensure_ascii=False,
                                                        default=str))}}
            for c in calls
        ],
    }


def _tool_message(call: dict, content: str) -> dict:
    """构造 tool role 回喂消息（两种模式通用）。"""
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


def run_dispatch(message: str, session: DispatcherSession, *, phase: int = 2,
                 session_id: str | None = None,
                 on_progress: Callable[[dict], None] | None = None) -> dict:
    """调度主循环：LLM 步进 → 工具执行/拦截 → 回喂，直到最终回复或确认门。

    返回三种形态之一：
    - {"status": "ok", "message": 最终回复}
    - {"status": "pending_confirmation", "action": 信封, "preview": [...],
       "message": 摘要+确认提示, "warnings": [...]}（写工具被拦截，等人工确认）
    - 超 MAX_ROUNDS 的兜底 {"status": "ok", "message": 拆分提示}

    session_id 提供时，加载 L2 操作记忆（跨会话持久化）并注入 system prompt。
    """
    # 构建 system prompt（基础 + L2 记忆上下文）
    sys_prompt = prompts.system_prompt(phase)
    if session_id:
        from app.dispatcher.memory import OperationMemory
        try:
            mem = OperationMemory(session_id)
            l2_context = mem.get_context_for_prompt()
            if l2_context:
                sys_prompt += f"\n\n【最近操作上下文】\n{l2_context}"
        except Exception:  # noqa: BLE001 L2 记忆加载失败不阻塞主流程
            pass

    messages = ([{"role": "system", "content": sys_prompt}]
                + list(session.history)
                + [{"role": "user", "content": message}])

    for _round in range(MAX_ROUNDS):
        if on_progress:
            on_progress({"type": "llm_thinking", "round": _round + 1})
        step = llm_step(messages, phase=phase)

        # 无工具调用：最终回复，记一轮对话后返回
        if not step["tool_calls"]:
            final_text = step["final_text"] or "（无回复内容）"
            sessions.record_turn(session, message, final_text)
            if on_progress:
                on_progress({"type": "final", "message": final_text})
            return {"status": "ok", "message": final_text}

        # 有工具调用：先把 assistant 消息回写进上下文，再逐个处理
        messages.append(_assistant_message(step))
        visible = {t.name for t in visible_tools(phase)}

        for call in step["tool_calls"]:
            name = call["name"]

            # 哨兵：native 模式参数 JSON 解析失败，回喂让模型重新调用
            if name == "__parse_error__":
                logger.warning(
                    "工具错误：参数 JSON 解析失败，已回喂重试 | raw=%s",
                    str(call["args"].get("raw", ""))[:_LOG_SUMMARY_CAP])
                if on_progress:
                    on_progress({"type": "tool_error", "tool": name,
                                 "error": "工具参数 JSON 解析失败"})
                messages.append(_tool_message(
                    call, "工具参数 JSON 解析失败，请重新调用。"
                          f"原始输出：{str(call['args'].get('raw', ''))[:500]}"))
                continue

            # 未知工具 / 当前 phase 不可见：回喂错误，绝不执行
            tool = TOOLS.get(name)
            if tool is None or name not in visible:
                logger.warning("工具错误：未知工具或当前阶段不可见 | 工具=%s", name)
                if on_progress:
                    on_progress({"type": "tool_error", "tool": name,
                                 "error": f"未知工具：{name}"})
                messages.append(_tool_message(
                    call, f"未知工具：{name}。请从可用工具清单中选择。"))
                continue

            args, arg_error = validate_args(call["args"], tool.parameters)
            if arg_error:
                logger.warning(
                    "工具错误：参数校验未通过 | 工具=%s | 错误=%s", name, arg_error)
                if on_progress:
                    on_progress({"type": "tool_error", "tool": name,
                                 "error": arg_error})
                messages.append(_tool_message(
                    call, f"工具 {name} 参数错误：{arg_error}。请修正后重新调用。"))
                continue

            if tool.risk == "write":
                # 确认门：写工具绝不在这里执行——预览 + 存 pending + 循环立即终止
                if on_progress:
                    on_progress({"type": "tool_call", "tool": name,
                                 "args_summary": _args_summary(args)})
                try:
                    preview = tool.preview(args)
                except Exception as exc:  # preview 契约未保证不抛，兜底回喂
                    logger.warning(
                        "工具错误：写工具预览生成失败 | 工具=%s | 错误=%s", name, exc)
                    messages.append(_tool_message(
                        call, f"工具 {name} 预览生成失败：{exc}。"))
                    continue
                action = {
                    "kind": "dispatcher_tool",
                    "tool": name,
                    "args": args,
                    "summary": preview["summary"],
                    "preview_lines": preview["lines"],
                    "warnings": preview.get("warnings", []),
                    "created_at": time.time(),
                    # W5 透传：工厂名对照预扫结果随信封留存，
                    # 刷新恢复确认卡（W3 history 端点裁剪回传）时仍完整
                    "factory_scan": preview.get("factory_scan"),
                }
                session.pending_action = action
                logger.info(
                    "确认门拦截写工具 | 工具=%s | 参数=%s | 预览摘要=%s",
                    name, _log_args_summary(args),
                    str(preview["summary"])[:_LOG_SUMMARY_CAP])
                sessions.record_tool(session, tool=name,
                                     args_summary=_args_summary(args),
                                     result_summary="待人工确认",
                                     confirmed=None)
                # 前端确认卡消息（不含 preview_lines，前端已单独渲染）
                msg_text = action["summary"] + "\n请确认是否执行（确认后生效）。"
                # 一次一确认：同轮其余调用（含写工具）全部忽略，须确认后再发起
                ignored = len(step["tool_calls"]) - step["tool_calls"].index(call) - 1
                if ignored:
                    msg_text += f"\n（本轮其余 {ignored} 个工具调用已忽略，确认后请重新发起。）"
                # 对话历史写入预览详情（LLM 后续轮次能回答追问，如"哪些存疑"）
                history_text = msg_text
                if action["preview_lines"]:
                    history_text = (action["summary"] + "\n"
                                    + "\n".join(action["preview_lines"]) + "\n"
                                    + "请确认是否执行（确认后生效）。"
                                    + (f"\n（本轮其余 {ignored} 个工具调用已忽略，确认后请重新发起。）"
                                       if ignored else ""))
                sessions.record_turn(session, message, history_text)
                if on_progress:
                    on_progress({"type": "pending_confirmation",
                                 "tool": name, "preview": action["preview_lines"],
                                 "message": msg_text, "action": action,
                                 "warnings": action["warnings"],
                                 # W5 透传：工厂名对照预扫结果（preview 可能无此键，.get 防御）
                                 "factory_scan": preview.get("factory_scan")})
                return {"status": "pending_confirmation",
                        "action": action,
                        "preview": action["preview_lines"],
                        "message": msg_text,
                        "warnings": action["warnings"],
                        "factory_scan": preview.get("factory_scan")}

            # 只读工具：直接执行（func 契约内部不抛异常，错误走 {"error": ...}）
            if on_progress:
                on_progress({"type": "tool_call", "tool": name,
                             "args_summary": _args_summary(args)})
            logger.info("工具调用 | 工具=%s | 参数=%s", name, _log_args_summary(args))
            result = tool.func(args)
            if isinstance(result, dict) and result.get("error"):
                logger.warning(
                    "工具错误：执行返回错误 | 工具=%s | 错误=%s",
                    name, str(result.get("error"))[:_LOG_SUMMARY_CAP])
            else:
                logger.info(
                    "工具结果 | 工具=%s | 结果=%s", name, _log_result_summary(result))
            sessions.record_tool(session, tool=name,
                                 args_summary=_args_summary(args),
                                 result_summary=_result_summary(result))
            if on_progress:
                on_progress({"type": "tool_result", "tool": name,
                             "result_summary": _result_summary(result)})
            messages.append(_tool_message(
                call, json.dumps(result, ensure_ascii=False, default=str)[:TOOL_RESULT_CAP]))

    # 超轮兜底：多半是模型反复调工具不收敛，引导操作员拆小问题
    if on_progress:
        on_progress({"type": "final", "message": "处理步骤过多，请把问题拆小一点再问。"})
    return {"status": "ok", "message": "处理步骤过多，请把问题拆小一点再问。"}


# ---------------------------------------------------------------------------
# confirm 执行通道（二期用，本次就写好）
# ---------------------------------------------------------------------------

def execute_confirmed(session: DispatcherSession | None,
                      client_action: dict | None,
                      on_progress: Callable[[dict], None] | None = None) -> dict:
    """人工确认后执行 pending action。

    action 来源优先级：session.pending_action（服务端留存，防客户端伪造）
    > client_action（降级通道，必须是 kind=="dispatcher_tool" 信封）。
    执行前再过 TTL / 工具风险等级 / validate_args 三道防线。
    on_progress（W4a）：节点级进度回调，透传 tool.execute（工具内部包装成
    exec_progress 事件，跑图类工具 create_batch/rerun/submit_review 生效）。
    """
    action = None
    if session is not None and session.pending_action:
        action = session.pending_action
    elif client_action is not None:
        if not isinstance(client_action, dict) \
                or client_action.get("kind") != "dispatcher_tool":
            logger.warning("确认门拒绝执行 | 原因=无效的确认请求（action 非 dispatcher_tool 信封）")
            return {"status": "error",
                    "message": "无效的确认请求：action 必须是 dispatcher_tool 信封"}
        action = client_action
    if action is None:
        logger.info("确认门拒绝执行 | 原因=没有待确认的操作")
        return {"status": "error", "message": "没有待确认的操作"}

    # TTL 防线：陈旧 action 不允许执行（防「上午的预览下午误确认」）
    if time.time() - float(action.get("created_at", 0)) > ACTION_TTL_SEC:
        logger.warning(
            "确认门拒绝执行 | 工具=%s | 原因=TTL 过期（超过 %d 秒，须重新发起）",
            action.get("tool"), ACTION_TTL_SEC)
        if session is not None:
            sessions.clear_pending(session)
        return {"status": "expired", "message": "该操作已超时，请重新发起"}

    # 工具防线：必须仍是已注册的写工具且带 execute（防工具表变更后误执行）
    name = str(action.get("tool") or "")
    tool = TOOLS.get(name)
    if tool is None or tool.risk != "write" or tool.execute is None:
        logger.warning(
            "确认门拒绝执行 | 工具=%s | 原因=不是已注册的写工具", name)
        if session is not None:
            sessions.clear_pending(session)
        return {"status": "error",
                "message": f"操作不可执行：工具 {name} 不是已注册的写工具"}

    # confirm 防线：参数再校验一次（action 可能来自客户端，不可信）
    args, arg_error = validate_args(action.get("args") or {}, tool.parameters)
    if arg_error:
        logger.info(
            "确认门拒绝执行 | 工具=%s | 原因=参数校验未通过：%s", name, arg_error)
        return {"status": "error",
                "message": f"参数校验未通过：{arg_error}，未执行任何操作"}

    result = tool.execute(args, on_progress=on_progress)
    logger.info(
        "确认门已执行写工具 | 工具=%s | 参数=%s | 结果=%s",
        name, _log_args_summary(args), _log_result_summary(result))

    # W2：确定性中文摘要（绝不抛异常）；result 含 error → status="error"
    summary = summarize_applied(name, args, result)
    failed = isinstance(result, dict) and bool(result.get("error"))
    if session is not None:
        # record_tool 的 result_summary 保持 JSON 摘要不变（审计用）
        sessions.record_tool(session, tool=name,
                             args_summary=_args_summary(args),
                             result_summary=_result_summary(result),
                             confirmed=True)
        sessions.clear_pending(session)
        # 对话历史写中文摘要（不再写 300 字 JSON 进 LLM 上下文），限 500 字符
        turn_text = f"已确认并执行 {name}：{summary['message']}"
        if summary["summary_lines"]:
            turn_text += "\n" + "\n".join(summary["summary_lines"])
        sessions.record_turn(session, "[确认执行]", turn_text[:500])

    status = "error" if failed else "applied"
    return {"status": status, "tool": name, "result": result,
            "message": summary["message"],
            "summary_lines": summary["summary_lines"],
            "links": summary["links"],
            # 补 args：修 L2 记忆 auto_update 拿不到 args 的既有 bug
            "args": args}


__all__ = ["llm_step", "run_dispatch", "execute_confirmed",
           "MAX_ROUNDS", "TOOL_RESULT_CAP", "ACTION_TTL_SEC"]
