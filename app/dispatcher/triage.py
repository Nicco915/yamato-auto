"""调度 Agent 的 Triage 分诊层（重构：分诊 → 路由 → 执行）。

设计意图：把「听懂操作员想干什么」从执行循环里剥离出来——每条操作员
消息先经过本模块一次零样本 LLM 调用（json_mode，无工具、无业务决策），
分类为 qa / action / clarify 并提取粗粒度槽位参数，再由代码侧路由：
qa 走知识库、action 进 Executor（loop）、clarify 直接反问用户。

可靠性铁律：这是每条消息的必经之路，任何失败（LLM 异常、JSON 坏、
契约不符、mock 剧本为空、开关关闭）一律返回 None，降级到旧执行循环
（run_dispatch 无 triage_hint 的原路径），绝不抛异常、绝不硬失败。

其他设计要点：
- mock 剧本（_TRIAGE_MOCK_SCRIPT）是确定性测试的关键口：DISPATCHER_MOCK=1
  时测试侧直接 append dict，run_triage 逐个弹出；剧本为空时返回 None
  降级旧循环——这是全部既有 dispatcher 测试零改动的关键；
- L2 操作记忆只作背景上下文注入 prompt（帮分诊器理解「再跑一次」「那个
  批次」这类指代），绝不参与槽位填充——槽位只来自本轮消息与 session 里
  已确认的槽位，防止旧记忆污染新意图；
- L1 槽位（current_target_tool / current_slots）以【进行中的任务】形式
  注入 prompt（帮分诊器理解「是的」「对」这类针对进行中任务的短答），
  但槽位合并仍由代码侧 merge_slots 完成，模型只输出本轮新提取参数；
- 分诊层不碰 items / alias_decisions / skip_processed——它们由 executor
  多轮协商产生，prompt 侧已明确禁止模型提取，本层 REQUIRED_SLOTS 也不列。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.dispatcher import prompts, sessions
from app.dispatcher.sessions import DispatcherSession

logger = logging.getLogger(__name__)


class TriageResult(BaseModel):
    """分诊输出契约（与 prompts._TRIAGE_PROMPT 中的 JSON 契约一致）。

    - intent：qa（操作指导问答）/ action（查数据或发起操作）/ clarify（反问）；
    - target_tool：action 时的目标工具名（取自可见工具清单），其余为 None；
    - extracted_args：粗粒度槽位白名单内的参数（thread_id / factory_filter /
      factory / approved / paths / status_filter），提取不到不写；
    - reply_message：clarify 时给操作员的一句中文反问；
    - confidence：0.0–1.0，模型自评，代码侧只 clamp 不信任。
    """

    intent: Literal["qa", "action", "clarify"]
    target_tool: Optional[str] = None
    extracted_args: dict[str, Any] = Field(default_factory=dict)
    reply_message: Optional[str] = None
    confidence: float = 0.0


# DISPATCHER_MOCK=1 时的确定性剧本：元素为 TriageResult 的 dict 形态，
# 测试侧直接 append；剧本为空时 run_triage 返回 None（降级旧循环——
# 这是全部既有 dispatcher 测试零改动的关键）
_TRIAGE_MOCK_SCRIPT: list[dict] = []

# 各写工具的必填槽位（粗粒度）：items/alias_decisions/skip_processed
# 刻意不在内——它们由 executor 多轮协商产生，分诊层不碰
REQUIRED_SLOTS: dict[str, list[str]] = {
    "create_batch": ["thread_id"],
    "rerun": ["thread_id"],
    "submit_review": ["thread_id"],
    "set_paths": ["paths"],
    "curate_kb": [],
}

# 日志脱敏：用户消息只记前 100 字符（排错定位用，不落全文）
_MSG_LOG_LIMIT = 100


def triage_enabled() -> bool:
    """分诊开关：DISPATCHER_TRIAGE=off 时整体关闭（降级旧循环，零行为变化）。"""
    return os.environ.get("DISPATCHER_TRIAGE", "").strip().lower() != "off"


def run_triage(message: str, session: DispatcherSession, *, phase: int = 2,
               session_id: str | None = None,
               on_progress=None) -> TriageResult | None:
    """执行一次分诊，返回 TriageResult；任何失败返回 None（降级旧循环）。

    流程：mock 剧本 → 开关 → 组装 messages（triage_prompt + L2 上下文 +
    L1 槽位上下文 slots_context + 最近对话历史）→ chat_completion(json_mode)
    → Pydantic 校验（失败重试一次，再失败 None）→ Python 侧后校验 →
    triage 进度事件。绝不抛异常。
    """
    # 1. 总开关：off 时零行为变化（优先级高于 mock 剧本——关停分诊就是
    #    全量关停，连测试剧本也不该再消耗）
    if not triage_enabled():
        return None

    # 2. mock 剧本：DISPATCHER_MOCK=1 时确定性弹剧本；空剧本返回 None
    #    降级旧循环（既有 dispatcher 测试零改动的关键）
    if os.environ.get("DISPATCHER_MOCK") == "1":
        if not _TRIAGE_MOCK_SCRIPT:
            return None
        scripted = _TRIAGE_MOCK_SCRIPT.pop(0)
        try:
            return TriageResult.model_validate(scripted)
        except Exception as exc:  # noqa: BLE001 剧本容错：坏了也降级，不炸测试
            logger.warning("triage mock 剧本校验失败，降级旧循环: %s", exc)
            return None

    # 3. L2 操作记忆上下文（只作背景，不参与槽位填充）；失败吞掉不阻塞
    l2_context = ""
    if session_id:
        from app.dispatcher.memory import OperationMemory
        try:
            l2_context = OperationMemory(session_id).get_context_for_prompt()
        except Exception:  # noqa: BLE001 L2 记忆加载失败不阻塞主流程
            pass

    # 4. 组装 messages：system = triage_prompt（含 L2 + L1 槽位 + 最近对话历史）
    # L1 槽位上下文：进行中的任务与已收集参数注入 prompt（帮分诊器理解
    # 「是的」「对」这类针对进行中任务的短答；槽位合并仍由代码侧
    # merge_slots 完成，模型只输出本轮新提取参数）
    slots_context = None
    if session.current_target_tool:
        slots_context = {
            "target_tool": session.current_target_tool,
            "slots": dict(session.current_slots),
        }
    messages = [
        {"role": "system", "content": prompts.triage_prompt(
            phase=phase, l2_context=l2_context, history=session.history,
            slots_context=slots_context)},
        {"role": "user", "content": message},
    ]

    # 5-6. LLM 调用 + 契约校验；失败追加纠错提示重试一次，再失败降级
    result = _call_and_validate(messages)
    if result is None:
        return None

    # 7. Python 侧后校验（不信任模型输出）
    result = _post_validate(result, phase=phase)

    # 8. 进度事件（观测用，回调异常不阻塞）
    if on_progress is not None:
        try:
            on_progress({
                "type": "triage",
                "intent": result.intent,
                "target_tool": result.target_tool,
                "confidence": result.confidence,
            })
        except Exception:  # noqa: BLE001 进度回调是观测设施，不能搞挂主流程
            pass

    return result


def _call_and_validate(messages: list[dict]) -> TriageResult | None:
    """调 LLM 并校验输出契约；失败追加纠错提示重试一次，再失败返回 None。

    所有异常（无 API key、网络、超时、JSON 坏、契约不符）全部吞掉记
    warning——分诊是增强层，任何抖动都不该击穿调度主流程。
    """
    from app.extraction import llm_client  # 延迟 import：无 API key 时其余路径仍可用

    for attempt in range(2):
        try:
            raw = llm_client.chat_completion(
                messages,
                json_mode=True,
                source_file="dispatcher_triage",
                max_tokens=800,
                temperature=0.0,
                model=os.environ.get("DISPATCHER_TRIAGE_MODEL") or None,
            )
            parsed = json.loads(raw)
            return TriageResult.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 分诊失败一律降级，绝不抛出
            if attempt == 0:
                # 首次失败：回喂纠错提示再试一次（LLM 异常同样走这条——
                # 重试成本低，网络抖动有机会恢复）
                logger.warning(
                    "triage 首次调用/校验失败，重试一次: %s: %s",
                    type(exc).__name__, str(exc)[:200],
                )
                messages.append({
                    "role": "user",
                    "content": "上次输出不是合法 JSON 或不符合契约，请只输出合法 JSON",
                })
                continue
            logger.warning(
                "triage 重试仍失败，降级旧循环: %s: %s",
                type(exc).__name__, str(exc)[:200],
            )
            return None
    return None  # 防御：循环正常不会走到这


def _post_validate(result: TriageResult, *, phase: int) -> TriageResult:
    """Python 侧后校验（不信任模型）：工具名白名单 + confidence clamp。

    - target_tool 非 None 但不在当前 phase 可见工具清单内（模型编造或越权
      选了未下发工具）→ 强制 intent=clarify，保留 reply_message 让模型
      的反问（若有）仍可用；
    - confidence clamp 到 [0.0, 1.0]（模型可能给 95 或 -1 这类越界值）。
    """
    from app.dispatcher.tools import visible_tools  # 延迟 import 防循环依赖

    if result.target_tool is not None:
        known = {t.name for t in visible_tools(phase)}
        if result.target_tool not in known:
            logger.warning(
                "triage 输出未知工具 %r，强制 clarify（phase=%d）",
                result.target_tool, phase,
            )
            result.intent = "clarify"
    result.confidence = max(0.0, min(1.0, result.confidence))
    return result


def merge_slots(session: DispatcherSession, triage: TriageResult) -> dict:
    """把本轮提取参数与 session 槽位合并（不写入 session，由调用方 set_slots）。

    target_tool 变了 → 旧槽位作废，从 extracted_args 全新起始；
    同工具 → extracted_args 浅合并覆盖旧槽位（新一轮逐键优先）。
    """
    if triage.target_tool != session.current_target_tool:
        return dict(triage.extracted_args)
    merged = dict(session.current_slots)
    merged.update(triage.extracted_args)
    return merged


def missing_required(tool: str | None, args: dict) -> list[str]:
    """返回目标工具缺失的必填槽位名列表；tool 为 None 或不在表内返回 []。"""
    if tool is None:
        return []
    required = REQUIRED_SLOTS.get(tool)
    if not required:
        return []
    return [k for k in required if k not in args]
