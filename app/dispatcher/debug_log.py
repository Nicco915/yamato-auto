"""调度 Agent 专用调试日志：独立 JSONL 文件，不上控制台、不进全局 app.log。

用途：事后排查"模型到底看到了什么、返回了什么、工具参数带了什么"。
每次 LLM 调用记录完整 messages（即模型实际看到的 prompt），每个工具
调用记录解析后的完整参数与结果，确认门拦截/执行各记一条。

设计要点：
- 独立 logger（dispatcher_debug），propagate=False——不进 root 的控制台
  handler，也不进 app.log/error.log，只写本模块自己的滚动文件；
- JSONL（一行一条 JSON）：时间戳 + event 类型 + 事件字段，便于
  grep / jq 按 session、事件类型回放；
- 序列化绝不抛异常：所有字段 default=str，整体失败静默丢弃（日志
  不能反过来弄挂调度循环）；
- 敏感参数键（api_key/token/secret/password 类）脱敏为 ***；
- 超长字段截断保护滚动配额（prompt 100k 字符上限，工具结果 20k）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 与 logging_config.py 同一目录约定（app/data/logs/），不 import app.config
_LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
_LOG_FILE = _LOG_DIR / "dispatcher.log"

# 调度日志单行可能很长（完整 prompt）：单文件 20MB，保留 5 个备份
_MAX_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 5

# 字段截断上限（防爆滚动配额）
_PROMPT_CAP = 100_000   # llm_request 的 messages 序列化上限
_RESULT_CAP = 20_000    # tool_result / confirm_execute 的 result 序列化上限

# 敏感参数键：命中即脱敏（与 executor.py 的 _SENSITIVE_KEY_PARTS 保持同一口径）
_SENSITIVE_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password", "passwd")

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    """惰性初始化独立 logger（幂等）。propagate=False 是关键防线。"""
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger("dispatcher_debug")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False  # 不上抛 root：不进控制台 / app.log / error.log
    if not lg.handlers:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        # JSONL 自带时间戳，handler 不再重复格式化
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
    _logger = lg
    return lg


def _truncate(text: str, cap: int) -> str:
    """超长截断并标注，保护滚动配额。"""
    if len(text) <= cap:
        return text
    return text[:cap] + f"...<截断，原长 {len(text)} 字符>"


def _json_safe(value, cap: int | None = None):
    """把任意值转成 JSON 可序列化形态；cap 提供时转成截断字符串。"""
    if cap is not None:
        try:
            return _truncate(
                json.dumps(value, ensure_ascii=False, default=str), cap)
        except Exception:  # noqa: BLE001 日志绝不抛异常
            return "<序列化失败>"
    try:
        # round-trip：把不可序列化的子对象经 default=str 落地
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return str(value)


def _redact(value):
    """递归脱敏：dict 中敏感键值替换为 ***（拷贝，不改原对象）。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(part in str(k).lower() for part in _SENSITIVE_KEY_PARTS):
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def log_event(event: str, **fields) -> None:
    """写一条调度调试事件（JSONL）。任何失败静默丢弃。

    event 取值（按调度循环时序）：
    - user_message：收到操作员消息（session_id / phase / message）
    - llm_request：LLM 调用前（round / mode / 完整 messages）
    - llm_response：LLM 返回（round / final_text / tool_calls 全参数）
    - tool_result：只读工具执行完成（name / args / result）
    - confirm_gate：确认门拦截写工具（name / args / 预览摘要）
    - confirm_execute：人工确认后执行（name / args / result / status）
    """
    try:
        record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
        }
        record.update({k: _json_safe(v) for k, v in fields.items()})
        _get_logger().debug(
            json.dumps(record, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 日志绝不抛异常
        pass


def log_llm_request(*, session_id: str | None, round_no: int, mode: str,
                    messages: list[dict]) -> None:
    """LLM 调用前记录：模型实际看到的完整 prompt（截断至 100k）。"""
    log_event("llm_request", session_id=session_id, round=round_no, mode=mode,
              messages=_json_safe(messages, cap=_PROMPT_CAP))


def log_llm_response(*, session_id: str | None, round_no: int,
                     step: dict) -> None:
    """LLM 返回记录：final_text 或 tool_calls（含解析后的完整参数）。"""
    log_event("llm_response", session_id=session_id, round=round_no,
              final_text=step.get("final_text"),
              tool_calls=_redact(step.get("tool_calls") or []))


def log_tool_result(*, session_id: str | None, name: str, args: dict,
                    result) -> None:
    """只读工具执行记录：完整参数（脱敏）+ 结果（截断至 20k）。"""
    log_event("tool_result", session_id=session_id, tool=name,
              args=_redact(args), result=_json_safe(result, cap=_RESULT_CAP))


def log_confirm_gate(*, session_id: str | None, name: str, args: dict,
                     summary: str) -> None:
    """确认门拦截记录：写工具被拦下时的参数与预览摘要。"""
    log_event("confirm_gate", session_id=session_id, tool=name,
              args=_redact(args), summary=summary)


def log_confirm_execute(*, session_id: str | None, name: str, args: dict,
                        result, status: str) -> None:
    """确认执行记录：人工确认后写工具的真实参数与结果。"""
    log_event("confirm_execute", session_id=session_id, tool=name,
              args=_redact(args), status=status,
              result=_json_safe(result, cap=_RESULT_CAP))
