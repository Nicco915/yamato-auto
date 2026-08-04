# -*- coding: utf-8 -*-
"""调度 Agent Triage 软路由黄灯区测试（triage._TRIAGE_MOCK_SCRIPT +
loop._MOCK_SCRIPT 双剧本注入，不调真实 LLM）。

黄灯区设计（2026-08-04，agent设计/triage升级方案_软路由_fewshot_链式续跑）：
action 意图按置信度三级路由——绿灯（>0.8）带 hint 进 loop；黄灯
（0.6–0.8 且目标是写工具）存 soft_pending 软挂起 + 回复确认式反问，
不进 loop；黄灯豁免（0.6–0.8 只读工具）与红灯（<0.6）维持旧循环。
软挂起一轮有效：下一条消息"是"→ 按已存意图带 hint 直接进 loop
（不再过 triage）；"算了"→ 状态全清；其他消息 → 清挂起后正常分诊。

覆盖（thread_id / session_id 一律用 DISP-SOFT-TEST- 前缀，与并行运行的
其他 dispatcher 测试隔离）：
1. 黄灯写工具：action/create_batch/conf=0.7 → 回复确认式反问（缺省代码
   拼装），soft_pending 落 session，loop 剧本不被消费；
2. 软确认：第二轮"是"（triage 剧本为空）→ 带 hint 进 loop（monkeypatch
   捕获 hint），create_batch 出确认卡 pending_confirmation；
3. 软否定：第二轮"算了" → soft_pending 与槽位全清，回复取消文案，
   triage 剧本不被消费（turn1 的 reply_message 优先透传）；
4. 换话题：第二轮新问题 → soft_pending 清除，正常消费该轮 triage 剧本；
5. 黄灯只读：conf=0.7 + list_batches → 不拦截，走旧循环（loop 剧本被消费）；
6. 绿灯回归：conf=0.9 rerun → 直接带 hint 进 loop 出确认卡。

剧本队列均为模块级全局（triage._TRIAGE_MOCK_SCRIPT / loop._MOCK_SCRIPT），
每个用例前一并 clear 再注入，用例间互不污染。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/triage_soft_confirm_test.py

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
# 本文件测 triage 分诊层（legacy 引擎专属机制），无论外部环境变量如何都钉死
# legacy——防止全量套件以 DISPATCHER_ENGINE=react 外导时被误跑
os.environ["DISPATCHER_ENGINE"] = "legacy"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import dispatcher  # noqa: E402
from app.dispatcher import loop, sessions, triage  # noqa: E402
from app.graph import get_graph  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_soft_confirm_test_", alias_map_copy=True)


def set_scripts(triage_items: list[dict] | None = None,
                loop_items: list[dict] | None = None) -> None:
    """清空两个剧本队列再注入（模块级全局，用例间必须隔离）。"""
    triage._TRIAGE_MOCK_SCRIPT.clear()
    triage._TRIAGE_MOCK_SCRIPT.extend(triage_items or [])
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(loop_items or [])


def spy_run_dispatch(captured: list[dict | None]):
    """monkeypatch dispatcher.run_dispatch，捕获每次调用的 triage_hint。"""
    orig = dispatcher.run_dispatch

    def spy(message, session, *, phase, session_id=None, on_progress=None,
            triage_hint=None):
        captured.append(triage_hint)
        return orig(message, session, phase=phase, session_id=session_id,
                    on_progress=on_progress, triage_hint=triage_hint)

    return orig, spy


def make_batch_fixture(prefix: str) -> tuple[str, str]:
    """create_batch 预览用最小上游/下游 fixture（同 dispatcher_triage_test
    case_7）：下游含 MAKER_MEI_KJ + SHOHIN_CD 两列，上游一个正规文件夹。"""
    import pandas as pd

    fixture = Path(tempfile.mkdtemp(prefix=prefix))
    downstream = fixture / "downstream.xlsx"
    pd.DataFrame({
        "MAKER_MEI_KJ": ["テスト工場A"],
        "SHOHIN_CD": ["4901234567890"],
    }).to_excel(downstream, index=False)
    upstream = fixture / "upstream"
    (upstream / "正規フォルダ").mkdir(parents=True)
    return str(downstream), str(upstream)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_yellow_write_tool() -> None:
    """黄灯写工具：conf=0.7 + create_batch → 确认式反问，软挂起落 session，
    loop 剧本不被消费。"""
    sid = "DISP-SOFT-TEST-C1"
    tid = "DISP-SOFT-TEST-C1-BATCH"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {"thread_id": tid},
            # 不给 reply_message：验证缺省时代码拼装的确认式反问
            "confidence": 0.7,
        }],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r = dispatcher.handle_message("把那个批次处理一下吧", session_id=sid)
    assert r["status"] == "ok", r
    assert r.get("intent") == "soft_confirm", r
    assert "发起新批次" in r["message"] and tid in r["message"], r
    assert "是" in r["message"], r
    session = sessions.get_session(sid)
    assert session.soft_pending is not None, "黄灯应落 soft_pending"
    assert session.soft_pending["target_tool"] == "create_batch", \
        session.soft_pending
    assert session.soft_pending["slots"] == {"thread_id": tid}, \
        session.soft_pending
    assert session.soft_pending["armed"] is True, session.soft_pending
    # 槽位同步落 session（与绿灯一致的暂存语义）
    assert session.current_target_tool == "create_batch", \
        session.current_target_tool
    assert len(loop._MOCK_SCRIPT) == 1, "黄灯不应消费 loop 剧本"
    hist = session.history
    assert hist[-1] == {"role": "assistant", "content": r["message"]}, hist


def case_2_soft_confirm_yes() -> None:
    """软确认：第二轮"是" → 不再过 triage，带 hint 进 loop，create_batch
    出确认卡 pending_confirmation。"""
    sid = "DISP-SOFT-TEST-C2"
    tid = "DISP-SOFT-TEST-C2-BATCH"
    # turn1：黄灯反问（同用例 1）
    set_scripts(triage_items=[{
        "intent": "action", "target_tool": "create_batch",
        "extracted_args": {"thread_id": tid},
        "confidence": 0.7,
    }])
    r1 = dispatcher.handle_message("把那个批次处理一下吧", session_id=sid)
    assert r1.get("intent") == "soft_confirm", r1
    assert sessions.get_session(sid).soft_pending is not None

    # turn2："是" → 入口短路消费 soft_pending，triage 剧本为空也不过 triage
    downstream, upstream = make_batch_fixture("yamato_soft_c2_")
    set_scripts(
        triage_items=[],   # 空剧本：若误过 triage 会降级旧循环（hint=None）
        loop_items=[{"tool_calls": [{
            "id": "c1", "name": "create_batch",
            "args": {
                "thread_id": tid,
                "downstream_file_path": downstream,
                "upstream_root": upstream,
            },
        }]}],
    )
    captured: list[dict | None] = []
    orig, spy = spy_run_dispatch(captured)
    dispatcher.run_dispatch = spy
    try:
        r2 = dispatcher.handle_message("是", session_id=sid)
    finally:
        dispatcher.run_dispatch = orig
    assert len(captured) == 1, f"run_dispatch 应被恰好调用一次: {captured}"
    hint = captured[0]
    assert hint is not None, "软确认应带 triage_hint 进 loop（不过 triage）"
    assert hint["target_tool"] == "create_batch", hint
    assert hint["args"] == {"thread_id": tid}, hint
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽（确实进了 loop）"
    # create_batch 是写工具：确认门拦截 → pending_confirmation
    assert r2["status"] == "pending_confirmation", r2
    assert r2["action"]["tool"] == "create_batch", r2
    session = sessions.get_session(sid)
    assert session.soft_pending is None, "软确认后 soft_pending 应清除"
    assert session.pending_action is not None \
        and session.pending_action["tool"] == "create_batch", \
        session.pending_action


def case_3_soft_deny() -> None:
    """软否定：第二轮"算了" → soft_pending 与槽位全清，回复取消文案，
    triage 剧本不被消费。"""
    sid = "DISP-SOFT-TEST-C3"
    tid = "DISP-SOFT-TEST-C3-BATCH"
    # turn1：黄灯反问（带 reply_message，验证 triage 反问优先透传）
    set_scripts(triage_items=[{
        "intent": "action", "target_tool": "create_batch",
        "extracted_args": {"thread_id": tid},
        "reply_message": f"您是指要为批次 {tid} 发起新批次吗？",
        "confidence": 0.65,
    }])
    r1 = dispatcher.handle_message("把那个批次处理一下吧", session_id=sid)
    assert r1.get("intent") == "soft_confirm", r1
    assert r1["message"] == f"您是指要为批次 {tid} 发起新批次吗？", r1

    # turn2："算了" → 短路否定分支（塞一条 triage 剧本哨兵验证不被消费）
    set_scripts(
        triage_items=[{"intent": "qa", "confidence": 0.99}],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r2 = dispatcher.handle_message("算了", session_id=sid)
    assert r2["status"] == "ok", r2
    assert r2.get("intent") == "soft_cancel", r2
    assert "已取消" in r2["message"], r2
    session = sessions.get_session(sid)
    assert session.soft_pending is None, "软否定后 soft_pending 应清除"
    assert session.current_target_tool is None and session.current_slots == {}, \
        (session.current_target_tool, session.current_slots)
    assert len(triage._TRIAGE_MOCK_SCRIPT) == 1, "软否定不应消费 triage 剧本"
    assert len(loop._MOCK_SCRIPT) == 1, "软否定不应消费 loop 剧本"


def case_4_topic_switch() -> None:
    """换话题：第二轮新问题 → soft_pending 清除，正常消费该轮 triage 剧本。"""
    sid = "DISP-SOFT-TEST-C4"
    tid = "DISP-SOFT-TEST-C4-BATCH"
    # turn1：黄灯反问
    set_scripts(triage_items=[{
        "intent": "action", "target_tool": "create_batch",
        "extracted_args": {"thread_id": tid},
        "confidence": 0.7,
    }])
    r1 = dispatcher.handle_message("把那个批次处理一下吧", session_id=sid)
    assert r1.get("intent") == "soft_confirm", r1

    # turn2：新问题（非确认/否定短答）→ 清挂起，正常走 triage（qa 直答）
    set_scripts(
        triage_items=[{"intent": "qa", "confidence": 0.95}],
        loop_items=[{"final_text": "不应被消费"}],
    )
    r2 = dispatcher.handle_message("怎么审核挂起的批次？", session_id=sid)
    assert r2["status"] == "ok", r2
    assert r2.get("intent") == "qa", r2
    session = sessions.get_session(sid)
    assert session.soft_pending is None, "换话题后 soft_pending 应清除"
    assert triage._TRIAGE_MOCK_SCRIPT == [], "新消息应正常消费 triage 剧本"
    assert len(loop._MOCK_SCRIPT) == 1, "qa 直答不应消费 loop 剧本"


def case_5_yellow_readonly() -> None:
    """黄灯只读：conf=0.7 + list_batches → 不拦截，不带 hint 走旧循环。"""
    sid = "DISP-SOFT-TEST-C5"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "list_batches",
            "extracted_args": {},
            "confidence": 0.7,
        }],
        loop_items=[{"final_text": "旧循环处理结果"}],
    )
    captured: list[dict | None] = []
    orig, spy = spy_run_dispatch(captured)
    dispatcher.run_dispatch = spy
    try:
        r = dispatcher.handle_message("现在有哪些批次来着？", session_id=sid)
    finally:
        dispatcher.run_dispatch = orig
    assert r["status"] == "ok" and r["message"] == "旧循环处理结果", r
    assert captured == [None], f"黄灯只读不应带 hint: {captured}"
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽（走了旧循环）"
    session = sessions.get_session(sid)
    assert session.soft_pending is None, "黄灯只读不应落 soft_pending"


def case_6_green_regression() -> None:
    """绿灯回归：conf=0.9 rerun → 直接带 hint 进 loop，确认门出预览卡。"""
    sid = "DISP-SOFT-TEST-C6"
    tid = "DISP-SOFT-TEST-C6-BATCH"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "rerun",
            "extracted_args": {"thread_id": tid},
            "confidence": 0.9,
        }],
        loop_items=[{"tool_calls": [
            {"id": "c1", "name": "rerun", "args": {"thread_id": tid}},
        ]}],
    )
    captured: list[dict | None] = []
    orig, spy = spy_run_dispatch(captured)
    dispatcher.run_dispatch = spy
    try:
        r = dispatcher.handle_message(f"重跑批次 {tid}", session_id=sid)
    finally:
        dispatcher.run_dispatch = orig
    assert captured and captured[0] is not None, "绿灯应带 hint 进 loop"
    assert captured[0]["target_tool"] == "rerun", captured
    assert r["status"] == "pending_confirmation", r
    session = sessions.get_session(sid)
    assert session.soft_pending is None, "绿灯不应落 soft_pending"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. 黄灯写工具（确认式反问 + 软挂起，不进 loop）", case_1_yellow_write_tool),
    ("2. 软确认（「是」→ 带 hint 进 loop 出确认卡）", case_2_soft_confirm_yes),
    ("3. 软否定（「算了」→ 状态全清 + 取消文案）", case_3_soft_deny),
    ("4. 换话题（清挂起 + 正常消费 triage 剧本）", case_4_topic_switch),
    ("5. 黄灯只读（不拦截，旧循环）", case_5_yellow_readonly),
    ("6. 绿灯回归（0.9 直接进 loop）", case_6_green_regression),
]


def main() -> int:
    # 预热：全新环境 checkpoints.db 不存在时，service 以 mode=ro 打开会失败，
    # 先触发建库建表（与 dispatcher_triage_test 同款规避）
    get_graph().get_state(
        {"configurable": {"thread_id": "DISP-SOFT-TEST-WARMUP"}})

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
        print("🎉 调度 Agent Triage 软路由黄灯区全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
