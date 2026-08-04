# -*- coding: utf-8 -*-
"""OpenAI 兼容客户端（走硅基流动 SiliconFlow 中转）。

配置读取顺序：项目根目录 .env 优先（override=True），其次环境变量。
- SILICONFLOW_API_KEY   缺失时给出清晰报错
- SILICONFLOW_BASE_URL  默认 https://api.siliconflow.cn/v1
- VISION_MODEL          默认 Qwen/Qwen2.5-VL-72B-Instruct
- TEXT_MODEL            默认 Qwen/Qwen2.5-72B-Instruct

可靠性：超时 120s；429/5xx 指数退避重试最多 3 次；每次调用记录 token 用量。

可观测性（L4）：每次调用记 INFO（用途/模型/耗时/tokens/finish_reason），
finish_reason=length（max_tokens 截断，历史"假死"事故指纹）单独 WARNING，
请求消息与响应原文 DEBUG 级各截断到前 500 字符。
铁律：绝不记 API key / Authorization 头；.env 内容不入日志。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, OpenAI, RateLimitError

logger = logging.getLogger(__name__)

# 加载项目根目录的 .env；override=True 表示 .env 中的值会覆盖已存在的同名环境变量。
# 本文件位于 <project>/app/app/extraction/llm_client.py，项目根 = parents[2]，
# 用 Path(__file__) 推导（而非 import app.config），避免循环依赖。
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_VISION_MODEL = "Qwen/Qwen2.5-VL-72B-Instruct"
DEFAULT_TEXT_MODEL = "Qwen/Qwen2.5-72B-Instruct"

REQUEST_TIMEOUT = 120  # 秒
MAX_API_RETRIES = 3  # 429/5xx 时最多重试 3 次

DEBUG_TRUNC_LIMIT = 500  # 请求/响应原文 DEBUG 日志的截断长度


def _truncate(text: str, limit: int = DEBUG_TRUNC_LIMIT) -> str:
    """截断长文本用于 DEBUG 日志，超长时标注已截断及总长度。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[已截断，共 {len(text)} 字符]"


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """脱敏请求消息用于 DEBUG 日志：vision 消息的 base64 图片数据替换为占位符。

    只动 content 里的 image_url 部分；messages 本身不含 API key
    （key 在 client 的 Authorization 头里，本模块任何地方都不入日志）。
    """
    safe: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            safe.append(m)  # 防御：非 dict 消息项原样保留，日志绝不抛异常
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "image_url",
                                  "image_url": "<base64 图片数据已省略>"})
                else:
                    parts.append(part)
            safe.append({**m, "content": parts})
        else:
            safe.append(m)
    return safe


def _response_text(resp) -> str:
    """取响应原文用于 DEBUG 日志：正文 + tool_calls 摘要（如有）。"""
    try:
        msg = resp.choices[0].message
    except (IndexError, AttributeError, TypeError):
        return "<无法解析响应结构>"
    text = msg.content or ""
    if not isinstance(text, str):
        # 防御：list 型 content 强转 str，避免日志处理抛 TypeError 击穿主流程
        text = str(text)
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        names = [getattr(getattr(tc, "function", None), "name", "?")
                 for tc in tool_calls]
        text += f" [tool_calls: {', '.join(names)}]"
    return text


def _finish_reason(resp) -> str:
    """取 finish_reason，缺choices/字段时返回空串。"""
    try:
        return resp.choices[0].finish_reason or ""
    except (IndexError, AttributeError, TypeError):
        return ""


def _call_label(kind: str, source_file: str) -> str:
    """调用点标识：用途（text/vision）+ 来源文件。"""
    return f"kind={kind} source={source_file or '-'}"


def thinking_enabled() -> bool:
    """是否开启思考模式（环境变量 LLM_ENABLE_THINKING，默认 "1" 保持现状）。

    设为 "0" 时在请求体加 enable_thinking=False（OpenAI SDK extra_body），
    已实测端点支持：reasoning_tokens 归零，响应从几十秒降到约 1 秒。
    """
    return os.environ.get("LLM_ENABLE_THINKING", "1").strip() != "0"


def extraction_thinking_enabled() -> bool:
    """提取文本通道是否开启思考模式（EXTRACTION_ENABLE_THINKING，默认 "0" 关闭）。

    提取是照抄任务，无需思考；默认关闭可避免大 JSON 输出被思考拖慢超时。
    调度 Agent 的思考模式仍由全局 LLM_ENABLE_THINKING 控制（默认开启）。
    """
    return os.environ.get("EXTRACTION_ENABLE_THINKING", "0").strip() == "1"


class MissingAPIKeyError(RuntimeError):
    """API key 缺失时的清晰报错。"""


@dataclass
class UsageRecord:
    """单次 LLM 调用的 token 用量记录。"""

    model: str
    kind: str  # "text" | "vision"
    source_file: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_sec: float = 0.0
    success: bool = True
    error: str = ""


@dataclass
class UsageTracker:
    """全局 token 用量收集器（线程安全）。"""

    records: list[UsageRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, rec: UsageRecord) -> None:
        with self._lock:
            self.records.append(rec)

    def summary(self) -> dict:
        with self._lock:
            total_prompt = sum(r.prompt_tokens for r in self.records)
            total_completion = sum(r.completion_tokens for r in self.records)
            return {
                "calls": len(self.records),
                "failed_calls": sum(1 for r in self.records if not r.success),
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_prompt + total_completion,
            }

    def reset(self) -> None:
        with self._lock:
            self.records.clear()


# 模块级全局用量追踪器
usage_tracker = UsageTracker()


def get_settings() -> dict:
    """读取配置；API key 缺失时抛出带操作指引的清晰错误。"""
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        raise MissingAPIKeyError(
            f"未找到 SILICONFLOW_API_KEY。请在 {_ENV_PATH} "
            "中写入 SILICONFLOW_API_KEY=sk-xxx，或设置同名环境变量后重试。"
        )
    text_model = os.environ.get("TEXT_MODEL", DEFAULT_TEXT_MODEL).strip() or DEFAULT_TEXT_MODEL
    return {
        "api_key": key,
        "base_url": os.environ.get("SILICONFLOW_BASE_URL", DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL,
        "vision_model": os.environ.get("VISION_MODEL", DEFAULT_VISION_MODEL).strip()
        or DEFAULT_VISION_MODEL,
        "text_model": text_model,
        # 提取流水线文本通道专用模型：EXTRACTION_TEXT_MODEL 未设置时回退
        # text_model（向后兼容）；调度 Agent 仍走 text_model，互不影响。
        "extraction_text_model": os.environ.get("EXTRACTION_TEXT_MODEL", "").strip()
        or text_model,
    }


def _get_client() -> OpenAI:
    s = get_settings()
    return OpenAI(
        api_key=s["api_key"], base_url=s["base_url"], timeout=REQUEST_TIMEOUT
    )


def _is_retryable(exc: Exception) -> bool:
    """429 或 5xx 才重试。"""
    if isinstance(exc, (RateLimitError, APITimeoutError)):
        return True
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        if status is not None and (status == 429 or 500 <= status < 600):
            return True
    return False


def _create_with_retry(kwargs: dict, model: str, kind: str,
                       source_file: str):
    """带重试与用量记录的 chat.completions.create 公共内部函数。

    - 429/5xx 指数退避重试最多 MAX_API_RETRIES 次；
    - 每次调用（含失败）记录 token 用量到 usage_tracker；
    - 每次调用记 INFO（模型/耗时/tokens/finish_reason），
      finish_reason=length 单独 WARNING（历史"假死"事故指纹），
      请求消息与响应原文 DEBUG 级各截断 500 字符（图片 base64 省略）；
    - 成功返回原始 ChatCompletion 响应对象（由调用方取 content / tool_calls）。
    """
    client = _get_client()
    label = _call_label(kind, source_file)
    last_exc: Exception | None = None
    logger.debug(
        "LLM 请求 | %s | model=%s | messages=%s",
        label, model,
        _truncate(json.dumps(
            _sanitize_messages(kwargs.get("messages", [])),
            ensure_ascii=False, default=str)),
    )
    for attempt in range(MAX_API_RETRIES + 1):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**kwargs)
            elapsed = time.time() - t0
            usage = getattr(resp, "usage", None)
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            finish_reason = _finish_reason(resp)
            usage_tracker.add(
                UsageRecord(
                    model=model,
                    kind=kind,
                    source_file=source_file,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    elapsed_sec=round(elapsed, 2),
                    success=True,
                )
            )
            logger.info(
                "LLM 调用完成 | %s | model=%s | 耗时=%.2fs | "
                "tokens prompt=%d completion=%d total=%d | finish_reason=%s",
                label, model, elapsed,
                prompt_tokens, completion_tokens, total_tokens,
                finish_reason or "unknown",
            )
            if finish_reason == "length":
                # max_tokens 截断：输出 JSON 大概率不完整，后续解析失败重试叠加
                # 曾导致正达批次 30 分钟"假死"——这是事故指纹，必须单独 WARNING
                logger.warning(
                    "LLM 输出被 max_tokens 截断（finish_reason=length）| %s | "
                    "model=%s | completion_tokens=%d | 输出 JSON 可能不完整",
                    label, model, completion_tokens,
                )
            logger.debug(
                "LLM 响应原文 | %s | model=%s | %s",
                label, model, _truncate(_response_text(resp)),
            )
            return resp
        # APITimeoutError 是 APIConnectionError（→APIError）的子类，
        # 必须放在 except APIError 之前，否则超时永远落不到专属分支
        except APITimeoutError as e:
            last_exc = e
            usage_tracker.add(
                UsageRecord(
                    model=model,
                    kind=kind,
                    source_file=source_file,
                    success=False,
                    error=f"APITimeoutError: {str(e)[:200]}",
                )
            )
            if attempt < MAX_API_RETRIES:
                backoff = (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "LLM 调用超时，第 %d/%d 次重试 | %s | model=%s | "
                    "%.1fs 后重试 | %s",
                    attempt + 1, MAX_API_RETRIES, label, model, backoff,
                    str(e)[:200],
                )
                time.sleep(backoff)
                continue
            logger.exception(
                "LLM 调用超时最终失败（重试耗尽）| %s | model=%s",
                label, model,
            )
            raise
        except APIError as e:
            # 部分模型不支持 response_format，降级一次后按正常重试流程走
            if "response_format" in kwargs and "response_format" in str(e).lower():
                kwargs.pop("response_format", None)
                logger.info(
                    "模型不支持 response_format，降级为普通输出重试 | %s | model=%s",
                    label, model,
                )
                continue
            last_exc = e
            elapsed = time.time() - t0
            retryable = _is_retryable(e) and attempt < MAX_API_RETRIES
            usage_tracker.add(
                UsageRecord(
                    model=model,
                    kind=kind,
                    source_file=source_file,
                    elapsed_sec=round(elapsed, 2),
                    success=False,
                    error=f"{type(e).__name__}: {str(e)[:200]}",
                )
            )
            if retryable:
                # 指数退避：1s, 2s, 4s + 抖动
                backoff = (2**attempt) + random.uniform(0, 0.5)
                logger.warning(
                    "LLM 调用失败，第 %d/%d 次重试 | %s | model=%s | "
                    "%.1fs 后重试 | %s: %s",
                    attempt + 1, MAX_API_RETRIES, label, model, backoff,
                    type(e).__name__, str(e)[:200],
                )
                time.sleep(backoff)
                continue
            logger.exception(
                "LLM 调用最终失败（重试耗尽或不可重试）| %s | model=%s",
                label, model,
            )
            raise
    assert last_exc is not None
    raise last_exc


def _base_kwargs(messages: list[dict], model: str, temperature: float,
                 max_tokens: int, thinking: bool | None = None) -> dict:
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if thinking is None:
        # 未显式指定时维持现状：读全局 LLM_ENABLE_THINKING（默认开）
        thinking = thinking_enabled()
    if not thinking:
        # 关闭思考模式：reasoning_tokens 归零，响应大幅提速
        kwargs["extra_body"] = {"enable_thinking": False}
    return kwargs


def chat_completion(
    messages: list[dict],
    *,
    vision: bool = False,
    source_file: str = "",
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 16384,
    json_mode: bool = True,
) -> str:
    """调用聊天补全接口，返回原始文本。

    - vision=True 时使用视觉模型（messages 中可含 image_url 内容）；
    - 429/5xx 指数退避重试最多 3 次；
    - token 用量记入 usage_tracker。
    """
    s = get_settings()
    use_model = model or (s["vision_model"] if vision else s["text_model"])
    kind = "vision" if vision else "text"

    kwargs = _base_kwargs(messages, use_model, temperature, max_tokens)
    if json_mode:
        # 硅基流动 Qwen 系列支持强制 JSON 输出；若模型不支持会在 API 层报错，
        # 此时降级为普通输出（由 _create_with_retry 摘掉 response_format 重试）。
        kwargs["response_format"] = {"type": "json_object"}

    resp = _create_with_retry(kwargs, use_model, kind, source_file)
    return resp.choices[0].message.content or ""


def extraction_chat_completion(
    messages: list[dict],
    *,
    vision: bool = False,
    source_file: str = "",
    temperature: float = 0.0,
    max_tokens: int = 16384,
    json_mode: bool = True,
) -> str:
    """提取流水线专用聊天补全，返回原始文本。

    与 chat_completion 的唯一区别：vision=False 时模型取自
    get_settings()["extraction_text_model"]（EXTRACTION_TEXT_MODEL，
    未设置时回退 TEXT_MODEL），使提取文本通道与调度 Agent 的
    TEXT_MODEL 解耦。vision=True 时直接委托 chat_completion，
    视觉路径完全不变。json_mode/重试/用量记录与 chat_completion 一致。
    """
    if vision:
        return chat_completion(
            messages,
            vision=True,
            source_file=source_file,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    s = get_settings()
    use_model = s["extraction_text_model"]

    # 提取默认关闭思考（EXTRACTION_ENABLE_THINKING 默认 "0"）：
    # 照抄任务无需思考，避免思考模式拖慢大 JSON 输出导致超时。
    kwargs = _base_kwargs(messages, use_model, temperature, max_tokens,
                          thinking=extraction_thinking_enabled())
    if json_mode:
        # 与 chat_completion 相同：不支持 response_format 的模型由
        # _create_with_retry 摘掉该参数降级重试。
        kwargs["response_format"] = {"type": "json_object"}

    resp = _create_with_retry(kwargs, use_model, "text", source_file)
    return resp.choices[0].message.content or ""


def chat_completion_with_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    source_file: str = "dispatcher",
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int = 4096,
) -> dict:
    """OpenAI 原生 tool-calling 调用，返回 {"content", "tool_calls"}。

    供调度 Agent 使用（llm_client 主通道只服务提取流水线，签名不动）：
    - tools 为 OpenAI 格式的工具定义列表；与 response_format 互斥，本函数不设；
    - tool_calls 中 arguments 为 JSON 字符串，由调用方解析并校验；
    - 重试/退避/用量记录与 chat_completion 共用 _create_with_retry。
    """
    s = get_settings()
    use_model = model or s["text_model"]

    kwargs = _base_kwargs(messages, use_model, temperature, max_tokens)
    kwargs["tools"] = tools

    resp = _create_with_retry(kwargs, use_model, "text", source_file)
    msg = resp.choices[0].message
    tool_calls = [
        {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": tc.function.arguments or "",
        }
        for tc in (msg.tool_calls or [])
    ]
    return {"content": msg.content, "tool_calls": tool_calls}
