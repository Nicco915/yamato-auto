# -*- coding: utf-8 -*-
"""调度 Agent Executor prompt 测试（prompts.executor_prompt + loop hint 门控）。

覆盖（session_id 一律用 DISP-EXEC-TEST- 前缀，与并行运行的其他
dispatcher 测试隔离）：
1. prompt 内容（纯单元）：executor_prompt(phase=2) 含执行器角色/分诊层表述、
   7 个只读工具名、写规则复用（alias_decisions/skip_processed）、铁律段，
   且不含「操作指导 vs 数据查询」判别段；phase=1 不含写工具；system_prompt
   未受影响；
2. 门控（mock 集成 + spy 计数）：无 hint（triage 空剧本降级）/ 低置信 action
   → system_prompt；高置信 action 带 hint → executor_prompt；
3. executor prompt 下的行为连通性：高置信 action（list_batches 只读免确认）
   → 工具调用链路完整（tool_calls → 执行 → final_text），返回 ok。

剧本队列均为模块级全局（triage._TRIAGE_MOCK_SCRIPT / loop._MOCK_SCRIPT），
每个用例前一并 clear 再注入，用例间互不污染。prompt 选择门控用 spy 包装
prompts.system_prompt / prompts.executor_prompt 计数（loop 通过模块属性
查找调用，monkeypatch 模块属性即生效），用例结束恢复原函数。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/dispatcher_executor_prompt_test.py

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
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

from app import dispatcher  # noqa: E402
from app.dispatcher import loop, prompts, sessions, triage  # noqa: E402
from app.graph import get_graph  # noqa: E402

# 二次钉死：llm_client 模块 import 时 load_dotenv(override=True) 会把
# 生产 .env 的 DISPATCHER_ENGINE=react 灌进 os.environ 覆盖上面的钉——
# app import 完成后必须重钉（_engine() 在调用时才读，此处钉住即生效）
os.environ["DISPATCHER_ENGINE"] = "legacy"

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_exec_prompt_test_", alias_map_copy=True)

# 7 个只读工具（与 prompts._EXECUTOR_READ_PROMPT 清单一一对应）
READ_ONLY_TOOLS = [
    "list_batches", "get_batch_status", "get_batch_detail",
    "get_review_payload", "explain_errors", "get_usage", "ask_guide",
]


def set_scripts(triage_items: list[dict] | None = None,
                loop_items: list[dict] | None = None) -> None:
    """清空两个剧本队列再注入（模块级全局，用例间必须隔离）。"""
    triage._TRIAGE_MOCK_SCRIPT.clear()
    triage._TRIAGE_MOCK_SCRIPT.extend(triage_items or [])
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(loop_items or [])


@contextmanager
def spy_prompts():
    """spy 包装 prompts.system_prompt / prompts.executor_prompt 计数。

    loop.run_dispatch 通过模块属性调用（prompts.system_prompt(phase)），
    patch 模块属性即生效；yield 计数 dict，退出时恢复原函数。
    """
    counts = {"system": 0, "executor": 0}
    orig_system = prompts.system_prompt
    orig_executor = prompts.executor_prompt

    def system_spy(phase: int = 2) -> str:
        counts["system"] += 1
        return orig_system(phase)

    def executor_spy(phase: int = 2) -> str:
        counts["executor"] += 1
        return orig_executor(phase)

    prompts.system_prompt = system_spy
    prompts.executor_prompt = executor_spy
    try:
        yield counts
    finally:
        prompts.system_prompt = orig_system
        prompts.executor_prompt = orig_executor


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_prompt_content() -> None:
    """prompt 内容（纯单元）：executor_prompt 角色/工具/写规则/铁律/差异段。"""
    p2 = prompts.executor_prompt(phase=2)
    # 角色：执行器 + 分诊层职责切分表述
    assert "执行器" in p2, "phase=2 应含执行器角色表述"
    assert "分诊层" in p2, "phase=2 应含分诊层表述"
    # 7 个只读工具名全在
    for name in READ_ONLY_TOOLS:
        assert name in p2, f"phase=2 缺少只读工具 {name}"
    # 写规则复用（_WRITE_PROMPT 原样复用：对照决定 + 跳过已处理）
    assert "alias_decisions" in p2, "phase=2 应复用写规则的 alias_decisions 段"
    assert "skip_processed" in p2, "phase=2 应复用写规则的 skip_processed 段"
    # 铁律段复用
    assert "铁律" in p2, "phase=2 应含铁律段"
    # 删掉分诊职责的判别 coaching
    assert "操作指导 vs 数据查询" not in p2, \
        "executor prompt 不应含「操作指导 vs 数据查询」判别段（分诊职责）"

    # phase=1：写工具段落不下发
    p1 = prompts.executor_prompt(phase=1)
    assert "create_batch" not in p1, "phase=1 不应含写工具 create_batch"
    for name in READ_ONLY_TOOLS:
        assert name in p1, f"phase=1 缺少只读工具 {name}"

    # system_prompt 未受影响（降级路径仍含判别段与写工具段）
    sp2 = prompts.system_prompt(phase=2)
    assert "操作指导 vs 数据查询" in sp2, \
        "system_prompt(phase=2) 应仍含「操作指导 vs 数据查询」段"
    assert "create_batch" in sp2, "system_prompt(phase=2) 应仍含写工具段"


def case_2_no_hint_uses_system_prompt() -> None:
    """门控：无 hint（triage 空剧本降级旧路）→ system_prompt 1 次、executor 0 次。"""
    sid = "DISP-EXEC-TEST-C2"
    set_scripts(
        triage_items=[],   # 空剧本：run_triage 返回 None 降级旧循环
        loop_items=[{"final_text": "降级路回复"}],
    )
    with spy_prompts() as counts:
        r = dispatcher.handle_message("查批次", session_id=sid)
    assert r["status"] == "ok" and r["message"] == "降级路回复", r
    assert counts == {"system": 1, "executor": 0}, \
        f"无 hint 应走 system_prompt: {counts}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"


def case_3_with_hint_uses_executor_prompt() -> None:
    """门控：高置信 action 带 hint → executor_prompt 1 次、system 0 次。"""
    sid = "DISP-EXEC-TEST-C3"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "rerun",
            "extracted_args": {"thread_id": "T1"},
            "confidence": 0.9,
        }],
        loop_items=[{"final_text": "执行器回复"}],
    )
    with spy_prompts() as counts:
        r = dispatcher.handle_message("重跑批次 T1", session_id=sid)
    assert r["status"] == "ok" and r["message"] == "执行器回复", r
    assert counts == {"system": 0, "executor": 1}, \
        f"带 hint 应走 executor_prompt: {counts}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"


def case_4_low_confidence_uses_system_prompt() -> None:
    """门控：低置信 action（0.5 不满足 >0.8）→ 旧路 system_prompt 1 次。"""
    sid = "DISP-EXEC-TEST-C4"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "rerun",
            "extracted_args": {"thread_id": "T1"},
            "confidence": 0.5,
        }],
        loop_items=[{"final_text": "旧循环处理结果"}],
    )
    with spy_prompts() as counts:
        r = dispatcher.handle_message("重跑批次 T1", session_id=sid)
    assert r["status"] == "ok" and r["message"] == "旧循环处理结果", r
    assert counts == {"system": 1, "executor": 0}, \
        f"低置信不应带 hint，应走 system_prompt: {counts}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"


def case_5_executor_tool_call_chain() -> None:
    """连通性：executor prompt 下只读工具调用链路完整（list_batches 免确认）。"""
    sid = "DISP-EXEC-TEST-C5"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "list_batches",
            "extracted_args": {},
            "confidence": 0.95,
        }],
        loop_items=[
            {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
            {"final_text": "共 0 个批次"},
        ],
    )
    with spy_prompts() as counts:
        r = dispatcher.handle_message("现在有哪些批次？", session_id=sid)
    assert r["status"] == "ok", r
    assert "批次" in r["message"], r
    assert counts == {"system": 0, "executor": 1}, \
        f"高置信 action 应走 executor_prompt: {counts}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"
    hist = sessions.get_session(sid).tool_history
    assert any(h["tool"] == "list_batches" for h in hist), \
        f"list_batches 应被真实执行: {hist}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. executor_prompt 内容（纯单元）", case_1_prompt_content),
    ("2. 门控：无 hint 降级 → system_prompt", case_2_no_hint_uses_system_prompt),
    ("3. 门控：高置信带 hint → executor_prompt", case_3_with_hint_uses_executor_prompt),
    ("4. 门控：低置信 → system_prompt", case_4_low_confidence_uses_system_prompt),
    ("5. executor prompt 下工具调用链路完整", case_5_executor_tool_call_chain),
]


def main() -> int:
    # 预热：全新环境 checkpoints.db 不存在时，service 以 mode=ro 打开会失败，
    # 先触发建库建表（与 dispatcher_triage_test 同款规避）
    get_graph().get_state(
        {"configurable": {"thread_id": "DISP-EXEC-TEST-WARMUP"}})

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
        print("🎉 调度 Agent Executor prompt 全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
