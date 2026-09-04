# -*- coding: utf-8 -*-
"""LLM 动态指标与 reasoning 捕获测试。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_llm_metrics.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.extraction import llm_client  # noqa: E402
from app.extraction.llm_client import context_percent  # noqa: E402


def test_context_percent_qwen():
    """qwen 系列模型按 128K 窗口计算百分比。"""
    assert context_percent("Qwen/Qwen3.7-plus", 65536) == 0.5
    assert context_percent("qwen2.5-72b", 131072) == 1.0


def test_context_percent_unknown():
    """未知模型返回 0.0，避免误导。"""
    assert context_percent("gpt-4", 65536) == 0.0
    assert context_percent("", 100) == 0.0


def test_chat_completion_with_tools_returns_reasoning_and_usage(monkeypatch):
    """chat_completion_with_tools 在响应中带 reasoning_content 与 usage 时正确返回。"""

    def _fake_create_with_retry(kwargs, model, kind, source_file):
        msg = SimpleNamespace(
            content="hello",
            reasoning_content="step1\nstep2",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    function=SimpleNamespace(
                        name="go_to_page",
                        arguments='{"page":"review","thread_id":"T1"}',
                    ),
                )
            ],
        )
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    monkeypatch.setattr(llm_client, "_create_with_retry", _fake_create_with_retry)

    result = llm_client.chat_completion_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        source_file="test",
    )

    assert result["content"] == "hello"
    assert result["reasoning_content"] == "step1\nstep2"
    assert result["usage"]["prompt_tokens"] == 100
    assert result["usage"]["completion_tokens"] == 50
    assert result["usage"]["total_tokens"] == 150
    assert result["usage"]["context_percent"] == context_percent(
        "Qwen/Qwen2.5-72B-Instruct", 150
    )
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "go_to_page"


def test_chat_completion_with_tools_missing_fields(monkeypatch):
    """响应缺少 reasoning_content / usage 时返回安全缺省值。"""

    def _fake_create_with_retry(kwargs, model, kind, source_file):
        msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    monkeypatch.setattr(llm_client, "_create_with_retry", _fake_create_with_retry)

    result = llm_client.chat_completion_with_tools(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        source_file="test",
    )

    assert result["content"] == "ok"
    assert result["reasoning_content"] == ""
    assert result["usage"]["total_tokens"] == 0
    assert result["usage"]["context_percent"] == 0.0
    assert result["tool_calls"] == []


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
