"""调度 Agent 的 LangChain BaseChatModel 薄适配器（create_react_agent 专用）。

设计要点：

1. 薄适配：只把 langgraph.prebuilt.create_react_agent 需要的 BaseChatModel
   接口（bind_tools / _generate）包在既有 llm_client.chat_completion_with_tools
   （OpenAI 兼容端点，qwen 模型）之上，不引入 langchain-openai 等厂商包，
   重试/退避/用量记录全部复用 llm_client 的 _create_with_retry。

2. 观测设施原样保留：on_progress 事件（llm_thinking，回调异常吞掉）、
   debug_log 的 llm_request / llm_response（mode 固定 "native-lc"），
   与旧 loop.py 双适配器循环同一套排错口径，迁移后排错先翻 dispatcher.log
   的习惯不变。

3. mock 剧本（测试关键口，格式与旧 loop.py 的 _MOCK_SCRIPT 完全一致）：
   DISPATCHER_MOCK=1 时 _generate 从模块级 _SCRIPT 弹剧本直接返回，
   不碰网络、不需要 API key；llm_client 延迟 import 同理（无 key 环境
   下 import 本模块仍可跑测试）。

4. 参数解析失败不丢：arguments JSON 坏了换成 {"__parse_error__": 原文前 500
   字符} 作为 args，ToolNode 校验失败会把错误回喂模型，让模型自我修正——
   与旧循环的 "__parse_error__" 哨兵同一思路，只是借 ToolNode 完成回喂。

仅同步调用：应用是 FastAPI 同步接口 + 线程池，不实现 async / streaming。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from app.dispatcher import debug_log

logger = logging.getLogger(__name__)

# DISPATCHER_MOCK=1 时的确定性剧本：条目 {"final_text": str | None,
# "tool_calls": [{"name", "args": dict}]}（允许只写一个 key，弹剧本时归一化），
# 格式与旧 loop.py 的 _MOCK_SCRIPT 完全一致，测试侧直接 append 或 set_script。
_SCRIPT: list[dict] = []


def set_script(items: list[dict]) -> None:
    """整体替换 mock 剧本（测试入口）。"""
    _SCRIPT.clear()
    _SCRIPT.extend(items)


def clear_script() -> None:
    """清空 mock 剧本（测试收尾用，防用例间串味）。"""
    _SCRIPT.clear()


def _script_to_message(item: dict) -> AIMessage:
    """把一条剧本归一化成 AIMessage（tool_calls 自动生成 mockcall_N id）。"""
    calls = []
    for i, c in enumerate(item.get("tool_calls") or []):
        calls.append({"id": f"mockcall_{i}",
                      "name": str(c.get("name") or ""),
                      "args": c.get("args") if isinstance(c.get("args"), dict) else {}})
    if calls:
        return AIMessage(content="", tool_calls=calls)
    return AIMessage(content=item.get("final_text") or "")


class QwenDispatcherChatModel(BaseChatModel):
    """包装 llm_client.chat_completion_with_tools 的薄 BaseChatModel。

    供 langgraph.prebuilt.create_react_agent 使用：bind_tools 转换并留存
    OpenAI 格式工具定义，_generate 做 LangChain 消息 ↔ OpenAI 消息互转，
    观测（on_progress / debug_log）与旧调度循环保持同一口径。
    """

    # pydantic 字段：随模型实例与 model_copy 拷贝传递
    on_progress: Callable[[dict], None] | None = None
    session_id: str | None = None

    # 运行态（不进序列化）：bind_tools 存副本、_generate 计轮次
    _bound_tools: list[dict] = PrivateAttr(default_factory=list)
    _round: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "qwen-dispatcher"

    def bind_tools(self, tools, **kwargs):
        """转换工具定义为 OpenAI function 格式后存副本，返回自身浅拷贝。

        create_react_agent 约定 bind_tools 返回绑好工具的新 Runnable；
        这里用 model_copy 浅拷贝（on_progress / session_id 随拷贝传递），
        _bound_tools 只设置在拷贝上，不污染原实例。
        """
        converted = [convert_to_openai_tool(t) for t in tools]
        clone = self.model_copy()
        clone._bound_tools = converted
        return clone

    # ------------------------------------------------------------------
    # 消息互转
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_messages(messages: list[BaseMessage]) -> list[dict]:
        """LangChain 消息列表 → OpenAI 兼容端点消息列表。"""
        out: list[dict] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                out.append({"role": "system", "content": m.content})
            elif isinstance(m, HumanMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AIMessage):
                msg: dict = {"role": "assistant",
                             # 有 tool_calls 时 content 可为 None（OpenAI 惯例）
                             "content": m.content if m.content else None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"],
                                      "arguments": json.dumps(
                                          tc.get("args") or {},
                                          ensure_ascii=False, default=str)}}
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            elif isinstance(m, ToolMessage):
                out.append({"role": "tool",
                            "tool_call_id": m.tool_call_id,
                            "content": m.content})
            else:
                # 兜底：其余消息类型按 user 文本回喂，不丢上下文
                out.append({"role": "user", "content": str(m.content)})
        return out

    @staticmethod
    def _from_response(resp: dict) -> AIMessage:
        """chat_completion_with_tools 返回 → AIMessage。

        arguments JSON 解析失败不丢：args 换成 {"__parse_error__": 原文前 500
        字符} 哨兵，ToolNode 校验失败后把错误回喂模型自我修正。
        """
        calls = []
        for tc in resp.get("tool_calls") or []:
            raw = tc.get("arguments") or ""
            try:
                args = json.loads(raw) if raw else {}
                if not isinstance(args, dict):
                    raise ValueError("arguments 不是 JSON 对象")
            except (json.JSONDecodeError, ValueError):
                args = {"__parse_error__": raw[:500]}
            calls.append({"id": tc["id"], "name": tc["name"], "args": args})
        if calls:
            return AIMessage(content=resp.get("content") or "", tool_calls=calls)
        return AIMessage(content=resp.get("content") or "")

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------

    def _generate(self, messages: list[BaseMessage], stop=None,
                  run_manager=None, **kwargs) -> ChatResult:
        """同步生成一步：mock 剧本 → on_progress → 转换 → 调 llm_client → 回包。"""
        # mock 分支：DISPATCHER_MOCK=1 时弹剧本，不碰网络（确定性测试关键口）
        if os.environ.get("DISPATCHER_MOCK") == "1":
            if not _SCRIPT:
                ai = AIMessage(content="[mock] 剧本已用尽")
            else:
                ai = _script_to_message(_SCRIPT.pop(0))
            return ChatResult(generations=[ChatGeneration(message=ai)])

        self._round += 1
        if self.on_progress:
            try:
                self.on_progress({"type": "llm_thinking", "round": self._round})
            except Exception:  # noqa: BLE001 进度回调绝不弄挂 LLM 调用
                pass

        oai_messages = self._to_openai_messages(messages)
        debug_log.log_llm_request(session_id=self.session_id,
                                  round_no=self._round, mode="native-lc",
                                  messages=oai_messages)

        from app.extraction import llm_client  # 延迟 import：无 API key 时 mock 仍可用
        resp = llm_client.chat_completion_with_tools(
            oai_messages,
            tools=self._bound_tools or None,
            source_file="dispatcher",
        )

        ai = self._from_response(resp)
        # 与旧循环同一格式记响应：{"final_text", "tool_calls"}（tool_calls
        # 带解析后的完整参数，便于事后排查模型到底想调什么）
        debug_log.log_llm_response(
            session_id=self.session_id, round_no=self._round,
            step={"final_text": None if ai.tool_calls else ai.content,
                  "tool_calls": [{"id": tc["id"], "name": tc["name"],
                                  "args": tc.get("args") or {}}
                                 for tc in ai.tool_calls]})
        return ChatResult(generations=[ChatGeneration(message=ai)])


__all__ = ["QwenDispatcherChatModel", "set_script", "clear_script"]
