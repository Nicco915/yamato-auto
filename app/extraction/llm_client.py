# -*- coding: utf-8 -*-
"""OpenAI 兼容客户端（走硅基流动 SiliconFlow 中转）。

配置读取顺序：项目根目录 .env 优先（override=True），其次环境变量。
- SILICONFLOW_API_KEY   缺失时给出清晰报错
- SILICONFLOW_BASE_URL  默认 https://api.siliconflow.cn/v1
- VISION_MODEL          默认 Qwen/Qwen2.5-VL-72B-Instruct
- TEXT_MODEL            默认 Qwen/Qwen2.5-72B-Instruct

可靠性：超时 120s；429/5xx 指数退避重试最多 3 次；每次调用记录 token 用量。
"""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, OpenAI, RateLimitError

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


def thinking_enabled() -> bool:
    """是否开启思考模式（环境变量 LLM_ENABLE_THINKING，默认 "1" 保持现状）。

    设为 "0" 时在请求体加 enable_thinking=False（OpenAI SDK extra_body），
    已实测端点支持：reasoning_tokens 归零，响应从几十秒降到约 1 秒。
    """
    return os.environ.get("LLM_ENABLE_THINKING", "1").strip() != "0"


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
    return {
        "api_key": key,
        "base_url": os.environ.get("SILICONFLOW_BASE_URL", DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL,
        "vision_model": os.environ.get("VISION_MODEL", DEFAULT_VISION_MODEL).strip()
        or DEFAULT_VISION_MODEL,
        "text_model": os.environ.get("TEXT_MODEL", DEFAULT_TEXT_MODEL).strip()
        or DEFAULT_TEXT_MODEL,
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
    client = _get_client()
    use_model = model or (s["vision_model"] if vision else s["text_model"])
    kind = "vision" if vision else "text"

    kwargs: dict = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # 硅基流动 Qwen 系列支持强制 JSON 输出；若模型不支持会在 API 层报错，
        # 此时降级为普通输出（由调用方做 JSON 解析重试）。
        kwargs["response_format"] = {"type": "json_object"}
    if not thinking_enabled():
        # 关闭思考模式：reasoning_tokens 归零，响应大幅提速
        kwargs["extra_body"] = {"enable_thinking": False}

    last_exc: Exception | None = None
    for attempt in range(MAX_API_RETRIES + 1):
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**kwargs)
            elapsed = time.time() - t0
            usage = getattr(resp, "usage", None)
            usage_tracker.add(
                UsageRecord(
                    model=use_model,
                    kind=kind,
                    source_file=source_file,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    elapsed_sec=round(elapsed, 2),
                    success=True,
                )
            )
            return resp.choices[0].message.content or ""
        except APIError as e:
            # 部分模型不支持 response_format，降级一次后按正常重试流程走
            if json_mode and "response_format" in str(e).lower():
                kwargs.pop("response_format", None)
                json_mode = False
                continue
            last_exc = e
            elapsed = time.time() - t0
            retryable = _is_retryable(e) and attempt < MAX_API_RETRIES
            usage_tracker.add(
                UsageRecord(
                    model=use_model,
                    kind=kind,
                    source_file=source_file,
                    elapsed_sec=round(elapsed, 2),
                    success=False,
                    error=f"{type(e).__name__}: {str(e)[:200]}",
                )
            )
            if retryable:
                # 指数退避：1s, 2s, 4s + 抖动
                time.sleep((2**attempt) + random.uniform(0, 0.5))
                continue
            raise
        except APITimeoutError as e:
            last_exc = e
            usage_tracker.add(
                UsageRecord(
                    model=use_model,
                    kind=kind,
                    source_file=source_file,
                    success=False,
                    error=f"APITimeoutError: {str(e)[:200]}",
                )
            )
            if attempt < MAX_API_RETRIES:
                time.sleep((2**attempt) + random.uniform(0, 0.5))
                continue
            raise
    assert last_exc is not None
    raise last_exc
