# -*- coding: utf-8 -*-
"""操作指导工具测试（DISPATCHER_MOCK=1 + GUIDE_MOCK=1，确定性验证）。

覆盖：
1. ask_guide 接口正常（返回 answer/references/context）；
2. 知识库匹配正确（"怎么重跑" → rerun_batch）；
3. 未知问题走通用模板（_generic）；
4. 调度 Agent 正确调用 ask_guide（tool_history 有记录）；
5. 真实 LLM 调用（关闭 mock，验证端到端）。

用法：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/dispatcher_guide_test.py
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/dispatcher_guide_test.py

用例 4/5 经 _react_script.run_dispatch 直调 react 引擎本体，
剧本经 _react_script.set_scripts 注入 mock 通道。

隔离（血泪红线）：checkpoint/master db、output、sessions 目录全部指向
临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"
os.environ["GUIDE_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.dispatcher import tools  # noqa: E402
from app.dispatcher.guide import ask_guide  # noqa: E402
from app.dispatcher.sessions import DispatcherSession  # noqa: E402

from _react_script import run_dispatch, set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_guide_test_")


def case_1_interface() -> None:
    """ask_guide 接口正常。"""
    r = ask_guide("怎么用这个系统？")
    assert "answer" in r and "references" in r and "context" in r, r
    assert isinstance(r["answer"], str) and r["answer"]
    assert isinstance(r["references"], list)
    print(f"  ✓ 接口正常：answer={r['answer'][:60]}... references={r['references']}")


def case_2_kb_match() -> None:
    """知识库匹配正确。"""
    r1 = ask_guide("怎么重跑批次？")
    assert any("rerun" in ref for ref in r1["references"]), r1
    print(f"  ✓ '怎么重跑' → {r1['references']}")

    r2 = ask_guide("最佳实践有哪些？")
    assert any("best" in ref for ref in r2["references"]), r2
    print(f"  ✓ '最佳实践' → {r2['references']}")


def case_3_unknown_fallback() -> None:
    """未知问题走通用模板。"""
    r = ask_guide("今天天气怎么样？")
    assert "_generic" in r["references"] or not r["references"], r
    assert r["answer"]  # 仍有回答
    print(f"  ✓ 未知问题 → {r['references']}，仍有回答")


def case_4_dispatcher_call() -> None:
    """调度 Agent 正确调用 ask_guide（直调当前引擎的主循环）。"""
    session = DispatcherSession()
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "ask_guide",
                         "args": {"question": "怎么发起批次？"}}]},
        {"final_text": "根据操作指引，发起批次很简单..."},
    ])
    r = run_dispatch("怎么发起批次？", session, phase=1)
    assert r["status"] == "ok", r
    assert len(session.tool_history) == 1, session.tool_history
    assert session.tool_history[0]["tool"] == "ask_guide"
    print(f"  ✓ 调度 Agent 调用了 ask_guide，tool_history 有记录")


def case_5_real_llm() -> None:
    """真实 LLM 调用（关闭 mock，直调当前引擎的主循环）。"""
    os.environ["DISPATCHER_MOCK"] = "0"
    os.environ.pop("GUIDE_MOCK", None)
    try:
        session = DispatcherSession()
        r = run_dispatch("怎么发起新批次？", session, phase=1)
        assert r["status"] == "ok", r
        assert len(session.tool_history) == 1
        assert session.tool_history[0]["tool"] == "ask_guide"
        assert "发起" in r.get("message", "") or "批次" in r.get("message", "")
        print(f"  ✓ 真实 LLM 调用成功：{r['message'][:80]}...")
    finally:
        os.environ["DISPATCHER_MOCK"] = "1"
        os.environ["GUIDE_MOCK"] = "1"


CASES = [
    ("1. ask_guide 接口正常", case_1_interface),
    ("2. 知识库匹配正确", case_2_kb_match),
    ("3. 未知问题走通用模板", case_3_unknown_fallback),
    ("4. 调度 Agent 正确调用 ask_guide", case_4_dispatcher_call),
    ("5. 真实 LLM 调用", case_5_real_llm),
]


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name, fn in CASES:
        print(f"===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
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
        print("🎉 操作指导工具测试全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
