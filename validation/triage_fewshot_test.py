# -*- coding: utf-8 -*-
"""Triage 静态精编 Few-Shot 注入测试（prompt 文本层断言）。

背景：`_TRIAGE_PROMPT` 在 `{l2_context}` 占位符之前注入 `## 分类示例` 段，
6 条静态精编（输入 → 标准 JSON 输出）few-shot 示例，针对历史误分类模式：
指代词（再跑一次）、确认性语句误提取参数（按推荐的来）、qa/action 边界
（挂起是什么意思 vs 具体批次为什么挂起）、next-step 路由固化（下一步是
什么）、黄灯示范（模糊指代低置信确认式反问）、中止识别（算了不弄了）。

硬性契约（对照 triage.TriageResult 逐字段核对）：
1. 示例 JSON 的字段名只能是 intent/target_tool/extracted_args/reply_message/
   confidence，intent 取值只有 qa/action/clarify；
2. 示例里的 target_tool 必须是 tools.visible_tools(phase=2) 里真实存在的
   工具名（教错工具名 = 教坏模型）；
3. 不引入新花括号占位符——prompt 用 str.replace 注入 {tool_list}/
   {l2_context}，示例 JSON 的花括号没问题，但不得新增 {xxx} 形式的词。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/triage_fewshot_test.py

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。本测试只读 prompt 文本，但保持一致
的隔离姿势防未来用例扩展碰真实库。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ---- env 前置（必须在 import app 之前；db 路径在 import 后隔离）----
os.environ.setdefault("EXTRACTION_MOCK", "1")   # 提取走 mock（本测试不跑图，防御性设置）
os.environ["DISPATCHER_MOCK"] = "1"             # 调度循环 + 分诊都走剧本，不调真实 LLM
os.environ["GUIDE_MOCK"] = "1"                  # ask_guide 走模板降级（确定性输出）
# 本文件测 triage 分诊层（legacy 引擎专属机制），无论外部环境变量如何都钉死
# legacy——防止全量套件以 DISPATCHER_ENGINE=react 外导时被误跑
os.environ["DISPATCHER_ENGINE"] = "legacy"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.dispatcher import prompts, triage  # noqa: E402
from app.dispatcher.tools import visible_tools  # noqa: E402

# 二次钉死：llm_client 模块 import 时 load_dotenv(override=True) 会把
# 生产 .env 的 DISPATCHER_ENGINE=react 灌进 os.environ 覆盖上面的钉——
# app import 完成后必须重钉（_engine() 在调用时才读，此处钉住即生效）
os.environ["DISPATCHER_ENGINE"] = "legacy"

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_triage_fewshot_test_", alias_map_copy=True)

# 契约允许的字段名（对照 triage.TriageResult 逐字一致）
_CONTRACT_FIELDS = {"intent", "target_tool", "extracted_args",
                    "reply_message", "confidence"}
_CONTRACT_INTENTS = {"qa", "action", "clarify"}


def _extract_examples(prompt_text: str) -> list[dict]:
    """从 `## 分类示例` 段解析全部 输出：{...} JSON，返回 dict 列表。"""
    # 段落文本：从 ## 分类示例 到段尾（本段位于 prompt 末尾，l2_context 为空时
    # 即文本末尾；有上下文时到【最近操作上下文】之前）
    start = prompt_text.index("## 分类示例")
    tail = prompt_text[start:]
    end = tail.find("【最近操作上下文】")
    section = tail[:end] if end != -1 else tail
    outputs = re.findall(r"^输出：(\{.*\})$", section, re.M)
    return [json.loads(o) for o in outputs]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_section_present() -> None:
    """triage_prompt(phase=2) 含 ## 分类示例 段，且在可用工具清单之后。"""
    p = prompts.triage_prompt(phase=2)
    assert "## 分类示例" in p, "triage_prompt 应含 ## 分类示例 段"
    assert p.index("## 分类示例") > p.index("## 可用工具清单"), \
        "## 分类示例 应位于可用工具清单之后（先给清单再给示例）"


def case_2_example_count_and_contract() -> None:
    """解析出 7 条示例；每条字段名/intent 取值逐字符合 TriageResult 契约。

    设计文档列 6 个编号示例，其中第 3 号是 qa/action 成对边界示例
    （「挂起是什么意思」+「test-1 为什么挂起」两条），故实际 7 条 JSON。
    """
    p = prompts.triage_prompt(phase=2)
    examples = _extract_examples(p)
    assert len(examples) == 7, \
        f"应解析出 7 条示例（6 个编号，边界对占 2 条），实际 {len(examples)} 条"
    for i, ex in enumerate(examples, 1):
        extra = set(ex) - _CONTRACT_FIELDS
        assert not extra, f"示例 {i} 含契约外字段: {extra}"
        assert ex["intent"] in _CONTRACT_INTENTS, \
            f"示例 {i} intent 取值非法: {ex['intent']}"
        assert isinstance(ex["extracted_args"], dict), \
            f"示例 {i} extracted_args 应为 dict"
        assert 0.0 <= ex["confidence"] <= 1.0, \
            f"示例 {i} confidence 越界: {ex['confidence']}"


def case_3_target_tools_real() -> None:
    """6 条示例里非 null 的 target_tool 均在 visible_tools(phase=2) 内。"""
    p = prompts.triage_prompt(phase=2)
    examples = _extract_examples(p)
    known = {t.name for t in visible_tools(2)}
    for i, ex in enumerate(examples, 1):
        tool = ex.get("target_tool")
        if tool is None:
            continue
        assert tool in known, \
            f"示例 {i} 的 target_tool={tool!r} 不在 visible_tools(2) 内: {sorted(known)}"


def case_4_covers_misclassification_patterns() -> None:
    """6 条示例覆盖六类历史误分类模式（指代/确认语句/边界对/next-step/黄灯/中止）。"""
    p = prompts.triage_prompt(phase=2)
    for needle in ["再跑一次", "按推荐的来", "挂起是什么意思",
                   "为什么挂起", "下一步是什么", "中地", "算了不弄了"]:
        assert needle in p, f"分类示例段应含模式输入: {needle}"
    # 边界对：概念问题 qa、具体批次问题 action/explain_errors
    examples = _extract_examples(p)
    qa_ex = next(e for e in examples if e["intent"] == "qa")
    assert qa_ex["target_tool"] == "ask_guide", \
        f"qa 边界示例 target_tool 应为 ask_guide: {qa_ex}"
    boundary_ex = next(e for e in examples
                       if e["extracted_args"].get("thread_id") == "test-1")
    assert boundary_ex["target_tool"] == "explain_errors", \
        f"具体批次边界示例应路由 explain_errors: {boundary_ex}"
    # 黄灯示例：confidence 落在 0.6–0.8 且 reply_message 非空（确认式反问）
    yellow = next(e for e in examples if e["intent"] == "action"
                  and 0.6 <= e["confidence"] <= 0.8)
    assert yellow["reply_message"], \
        f"黄灯示例 reply_message 应非空（确认式反问）: {yellow}"
    # 中止示例：clarify + target_tool null + reply_message 确认已取消
    cancel = next(e for e in examples if e["intent"] == "clarify")
    assert cancel["target_tool"] is None, \
        f"中止示例 target_tool 应为 null: {cancel}"
    assert "取消" in cancel["reply_message"], \
        f"中止示例 reply_message 应确认已取消: {cancel}"


def case_5_placeholder_injection_clean() -> None:
    """{tool_list}/{l2_context} 注入后无残留；不引入新花括号占位符。"""
    p = prompts.triage_prompt(phase=2)
    assert "{tool_list}" not in p, "不应残留 {tool_list} 占位符"
    assert "{l2_context}" not in p, "不带 l2_context 调用时占位符应被替换为空"
    # 不得新增 {xxx} 形式的词（prompt 用 str.replace 注入，新占位符永不替换）
    leftovers = re.findall(r"\{[a-z_]+\}", p)
    assert not leftovers, f"不应残留花括号占位符形式的词: {leftovers}"
    # 带 l2_context 调用时上下文段渲染在分类示例之后
    # （「【最近操作上下文】」字样在意图定义段也出现过，须从分类示例段往后找）
    p2 = prompts.triage_prompt(phase=2, l2_context="最近批次：ETD0725")
    ctx_at = p2.find("【最近操作上下文】", p2.index("## 分类示例"))
    assert ctx_at != -1, "l2_context 应渲染成【最近操作上下文】段"
    assert "最近批次：ETD0725" in p2[ctx_at:], "l2_context 内容应注入到示例段之后"


def case_6_triage_result_accepts_examples() -> None:
    """6 条示例 JSON 逐条过 TriageResult.model_validate（与代码侧契约同源校验）。"""
    p = prompts.triage_prompt(phase=2)
    examples = _extract_examples(p)
    for i, ex in enumerate(examples, 1):
        triage.TriageResult.model_validate(ex)  # 不抛即过
        # 静态类型之外的语义：confidence 是数字
        assert isinstance(ex["confidence"], (int, float)), \
            f"示例 {i} confidence 应为数字: {ex['confidence']!r}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. triage_prompt 含 ## 分类示例 段（位置正确）", case_1_section_present),
    ("2. 解析 7 条示例（6 编号含边界对）且契约逐字符合", case_2_example_count_and_contract),
    ("3. 示例 target_tool 均在 visible_tools(2) 内", case_3_target_tools_real),
    ("4. 覆盖六类历史误分类模式（含黄灯/中止语义）", case_4_covers_misclassification_patterns),
    ("5. 占位符注入无残留、无新增花括号占位符", case_5_placeholder_injection_clean),
    ("6. 示例 JSON 逐条过 TriageResult 校验", case_6_triage_result_accepts_examples),
]


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name, fn in CASES:
        print(f"===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 收集全部失败，最后统一总结
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results.append((name, False, f"{type(e).__name__}: {e}"))
        else:
            print(f"[PASS] {name}")
            results.append((name, True, ""))
        print()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    if passed == len(results):
        print("🎉 Triage 静态 few-shot 注入测试全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
