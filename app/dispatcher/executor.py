# -*- coding: utf-8 -*-
"""调度 Agent 的确认门执行器（两引擎共用时即已独立，react 唯一化后单独成模块）。

职责：人工确认后执行 pending action。与引擎无关——react 引擎产出的
pending_action 与客户端降级通道的 action 都走这里的同一套防线：

1. action 来源优先级：session.pending_action（服务端留存，防客户端伪造）
   > client_action（降级通道，必须是 kind=="dispatcher_tool" 信封）；
2. TTL 防线：陈旧 action 不允许执行（防「上午的预览下午误确认」）；
3. 工具防线：必须仍是已注册的写工具且带 execute（防工具表变更后误执行）；
4. confirm 防线：参数再过一次 validate_args（action 可能来自客户端，不可信）。

on_progress：节点级进度回调，透传 tool.execute（工具内部包装成
exec_progress 事件，跑图类工具 create_batch/rerun/submit_review 生效）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from app.dispatcher import debug_log, sessions
from app.dispatcher.sessions import DispatcherSession
from app.dispatcher.summarize import summarize_applied
from app.dispatcher.tools import TOOLS, validate_args

logger = logging.getLogger(__name__)

ACTION_TTL_SEC = 30 * 60  # pending action 有效期：超时须重新发起（防陈旧确认）


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


def execute_confirmed(session: DispatcherSession | None,
                      client_action: dict | None,
                      on_progress: Callable[[dict], None] | None = None,
                      session_id: str | None = None) -> dict:
    """人工确认后执行 pending action。

    action 来源优先级：session.pending_action（服务端留存，防客户端伪造）
    > client_action（降级通道，必须是 kind=="dispatcher_tool" 信封）。
    执行前再过 TTL / 工具风险等级 / validate_args 三道防线。
    on_progress：节点级进度回调，透传 tool.execute（工具内部包装成
    exec_progress 事件，跑图类工具 create_batch/rerun/submit_review 生效）。
    """
    action = None
    if session is not None and session.pending_action:
        action = session.pending_action
    elif client_action is not None:
        if not isinstance(client_action, dict) \
                or client_action.get("kind") != "dispatcher_tool":
            logger.warning("确认门拒绝执行 | 原因=无效的确认请求（action 非 dispatcher_tool 信封）")
            debug_log.log_event("confirm_rejected", session_id=session_id,
                                reason="无效的确认请求（action 非 dispatcher_tool 信封）")
            return {"status": "error",
                    "message": "无效的确认请求：action 必须是 dispatcher_tool 信封"}
        action = client_action
    if action is None:
        logger.info("确认门拒绝执行 | 原因=没有待确认的操作")
        debug_log.log_event("confirm_rejected", session_id=session_id,
                            reason="没有待确认的操作")
        return {"status": "error", "message": "没有待确认的操作"}

    # TTL 防线：陈旧 action 不允许执行（防「上午的预览下午误确认」）
    if time.time() - float(action.get("created_at", 0)) > ACTION_TTL_SEC:
        logger.warning(
            "确认门拒绝执行 | 工具=%s | 原因=TTL 过期（超过 %d 秒，须重新发起）",
            action.get("tool"), ACTION_TTL_SEC)
        debug_log.log_event("confirm_rejected", session_id=session_id,
                            tool=action.get("tool"),
                            reason=f"TTL 过期（超过 {ACTION_TTL_SEC} 秒）")
        if session is not None:
            sessions.clear_pending(session)
        return {"status": "expired", "message": "该操作已超时，请重新发起"}

    # 工具防线：必须仍是已注册的写工具且带 execute（防工具表变更后误执行）
    name = str(action.get("tool") or "")
    tool = TOOLS.get(name)
    if tool is None or tool.risk != "write" or tool.execute is None:
        logger.warning(
            "确认门拒绝执行 | 工具=%s | 原因=不是已注册的写工具", name)
        debug_log.log_event("confirm_rejected", session_id=session_id,
                            tool=name, reason="不是已注册的写工具")
        if session is not None:
            sessions.clear_pending(session)
        return {"status": "error",
                "message": f"操作不可执行：工具 {name} 不是已注册的写工具"}

    # confirm 防线：参数再校验一次（action 可能来自客户端，不可信）
    args, arg_error = validate_args(action.get("args") or {}, tool.parameters)
    if arg_error:
        logger.info(
            "确认门拒绝执行 | 工具=%s | 原因=参数校验未通过：%s", name, arg_error)
        debug_log.log_event("confirm_rejected", session_id=session_id,
                            tool=name, reason=f"参数校验未通过：{arg_error}")
        return {"status": "error",
                "message": f"参数校验未通过：{arg_error}，未执行任何操作"}

    result = tool.execute(args, on_progress=on_progress)
    logger.info(
        "确认门已执行写工具 | 工具=%s | 参数=%s | 结果=%s",
        name, _log_args_summary(args), _log_result_summary(result))
    debug_log.log_confirm_execute(
        session_id=session_id, name=name, args=args, result=result,
        status=("error" if isinstance(result, dict) and result.get("error")
                else "applied"))

    # 确定性中文摘要（绝不抛异常）；result 含 error → status="error"
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


__all__ = ["execute_confirmed", "ACTION_TTL_SEC"]
