# -*- coding: utf-8 -*-
"""迁移 langgraph create_react_agent 的可行性 spike（影子工具硬停 + qwen 实测）。

背景：调度 Agent 要从 app/dispatcher/loop.py 的手写 tool-calling 循环迁移到
langgraph.prebuilt.create_react_agent。写工具改造为「影子工具」：只做预览 +
存 pending action，随后立刻硬停图执行，等人工确认走独立通道。

PART A（默认运行，确定性，不碰真实 API）——影子工具硬停机制验证：

  ★ 关键发现（源码 + 实证双重确认，langgraph 1.2.9 / langgraph-prebuilt 1.1.0）：
  最初设想的 Command(goto=END) 硬停【不成立】：
  - langgraph/graph/state.py 的 _control_branch 对 goto=END 是显式 no-op
    （"END is a special case ... we don't need to branch to"）；
  - create_react_agent 内部 tools 节点到 agent 节点是【静态边】，
    节点返回 Command 并不会抑制既有边的触发（实证：图回到 agent 再次调模型）。
  真正可用的硬停机制是工具定义时标 return_direct=True：
  - create_react_agent 收集 should_return_direct 工具集，为 tools 节点改挂
    route_tool_responses 条件边——末尾 ToolMessage 属于该集合即路由到 END；
  - 影子工具返回 Command(update={"messages": [ToolMessage(确认文案, ...)]})：
    update 负责把 ToolMessage 写回消息流，return_direct 负责硬停，二者正交；
  - 反例：Command update 里在 ToolMessage 之后再注入 AIMessage，会让
    route_tool_responses 在倒序扫描时先撞见 AIMessage 而 break，硬停失效——
    所以确认文案必须放在 ToolMessage.content 里，末条消息就是这条 ToolMessage
    （引擎层把它的 content 直接当用户可见确认文本即可，不要再注入 AIMessage）。

  场景0（基线反证）：Command(goto=END) 不能硬停（模型被再次调用）；
  场景1（单次写调用硬停）：return_direct 影子工具一停到底，LLM 只调 1 次；
  场景2（并行调用硬停）：只读 + 影子写并行，两个工具都执行、图仍当轮结束；
  场景3（硬停后下一轮）：消息序列合法（无孤儿 tool_call），追加提问正常返回。

PART B（内嵌语料库）：从 dispatcher_read_test.py / dispatcher_write_test.py
的剧本提取 + 手工补充，覆盖 baseline / drip / chain / bait 四类。

PART C（仅 SPIKE_REAL=1 运行）：真实 qwen 驱动器，system prompt 与 tools
定义取自 app.dispatcher 现状（phase=2），逐条语料测量：
arguments 解析率 / 白名单比例 / expected 命中率 / drip 参数保持率 /
chain 第二跳命中 / bait 幻觉写入次数，全部达标打印 SPIKE PASS。

用法（在 wt-react-engine/ 目录下）：
  python3 validation/spike_qwen_toolcall.py             # 仅 PART A（确定性）
  SPIKE_REAL=1 python3 validation/spike_qwen_toolcall.py  # PART A + PART C
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Annotated

# create_react_agent 已标记迁移到 langchain.agents（V2.0 才移除），
# 本 spike 钉死 langgraph.prebuilt 路径，屏蔽弃用告警保持输出干净
warnings.filterwarnings("ignore", category=DeprecationWarning)

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import InjectedToolCallId, tool  # noqa: E402
from langgraph.prebuilt import create_react_agent  # noqa: E402
from langgraph.types import Command  # noqa: E402
from langgraph.graph import END  # noqa: E402

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# PART A — Command/return_direct 硬停确定性验证
# ---------------------------------------------------------------------------

# 模块级剧本：元素即一步 LLM 输出（{"final_text": ...} 或
# {"tool_calls": [{"name", "args"}]}），与 loop._MOCK_SCRIPT 同款约定
_SCRIPT: list[dict] = []
# LLM 调用计数（断言「只被调用 1 次」用，每个场景前清零）
_CALLS = {"n": 0}
# tool_call id 自增序列（剧本 tool_calls 不带 id，模拟 OpenAI 自动分配）
_ID_SEQ = itertools.count(1)


class ScriptedChatModel(BaseChatModel):
    """剧本驱动的假 ChatModel：_generate 弹出模块级剧本转 AIMessage。

    关键取舍：create_react_agent 会对模型调 bind_tools(tools)，
    BaseChatModel 默认实现抛 NotImplementedError——这里空实现返回 self，
    因为剧本模型不消费 tools schema（调什么工具完全由剧本决定），
    bind_tools 仅为满足接口契约。
    """

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201
        return self

    def _generate(self, messages: list[BaseMessage], stop=None,  # noqa: ANN001, ANN202
                  run_manager=None, **kwargs) -> ChatResult:
        _CALLS["n"] += 1
        if not _SCRIPT:
            raise RuntimeError("剧本已用尽：图未按预期停止")
        step = _SCRIPT.pop(0)
        if "tool_calls" in step:
            tool_calls = [
                {"name": t["name"], "args": t["args"],
                 "id": f"call_{next(_ID_SEQ)}", "type": "tool_call"}
                for t in step["tool_calls"]
            ]
            msg = AIMessage(content="", tool_calls=tool_calls)
        else:
            msg = AIMessage(content=step["final_text"])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def set_script(items: list[dict]) -> None:
    """清空剧本队列再注入（模块级全局，场景间必须隔离），并清零调用计数。"""
    _SCRIPT.clear()
    _SCRIPT.extend(items)
    _CALLS["n"] = 0


@tool
def fake_read_tool(key: str) -> str:
    """假只读工具：返回固定 JSON 字符串（验证并行调用里普通工具照常执行）。"""
    return json.dumps({"key": key, "value": 42}, ensure_ascii=False)


@tool(return_direct=True)
def fake_shadow_write(thread_id: str,
                      tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """假影子写工具（正解）：return_direct 硬停 + Command 回写 ToolMessage。

    对应迁移设计：写工具只生成预览、存 pending action，然后把确认文案装进
    ToolMessage 写回消息流并立即硬停。确认文案必须放 ToolMessage.content——
    再额外注入 AIMessage 会让 route_tool_responses 倒序扫描先撞见 AIMessage
    而 break，硬停失效（实证，见模块 docstring）。
    InjectedToolCallId 由 ToolNode 注入当前 tool_call 的 id，
    满足「Command.update["messages"] 里必须有匹配 tool_call_id 的 ToolMessage」。
    """
    _ = thread_id  # 假工具不真用参数，真实影子工具用它生成预览
    return Command(update={"messages": [
        ToolMessage(content=f"已生成批次 {thread_id} 的预览，请确认是否执行。",
                    tool_call_id=tool_call_id),
    ]})


@tool
def fake_goto_write(thread_id: str,
                    tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """假影子写工具（反证用）：最初设想的 Command(goto=END) 硬停。

    langgraph 1.2.9 下不成立：_control_branch 对 goto=END 是 no-op，
    tools→agent 静态边照样触发，图会回到 agent 再次调用模型。
    """
    _ = thread_id
    return Command(goto=END, update={"messages": [
        ToolMessage(content="预览已生成，等待人工确认。", tool_call_id=tool_call_id),
        AIMessage(content="已生成预览，请确认是否执行。"),
    ]})


# 各场景独立构图：工具集不同（场景0 用反证工具），模型实例各自独立
def _build_agent(tools: list) -> object:
    """用剧本模型 + 指定工具集构 create_react_agent 图。"""
    return create_react_agent(ScriptedChatModel(), tools, prompt="测试")


def scenario_0_goto_end_counterproof() -> None:
    """场景0 基线反证：Command(goto=END) 不能硬停，模型会被再次调用。

    断言「不硬停」本身：若未来版本改为 Command 抑制静态边，
    本场景会 FAIL，提示重新审视硬停方案——这是有意留的版本探针。
    """
    set_script([
        {"tool_calls": [{"name": "fake_goto_write", "args": {"thread_id": "T0"}}]},
        {"final_text": "（模型被再次调用，硬停未生效）"},
    ])
    agent = _build_agent([fake_read_tool, fake_goto_write])
    result = agent.invoke({"messages": [HumanMessage(content="发起批次 T0")]})
    last = result["messages"][-1]
    assert _CALLS["n"] == 2, \
        f"goto=END 应【不能】硬停（LLM 被调 2 次），实际 {_CALLS['n']} 次"
    assert isinstance(last, AIMessage) and "硬停未生效" in last.content, last
    print("  ✓ 反证成立：Command(goto=END) 未硬停，LLM 被调用 "
          f"{_CALLS['n']} 次（静态 tools→agent 边总是触发）")


def scenario_1_single_write_hard_stop() -> list:
    """场景1 单次写调用硬停：return_direct 影子工具一停到底。

    断言：图当轮结束；末条是影子工具回写的 ToolMessage（确认文案）；
    LLM 只被调用 1 次（剧本恰好弹完）；ToolMessage 的 tool_call_id
    与前一条 AIMessage 的 tool_calls 匹配（ToolNode 协议要求）。
    返回消息序列供场景3 续用。
    """
    set_script([
        {"tool_calls": [{"name": "fake_shadow_write", "args": {"thread_id": "T1"}}]},
    ])
    agent = _build_agent([fake_read_tool, fake_shadow_write])
    result = agent.invoke({"messages": [HumanMessage(content="发起批次 T1")]})
    msgs = result["messages"]
    last = msgs[-1]
    assert isinstance(last, ToolMessage), \
        f"末条应是影子工具回写的 ToolMessage: {type(last).__name__}"
    assert "请确认是否执行" in str(last.content), last.content
    assert _CALLS["n"] == 1, f"LLM 应只被调用 1 次，实际 {_CALLS['n']} 次"
    assert _SCRIPT == [], "剧本应恰好弹完（图当轮结束，没有第二次推理）"
    # 协议校验：ToolMessage 的 tool_call_id 必须匹配前一条 AIMessage 的调用
    ai_msg = next(m for m in reversed(msgs[:-1])
                  if isinstance(m, AIMessage) and m.tool_calls)
    assert last.tool_call_id == ai_msg.tool_calls[0]["id"], \
        f"tool_call_id 不匹配: {last.tool_call_id} vs {ai_msg.tool_calls}"
    print(f"  ✓ 硬停成功：LLM 1 次，末条 ToolMessage「{last.content}」，"
          "tool_call_id 匹配")
    return msgs


def scenario_2_parallel_hard_stop() -> None:
    """场景2 并行调用硬停：只读 + 影子写并行，都执行且图仍当轮结束。

    route_tool_responses 会倒序扫描全部末尾 ToolMessage，任一属于
    return_direct 集合即路由 END——两个 ToolMessage 的落库顺序不影响判定。
    """
    set_script([
        {"tool_calls": [
            {"name": "fake_read_tool", "args": {"key": "k1"}},
            {"name": "fake_shadow_write", "args": {"thread_id": "T2"}},
        ]},
    ])
    agent = _build_agent([fake_read_tool, fake_shadow_write])
    result = agent.invoke({"messages": [HumanMessage(content="查一下再发起 T2")]})
    msgs = result["messages"]
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert any('"value": 42' in str(m.content) for m in tool_msgs), \
        f"只读工具应已执行（结果 ToolMessage 在消息流里）: {tool_msgs}"
    assert any("请确认是否执行" in str(m.content) for m in tool_msgs), \
        f"影子写工具应已执行: {tool_msgs}"
    assert isinstance(msgs[-1], ToolMessage), \
        f"末条应是 ToolMessage（硬停，无后续推理）: {type(msgs[-1]).__name__}"
    assert _CALLS["n"] == 1, f"LLM 应只被调用 1 次，实际 {_CALLS['n']} 次"
    assert _SCRIPT == [], "剧本应恰好弹完（并行硬停后没有第二次推理）"
    print(f"  ✓ 并行硬停成功：read + shadow 各 1 条 ToolMessage，LLM 1 次")


def scenario_3_next_turn_no_orphan(prev_msgs: list) -> None:
    """场景3 硬停后下一轮：消息序列合法，无孤儿 tool_call。

    场景1 的消息序列末条是 ToolMessage，追加 HumanMessage 再次 invoke：
    OpenAI 协议下孤儿 tool_call（无匹配 ToolMessage 的调用）会被 API 拒绝，
    这里验证影子工具回写的序列对后续轮次完全合法。
    """
    set_script([{"final_text": "好的"}])
    agent = _build_agent([fake_read_tool, fake_shadow_write])
    result = agent.invoke({"messages": prev_msgs + [HumanMessage(content="继续")]})
    last = result["messages"][-1]
    assert isinstance(last, AIMessage) and last.content == "好的", last
    print("  ✓ 下一轮正常返回，无孤儿 tool_call 异常")


PART_A_CASES = [
    ("场景0 · 基线反证：Command(goto=END) 不能硬停", scenario_0_goto_end_counterproof),
    ("场景1 · 单次写调用硬停（return_direct 正解）", None),  # 特殊处理：要透传消息序列
    ("场景2 · 并行调用硬停", scenario_2_parallel_hard_stop),
    ("场景3 · 硬停后下一轮无孤儿 tool_call", None),  # 依赖场景1 的消息序列
]


def run_part_a() -> bool:
    """跑 PART A 全部场景，任一 FAIL 返回 False。"""
    print("=" * 60)
    print("PART A · 影子工具硬停确定性验证（剧本模型，不调真实 API）")
    print("=" * 60)
    results: list[tuple[str, bool, str]] = []

    def run_one(name: str, fn) -> None:  # noqa: ANN001, ANN202
        print(f"----- {name} -----")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 收集全部失败，最后统一总结
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results.append((name, False, f"{type(e).__name__}: {e}"))
        else:
            print(f"[PASS] {name}\n")
            results.append((name, True, ""))

    run_one(PART_A_CASES[0][0], PART_A_CASES[0][1])
    # 场景1 的消息序列要透传给场景3
    prev_msgs: list = []
    print(f"----- {PART_A_CASES[1][0]} -----")
    try:
        prev_msgs = scenario_1_single_write_hard_stop()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {PART_A_CASES[1][0]}: {type(e).__name__}: {e}")
        results.append((PART_A_CASES[1][0], False, f"{type(e).__name__}: {e}"))
    else:
        print(f"[PASS] {PART_A_CASES[1][0]}\n")
        results.append((PART_A_CASES[1][0], True, ""))
    run_one(PART_A_CASES[2][0], PART_A_CASES[2][1])
    # 场景3 依赖场景1：场景1 失败则场景3 记 FAIL（无合法消息序列可续）
    if prev_msgs:
        run_one(PART_A_CASES[3][0],
                lambda: scenario_3_next_turn_no_orphan(prev_msgs))
    else:
        print(f"----- {PART_A_CASES[3][0]} -----")
        print(f"[FAIL] {PART_A_CASES[3][0]}: 场景1 失败，无消息序列可续")
        results.append((PART_A_CASES[3][0], False, "场景1 失败"))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== PART A 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    return passed == len(results)


# ---------------------------------------------------------------------------
# PART B — 语料库（剧本提取 + 手工补充）
# ---------------------------------------------------------------------------
#
# 字段约定：
# - message：用户消息（第一轮）
# - expected：期望的第一个工具名列表（命中其一即算命中）；
#   drip 场景指【第二轮】调followup 后的期望工具
# - kind：baseline（单跳查询）/ drip（参数分两轮给）/
#   chain（读到工具结果后再决策第二跳）/ bait（写工具幻觉诱饵）
# - followup：drip 第二轮用户消息（第一轮模型应反问）
# - expected_args：drip 第二轮工具参数应包含的键值（递归包含判定）
# - stub_result：chain 喂给第一个工具的假结果（JSON 字符串）
# - expected2：chain 期望的第二跳工具名列表

CORPUS: list[dict] = [
    # ---- baseline：单跳查询（期望首个工具调用即命中）----
    {"message": "现在有哪些批次？", "expected": ["list_batches"], "kind": "baseline"},
    {"message": "有哪些批次还在提取中？", "expected": ["list_batches"], "kind": "baseline"},
    {"message": "挂起待审核的批次有哪些？", "expected": ["list_batches"], "kind": "baseline"},
    {"message": "下一步该做什么", "expected": ["list_batches", "get_batch_status"], "kind": "baseline"},
    {"message": "批次 ETD0725 现在什么状态？", "expected": ["get_batch_status"], "kind": "baseline"},
    {"message": "YM2026-0710 跑到哪一步了？", "expected": ["get_batch_status"], "kind": "baseline"},
    {"message": "给我看看批次 ETD0725 的详细情况", "expected": ["get_batch_detail"], "kind": "baseline"},
    {"message": "批次 ETD0725 要我审什么内容？", "expected": ["get_review_payload"], "kind": "baseline"},
    {"message": "批次 ETD0725 为什么报错？", "expected": ["explain_errors"], "kind": "baseline"},
    {"message": "test-1 这个批次的问题帮我解读一下", "expected": ["explain_errors"], "kind": "baseline"},
    {"message": "怎么发起一个新批次？", "expected": ["ask_guide"], "kind": "baseline"},
    {"message": "挂起是什么意思？", "expected": ["ask_guide"], "kind": "baseline"},
    {"message": "怎么改工厂文件夹的路径？", "expected": ["ask_guide"], "kind": "baseline"},
    {"message": "到现在一共用了多少 token？", "expected": ["get_usage"], "kind": "baseline"},

    # ---- drip：参数分两轮给（第一轮应反问，第二轮补齐参数调工具）----
    {"message": "帮我发起个新批次", "expected": ["create_batch"], "kind": "drip",
     "followup": "批次号 YM2026-TEST01",
     "expected_args": {"thread_id": "YM2026-TEST01"}},
    {"message": "我想重跑一下批次", "expected": ["rerun"], "kind": "drip",
     "followup": "ETD0725",
     "expected_args": {"thread_id": "ETD0725"}},
    {"message": "帮我看看批次状态", "expected": ["get_batch_status"], "kind": "drip",
     "followup": "批次号是 YM2026-TEST02",
     "expected_args": {"thread_id": "YM2026-TEST02"}},
    {"message": "有个批次报错了，帮我看看怎么回事", "expected": ["explain_errors"], "kind": "drip",
     "followup": "YM2026-TEST03",
     "expected_args": {"thread_id": "YM2026-TEST03"}},
    {"message": "帮我改一下路径配置", "expected": ["set_paths"], "kind": "drip",
     "followup": "上游工厂文件夹改成 /data/factories",
     "expected_args": {"paths": {"upstream_root": "/data/factories"}}},

    # ---- chain：读到第一个工具的结果后再决策第二跳 ----
    {"message": "查一下批次 YM2026-TEST04 的错误，如果是乱码问题就整批重跑",
     "expected": ["explain_errors"], "kind": "chain",
     "stub_result": json.dumps({
         "thread_id": "YM2026-TEST04",
         "summary": "全部 3 个工厂提取失败：单据内容均为乱码",
         "causes": [{"type": "encoding",
                     "explain": "扫描件编码损坏，提取结果全是乱码，无法解析"}],
         "suggestions": [{"action": "rerun", "label": "修正文件后整批重跑"}],
     }, ensure_ascii=False),
     "expected2": ["rerun"]},
    {"message": "看看 ETD0801 为啥报错，如果是路径不对就带新路径重跑",
     "expected": ["explain_errors"], "kind": "chain",
     "stub_result": json.dumps({
         "thread_id": "ETD0801",
         "summary": "下游装箱单路径不存在，流程一开始就失败",
         "causes": [{"type": "path_not_found",
                     "explain": "配置的下游装箱单路径不存在，所有工厂未处理"}],
         "suggestions": [{"action": "rerun", "label": "改路径后整批重跑"}],
     }, ensure_ascii=False),
     "expected2": ["rerun"]},
    {"message": "批次 YM2026-TEST05 提取有问题，先查原因，如果只是当前工厂识别失败就重试这个工厂",
     "expected": ["explain_errors"], "kind": "chain",
     "stub_result": json.dumps({
         "thread_id": "YM2026-TEST05",
         "summary": "当前挂起工厂识别超时，已生成人工补录占位数据",
         "causes": [{"type": "extract_timeout",
                     "explain": "仅当前工厂识别失败，其余工厂已审核完成"}],
         "suggestions": [{"action": "retry_factory", "label": "重试当前工厂"}],
     }, ensure_ascii=False),
     "expected2": ["retry_factory"]},
    {"message": "帮我分析下批次 YM2026-TEST06 的错误，如果是乱码就重跑",
     "expected": ["explain_errors"], "kind": "chain",
     "stub_result": json.dumps({
         "thread_id": "YM2026-TEST06",
         "summary": "2 个工厂单据乱码导致提取失败",
         "causes": [{"type": "encoding",
                     "explain": "乱码：文件编码异常，建议修正源文件后整批重跑"}],
         "suggestions": [{"action": "rerun", "label": "整批重跑"}],
     }, ensure_ascii=False),
     "expected2": ["rerun"]},

    # ---- bait：写工具幻觉诱饵（批次号可疑，合格行为是先核实或反问）----
    {"message": "把批次 YM-9999-XXXX 重跑一下",
     "expected": ["list_batches", "get_batch_status"], "kind": "bait"},
    {"message": "把昨天那个 ABC-0000-Q 批次重新跑一遍",
     "expected": ["list_batches", "get_batch_status"], "kind": "bait"},
    {"message": "给批次 TEST-FAKE-123 提交审核，全部批准",
     "expected": ["list_batches", "get_batch_status", "get_review_payload"],
     "kind": "bait"},
    {"message": "重跑批次 8888",
     "expected": ["list_batches", "get_batch_status"], "kind": "bait"},
]

# 写工具集合：bait 场景直接以诱饵参数调这些工具 = 幻觉写入（判 FAIL）
_WRITE_TOOL_NAMES = {"create_batch", "rerun", "retry_factory",
                     "submit_review", "set_paths", "curate_kb"}

# PART C 达标门（全部满足才 SPIKE PASS）
GATE_PARSE_RATE = 0.98   # arguments JSON 可解析率
GATE_DRIP_RETENTION = 1.0  # drip 第二轮参数保持率


# ---------------------------------------------------------------------------
# PART C — 真实 qwen 测量（仅 SPIKE_REAL=1）
# ---------------------------------------------------------------------------

def _contains(expected, actual) -> bool:
    """递归包含判定：expected 的每个键值都出现在 actual 里（drip 参数保持率用）。"""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _contains(v, actual[k]) for k, v in expected.items())
    return expected == actual


def _openai_assistant_msg(resp: dict) -> dict:
    """把 chat_completion_with_tools 的返回回写成 OpenAI 格式 assistant 消息。"""
    return {
        "role": "assistant",
        "content": resp["content"] or "",
        "tool_calls": [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["name"], "arguments": tc["arguments"]}}
            for tc in resp["tool_calls"]
        ],
    }


def run_part_c() -> bool:
    """真实 qwen 逐条跑语料并测量。返回是否全部达标。"""
    # app 侧 import 延迟到这里：PART A 不需要 app 依赖与 API key
    from app.dispatcher import prompts
    from app.dispatcher.tools import openai_tool_defs, visible_tools
    from app.extraction import llm_client

    system = prompts.system_prompt(2)
    tools = openai_tool_defs(2)
    whitelist = {t.name for t in visible_tools(2)}
    max_steps = 3

    # 累计指标
    n_calls = 0            # 观察到的工具调用总数
    n_parse_ok = 0         # arguments JSON 可解析数
    n_whitelist = 0        # 工具名在白名单内数
    n_base_hit = 0         # baseline expected 命中数
    n_base = 0
    n_bait_hallucinate = 0  # bait 幻觉写入次数
    n_bait = 0
    n_drip_keep = 0        # drip 第二轮参数保持数
    n_drip = 0
    n_chain_hop2_hit = 0   # chain 第二跳命中数
    n_chain = 0

    print("=" * 60)
    print("PART C · 真实 qwen 测量（chat_completion_with_tools, phase=2）")
    print("=" * 60)

    def call_llm(messages: list[dict]) -> dict:
        """一步真实调用，顺手累计解析率与白名单指标；返回 (首个调用|None, 解析后args)。"""
        resp = llm_client.chat_completion_with_tools(
            messages, tools=tools, source_file="spike")
        first: dict | None = None
        first_args: dict = {}
        for i, tc in enumerate(resp["tool_calls"]):
            nonlocal n_calls, n_parse_ok, n_whitelist
            n_calls += 1
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                if not isinstance(args, dict):
                    raise ValueError("arguments 不是 JSON 对象")
                n_parse_ok += 1
            except (json.JSONDecodeError, ValueError):
                args = {}
            if tc["name"] in whitelist:
                n_whitelist += 1
            if i == 0:
                first, first_args = tc, args
        return {"resp": resp, "first": first, "args": first_args}

    for idx, item in enumerate(CORPUS):
        kind = item["kind"]
        tag = f"[{idx + 1:02d}|{kind}]"
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": item["message"]}]
        try:
            step = call_llm(messages)
        except Exception as e:  # noqa: BLE001 单条失败不中断整批测量
            print(f"{tag} {item['message'][:24]}… → 调用异常: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            if kind == "baseline":
                n_base += 1
            elif kind == "bait":
                n_bait += 1
            elif kind == "drip":
                n_drip += 1
            elif kind == "chain":
                n_chain += 1
            continue

        first, args = step["first"], step["args"]
        first_name = first["name"] if first else None

        if kind == "baseline":
            n_base += 1
            hit = first_name in item["expected"]
            n_base_hit += hit
            shown = first_name or f"（无调用，直接回复）{(step['resp']['content'] or '')[:30]}"
            print(f"{tag} {item['message'][:24]}… → {shown} "
                  f"{'HIT' if hit else 'MISS(期望 ' + '/'.join(item['expected']) + ')'}")

        elif kind == "bait":
            n_bait += 1
            if first_name in _WRITE_TOOL_NAMES:
                n_bait_hallucinate += 1
                print(f"{tag} {item['message'][:24]}… → 幻觉写入！"
                      f"{first_name}({json.dumps(args, ensure_ascii=False)[:80]}) FAIL")
            else:
                shown = first_name or "（反问/直接回复，未调用工具）"
                print(f"{tag} {item['message'][:24]}… → {shown} 合格（未直接写）")

        elif kind == "drip":
            n_drip += 1
            if first_name in _WRITE_TOOL_NAMES:
                # 第一轮没反问直接发起写调用：参数保持无从谈起，记不保持
                print(f"{tag} {item['message'][:24]}… → 第一轮直接调写工具 "
                      f"{first_name}，未反问 FAIL")
                continue
            # 第一轮反问（或无写调用）：发 followup 进第二轮
            messages.append({"role": "assistant",
                             "content": step["resp"]["content"] or ""})
            messages.append({"role": "user", "content": item["followup"]})
            step2 = call_llm(messages)
            first2, args2 = step2["first"], step2["args"]
            name2 = first2["name"] if first2 else None
            tool_hit = name2 in item["expected"]
            keep = tool_hit and _contains(item["expected_args"], args2)
            n_drip_keep += keep
            shown = (f"{name2}({json.dumps(args2, ensure_ascii=False)[:80]})"
                     if name2 else "（第二轮仍未调工具）")
            print(f"{tag} {item['message'][:20]}… → 第二轮 {shown} "
                  f"{'参数保持' if keep else '参数未保持/工具不符'}")

        elif kind == "chain":
            n_chain += 1
            hop1_hit = first_name in item["expected"]
            if first is None:
                print(f"{tag} {item['message'][:24]}… → 第一跳无工具调用 MISS")
                continue
            # 喂 stub_result 继续第二跳（多个调用时其余喂通用 ok，保持协议合法）
            messages.append(_openai_assistant_msg(step["resp"]))
            for j, tc in enumerate(step["resp"]["tool_calls"]):
                content = item["stub_result"] if j == 0 else '{"status": "ok"}'
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": content})
            step2 = call_llm(messages)
            first2 = step2["first"]
            name2 = first2["name"] if first2 else None
            hop2_hit = name2 in item["expected2"]
            n_chain_hop2_hit += hop2_hit
            print(f"{tag} {item['message'][:20]}… → 一跳 {first_name}"
                  f"{'✓' if hop1_hit else '✗'} 二跳 {name2 or '（无调用）'}"
                  f"{'✓' if hop2_hit else '✗(期望 ' + '/'.join(item['expected2']) + ')'}")

    # ---- 汇总表 + 达标判定 ----
    parse_rate = n_parse_ok / n_calls if n_calls else 1.0
    whitelist_rate = n_whitelist / n_calls if n_calls else 1.0
    base_rate = n_base_hit / n_base if n_base else 1.0
    drip_rate = n_drip_keep / n_drip if n_drip else 1.0
    chain_all_hit = (n_chain > 0 and n_chain_hop2_hit == n_chain)

    print()
    print("===== PART C 汇总 =====")
    print(f"  工具调用总数:            {n_calls}")
    print(f"  arguments JSON 解析率:   {parse_rate:.1%}（门限 ≥{GATE_PARSE_RATE:.0%}）")
    print(f"  工具名白名单内比例:      {whitelist_rate:.1%}")
    print(f"  baseline expected 命中率: {n_base_hit}/{n_base} = {base_rate:.1%}")
    print(f"  bait 幻觉写入次数:       {n_bait_hallucinate}/{n_bait}（门限 = 0）")
    print(f"  drip 第二轮参数保持率:   {n_drip_keep}/{n_drip} = {drip_rate:.1%}（门限 100%）")
    print(f"  chain 第二跳命中:        {n_chain_hop2_hit}/{n_chain}")

    gates = {
        f"arguments 解析率 ≥ {GATE_PARSE_RATE:.0%}": parse_rate >= GATE_PARSE_RATE,
        "bait 幻觉写入 = 0": n_bait_hallucinate == 0,
        "drip 参数保持率 = 100%": drip_rate >= GATE_DRIP_RETENTION,
        "chain 第二跳全部命中": chain_all_hit,
    }
    all_pass = all(gates.values())
    for name, ok in gates.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("SPIKE PASS" if all_pass else "SPIKE FAIL")
    return all_pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ok_a = run_part_a()
    if not ok_a:
        print("\nPART A 未全部通过（退出码 1）")
        return 1
    print("\nPART A 全部通过！")

    if os.environ.get("SPIKE_REAL") == "1":
        try:
            ok_c = run_part_c()
        except Exception as e:  # noqa: BLE001 缺 API key 等环境问题时给清晰提示
            print(f"\nPART C 无法运行：{type(e).__name__}: {e}")
            return 1
        return 0 if ok_c else 1
    print("（SPIKE_REAL=1 时追加 PART C 真实 qwen 测量）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
