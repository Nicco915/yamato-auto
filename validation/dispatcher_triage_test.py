# -*- coding: utf-8 -*-
"""调度 Agent Triage 分诊路由测试（triage._TRIAGE_MOCK_SCRIPT + loop._MOCK_SCRIPT
双剧本注入，不调真实 LLM）。

覆盖（thread_id / session_id 一律用 DISP-TRIAGE-TEST- 前缀，与并行运行的
dispatcher_read/write 测试隔离）：
1. qa 直路由：triage 判 qa → ask_guide 直答，不进 loop，历史落 record_turn；
2. clarify 缺参：action/rerun 无 thread_id → 反问批次号，槽位落 session；
3. 两轮槽位合并：turn1 缺参反问、turn2 补齐 thread_id → 带 triage_hint 进
   loop（monkeypatch 捕获 hint），rerun 写工具走确认门 pending_confirmation；
4. 工具切换：旧 create_batch 槽位被 rerun 替换，旧参数不合并；
5. 中止：clarify + target_tool=None → 旧槽位清空，reply 透传；
6. 置信度边界：confidence=0.8（不满足 >0.8）+ 只读工具 → 走旧循环不带 hint
   （2026-08-04 黄灯区改造：0.8+写工具现在走黄灯确认式反问，见
   triage_soft_confirm_test.py；本用例改用只读工具 list_batches 保持
   「低置信走旧循环」的原测试意图）；
7. blocked 预览：create_batch 带不存在 folder 的 alias_decisions →
   业务硬校验拦截转 clarify（不出确认卡），槽位清空、pending_action 为空；
8. 开关关闭：DISPATCHER_TRIAGE=off 时 triage 剧本不被消费，直走旧循环；
9. mock 降级：triage 空剧本 → 与旧路一致（loop 剧本正常消费）。

剧本队列均为模块级全局（triage._TRIAGE_MOCK_SCRIPT / loop._MOCK_SCRIPT），
每个用例前一并 clear 再注入，用例间互不污染。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/dispatcher_triage_test.py

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---- env 前置（必须在 import app 之前；db 路径在 import 后隔离）----
os.environ.setdefault("EXTRACTION_MOCK", "1")   # 提取走 mock（本测试不跑图，防御性设置）
os.environ["DISPATCHER_MOCK"] = "1"             # 调度循环 + 分诊都走剧本，不调真实 LLM
os.environ["GUIDE_MOCK"] = "1"                  # ask_guide 走模板降级（确定性输出）

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import dispatcher  # noqa: E402
from app.dispatcher import loop, sessions, triage  # noqa: E402
from app.graph import get_graph  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_triage_test_", alias_map_copy=True)


def set_scripts(triage_items: list[dict] | None = None,
                loop_items: list[dict] | None = None) -> None:
    """清空两个剧本队列再注入（模块级全局，用例间必须隔离）。"""
    triage._TRIAGE_MOCK_SCRIPT.clear()
    triage._TRIAGE_MOCK_SCRIPT.extend(triage_items or [])
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(loop_items or [])


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_qa_direct() -> None:
    """qa 直路由：ask_guide 直接回答，loop 剧本不被消费，历史落 record_turn。"""
    sid = "DISP-TRIAGE-TEST-C1"
    question = "怎么审核挂起的批次？"
    set_scripts(
        triage_items=[{"intent": "qa", "confidence": 0.95}],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r = dispatcher.handle_message(question, session_id=sid)
    assert r["status"] == "ok", r
    assert r.get("intent") == "qa", r
    assert "references" in r and isinstance(r["references"], list), r
    assert r["message"], r
    assert len(loop._MOCK_SCRIPT) == 1, "qa 直路由不应消费 loop 剧本"
    hist = sessions.get_session(sid).history
    assert hist[-2] == {"role": "user", "content": question}, hist
    assert hist[-1] == {"role": "assistant", "content": r["message"]}, hist


def case_2_clarify_missing_param() -> None:
    """clarify 缺参：action/rerun 无 thread_id → 反问批次号，槽位落 session。"""
    sid = "DISP-TRIAGE-TEST-C2"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "rerun",
            "extracted_args": {},
            "reply_message": "请提供要重跑的批次号。",
            "confidence": 0.9,
        }],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r = dispatcher.handle_message("帮我重跑一下", session_id=sid)
    assert r["status"] == "ok", r
    assert r.get("intent") == "clarify", r
    assert "批次号" in r["message"], r
    session = sessions.get_session(sid)
    assert session.current_target_tool == "rerun", session.current_target_tool
    assert len(loop._MOCK_SCRIPT) == 1, "clarify 反问不应消费 loop 剧本"


def case_3_two_turn_slot_merge() -> None:
    """两轮槽位合并：turn2 补齐 thread_id → 带 hint 进 loop，rerun 走确认门。"""
    sid = "DISP-TRIAGE-TEST-C3"
    tid = "DISP-TRIAGE-TEST-C3-BATCH"
    # turn1：缺参反问（同用例 2），槽位先落 rerun / {}
    set_scripts(triage_items=[{
        "intent": "action", "target_tool": "rerun",
        "extracted_args": {},
        "reply_message": "请提供要重跑的批次号。",
        "confidence": 0.9,
    }])
    r1 = dispatcher.handle_message("帮我重跑一下", session_id=sid)
    assert r1.get("intent") == "clarify", r1
    assert sessions.get_session(sid).current_target_tool == "rerun"

    # turn2：补齐 thread_id → 高置信 action，带 hint 进 loop（写工具确认门拦截）
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "rerun",
            "extracted_args": {"thread_id": tid},
            "confidence": 0.95,
        }],
        loop_items=[{"tool_calls": [
            {"id": "c1", "name": "rerun", "args": {"thread_id": tid}},
        ]}],
    )
    captured: list[dict | None] = []
    orig = dispatcher.run_dispatch

    def spy(message, session, *, phase, session_id=None, on_progress=None,
            triage_hint=None):
        captured.append(triage_hint)
        return orig(message, session, phase=phase, session_id=session_id,
                    on_progress=on_progress, triage_hint=triage_hint)

    dispatcher.run_dispatch = spy
    try:
        r2 = dispatcher.handle_message(f"重跑批次 {tid}", session_id=sid)
    finally:
        dispatcher.run_dispatch = orig
    assert len(captured) == 1, f"run_dispatch 应被恰好调用一次: {captured}"
    hint = captured[0]
    assert hint is not None, "高置信 action 应带 triage_hint 进 loop"
    assert hint["target_tool"] == "rerun", hint
    assert hint["args"].get("thread_id") == tid, hint
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽（确实进了 loop）"
    # rerun 是写工具：确认门拦截 → pending_confirmation
    assert r2["status"] == "pending_confirmation", r2
    assert r2["action"]["tool"] == "rerun", r2
    session = sessions.get_session(sid)
    assert session.pending_action is not None \
        and session.pending_action["tool"] == "rerun", session.pending_action
    # 确认卡已出，槽位使命完成应清空
    assert session.current_target_tool is None and session.current_slots == {}, \
        (session.current_target_tool, session.current_slots)


def case_4_tool_switch() -> None:
    """工具切换：旧 create_batch 槽位被 rerun 替换，旧参数不合并进来。"""
    sid = "DISP-TRIAGE-TEST-C4"
    session = sessions.get_session(sid)
    sessions.set_slots(session, "create_batch",
                       {"thread_id": "DISP-TRIAGE-TEST-C4-OLD"})
    set_scripts(triage_items=[{
        "intent": "action", "target_tool": "rerun",
        "extracted_args": {},
        "reply_message": "请提供要重跑的批次号。",
        "confidence": 0.9,
    }])
    r = dispatcher.handle_message("算了，帮我重跑一个批次", session_id=sid)
    assert r.get("intent") == "clarify", r
    assert session.current_target_tool == "rerun", session.current_target_tool
    assert "thread_id" not in session.current_slots, \
        f"旧 create_batch 参数不应合并进 rerun 槽位: {session.current_slots}"


def case_5_abort() -> None:
    """中止：槽位存在时 clarify + target_tool=None → 槽位清空，reply 透传。"""
    sid = "DISP-TRIAGE-TEST-C5"
    session = sessions.get_session(sid)
    sessions.set_slots(session, "create_batch",
                       {"thread_id": "DISP-TRIAGE-TEST-C5-OLD"})
    set_scripts(
        triage_items=[{
            "intent": "clarify", "target_tool": None,
            "reply_message": "好的，已取消本次操作。",
            "confidence": 0.9,
        }],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r = dispatcher.handle_message("不用了，取消", session_id=sid)
    assert r["status"] == "ok", r
    assert r.get("intent") == "clarify", r
    assert r["message"] == "好的，已取消本次操作。", r
    assert session.current_target_tool is None and session.current_slots == {}, \
        (session.current_target_tool, session.current_slots)
    assert len(loop._MOCK_SCRIPT) == 1, "中止不应消费 loop 剧本"


def case_6_confidence_boundary() -> None:
    """置信度边界：confidence=0.8（不满足 >0.8）+ 只读工具 → 走旧循环不带 hint。

    2026-08-04 黄灯区改造：0.8+写工具（原 rerun 剧本）在新行为下走黄灯
    确认式反问，本用例改用只读工具 list_batches 保持「低置信走旧循环」
    的原测试意图；写工具黄灯行为由 triage_soft_confirm_test.py 覆盖。
    """
    sid = "DISP-TRIAGE-TEST-C6"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "list_batches",
            "extracted_args": {},
            "confidence": 0.8,
        }],
        loop_items=[{"final_text": "旧循环处理结果"}],
    )
    captured: list[dict | None] = []
    orig = dispatcher.run_dispatch

    def spy(message, session, *, phase, session_id=None, on_progress=None,
            triage_hint=None):
        captured.append(triage_hint)
        return orig(message, session, phase=phase, session_id=session_id,
                    on_progress=on_progress, triage_hint=triage_hint)

    dispatcher.run_dispatch = spy
    try:
        r = dispatcher.handle_message("现在有哪些批次？", session_id=sid)
    finally:
        dispatcher.run_dispatch = orig
    assert r["status"] == "ok" and r["message"] == "旧循环处理结果", r
    assert captured == [None], f"0.8 不满足 >0.8，不应带 hint: {captured}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"


def case_7_blocked_preview() -> None:
    """blocked 预览：alias_decisions 的 folder 不存在 → 硬校验拦截转 clarify。"""
    import pandas as pd

    sid = "DISP-TRIAGE-TEST-C7"
    tid = "DISP-TRIAGE-TEST-C7-BATCH"
    factory = "テスト工場A"
    # 最小上游/下游：下游含 MAKER_MEI_KJ + SHOHIN_CD 两列；上游只有一个正规文件夹
    fixture = Path(tempfile.mkdtemp(prefix="yamato_triage_c7_"))
    downstream = fixture / "downstream.xlsx"
    pd.DataFrame({
        "MAKER_MEI_KJ": [factory],
        "SHOHIN_CD": ["4901234567890"],
    }).to_excel(downstream, index=False)
    upstream = fixture / "upstream"
    (upstream / "正規フォルダ").mkdir(parents=True)

    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {"thread_id": tid},
            "confidence": 0.95,
        }],
        loop_items=[{"tool_calls": [{
            "id": "c1", "name": "create_batch",
            "args": {
                "thread_id": tid,
                "downstream_file_path": str(downstream),
                "upstream_root": str(upstream),
                "alias_decisions": [
                    {"factory": factory, "folder": "存在しないフォルダ",
                     "save": False},
                ],
            },
        }]}],
    )
    r = dispatcher.handle_message(f"创建批次 {tid}", session_id=sid)
    assert r["status"] == "ok", r
    assert r.get("clarify") is True, f"blocked 预览应转 clarify: {r}"
    assert "操作无法发起" in r["message"], r
    session = sessions.get_session(sid)
    assert session.pending_action is None, \
        f"blocked 不应出确认卡: {session.pending_action}"
    assert session.current_target_tool is None and session.current_slots == {}, \
        (session.current_target_tool, session.current_slots)
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"


def case_8_switch_off() -> None:
    """开关关闭：DISPATCHER_TRIAGE=off 时 triage 剧本不被消费，直走旧循环。"""
    sid = "DISP-TRIAGE-TEST-C8"
    os.environ["DISPATCHER_TRIAGE"] = "off"
    try:
        set_scripts(
            triage_items=[{"intent": "qa", "confidence": 0.99}],
            loop_items=[{"final_text": "旧路回复"}],
        )
        r = dispatcher.handle_message("随便说点什么", session_id=sid)
        assert r["status"] == "ok" and r["message"] == "旧路回复", r
        assert len(triage._TRIAGE_MOCK_SCRIPT) == 1, \
            "开关关闭时 triage 剧本不应被消费"
        assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"
    finally:
        os.environ.pop("DISPATCHER_TRIAGE", None)


def case_9_mock_degrade() -> None:
    """mock 降级：triage 空剧本 → 与旧路一致（loop 剧本正常消费）。"""
    sid = "DISP-TRIAGE-TEST-C9"
    set_scripts(
        triage_items=[],   # 空剧本：run_triage 返回 None 降级旧循环
        loop_items=[
            {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
            {"final_text": "旧路问答结果"},
        ],
    )
    r = dispatcher.handle_message("现在有哪些批次？", session_id=sid)
    assert r["status"] == "ok" and r["message"] == "旧路问答结果", r
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽"
    hist = sessions.get_session(sid).tool_history
    assert any(h["tool"] == "list_batches" for h in hist), hist


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. qa 直路由（不进 loop）", case_1_qa_direct),
    ("2. clarify 缺参反问（槽位落 session）", case_2_clarify_missing_param),
    ("3. 两轮槽位合并（带 hint 进 loop + 确认门）", case_3_two_turn_slot_merge),
    ("4. 工具切换（旧槽位作废）", case_4_tool_switch),
    ("5. 中止（槽位清空 + reply 透传）", case_5_abort),
    ("6. 置信度边界 0.8 + 只读工具（不带 hint）", case_6_confidence_boundary),
    ("7. blocked 预览转 clarify", case_7_blocked_preview),
    ("8. 开关关闭（triage 剧本不消费）", case_8_switch_off),
    ("9. mock 空剧本降级旧路", case_9_mock_degrade),
]


def main() -> int:
    # 预热：全新环境 checkpoints.db 不存在时，service 以 mode=ro 打开会失败，
    # 先触发建库建表（与 dispatcher_read_test 同款规避）
    get_graph().get_state(
        {"configurable": {"thread_id": "DISP-TRIAGE-TEST-WARMUP"}})

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
        print("🎉 调度 Agent Triage 分诊路由全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
