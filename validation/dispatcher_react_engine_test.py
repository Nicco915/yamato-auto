# -*- coding: utf-8 -*-
"""调度 Agent react 引擎（create_react_agent）端到端测试。

DISPATCHER_ENGINE=react + DISPATCHER_MOCK=1，经 lc_llm.set_script 注入
确定性剧本（注入型工具的影子写/反问在 ToolNode 内以完整 ToolCall 调用，
剧本里的 tool_calls 无需给 id——lc_llm mock 自动补 mockcall_N）。

覆盖（session_id 一律用 DISP-REACT-TEST- 前缀，thread_id 用 YM-REACT- 前缀，
与并行运行的其他 dispatcher 测试隔离）：
1. 只读链路：get_usage 调用 + 最终回复 + tool_history 审计；
2. 写工具确认门：create_batch 影子工具出确认卡（绝不执行）→ confirm 执行；
3. 黄灯三轮：request_clarification 布防 → 「是」不进 LLM 直接出确认卡
   → confirm 执行；
4. 黄灯否定：布防后「算了」→ soft_cancel + soft_pending 清空；
5. 并行双写只出一卡：同轮 create_batch + rerun，一次一确认恰好一卡；
6. 超轮兜底：剧本全是无终复的只读工具调用 → GraphRecursionError 兜底文案；
7. legacy 回归：切 DISPATCHER_ENGINE=legacy 跑旧路径（triage 空剧本降级
   + loop 剧本 final_text），确认旧路未破。

用法（在 app/ 目录下）：
  python3 validation/dispatcher_react_engine_test.py

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"
os.environ["DISPATCHER_ENGINE"] = "react"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.dispatcher import lc_llm, loop, sessions  # noqa: E402
from app.graph import get_graph  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_react_engine_test_", alias_map_copy=True)


# ---- 真实 fixture（复用 dispatcher_write_test 同款数据）----
REAL_ROOT = os.environ.get("YAMATO_TEST_REAL_ROOT", "/Users/nz/Downloads/yamato/96/工厂")
DOWNSTREAM = os.environ.get(
    "YAMATO_TEST_DOWNSTREAM",
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
)
EMPTY_DIR = Path(tempfile.mkdtemp(prefix="yamato_react_empty_"))


def with_lock_retry(fn):
    """sqlite 共享文件瞬时 lock：sleep 1s 重试一次。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        if "lock" in str(e).lower():
            time.sleep(1)
            return fn()
        raise


def fresh_session_id(tag: str) -> str:
    return f"DISP-REACT-TEST-{tag}-{int(time.time()*1000)}"


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_readonly_chain() -> None:
    """只读链路：get_usage 被调用，最终回复正确，tool_history 有审计记录。"""
    sid = fresh_session_id("C1")
    lc_llm.set_script([
        {"tool_calls": [{"name": "get_usage", "args": {}}]},
        {"final_text": "用量如下"},
    ])
    # 「查一下用量」等短查询现在走快路径（fastpath，不进 LLM）；
    # 本用例验证 LLM 链路，故用快路径不放行的问法
    r = dispatcher.handle_message("帮我查一下当前的调用用量和失败次数", session_id=sid)
    assert r["status"] == "ok", r
    assert r["message"] == "用量如下", r
    assert r.get("session_id") == sid, r
    sess = sessions.get_session(sid)
    assert any(t["tool"] == "get_usage" for t in sess.tool_history), \
        f"tool_history 应有 get_usage 记录: {sess.tool_history}"
    # 对话历史已写入
    assert sess.history[-1] == {"role": "assistant", "content": "用量如下"}
    print("  ✓ 只读链路：get_usage 已调用并审计，最终回复正确")


def case_2_write_tool_confirm_gate() -> str:
    """写工具确认门：create_batch 影子工具出确认卡不执行 → confirm 执行成功。"""
    sid = fresh_session_id("C2")
    tid = "YM-REACT-T1"
    lc_llm.set_script([
        {"tool_calls": [{"name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_DIR)}}]},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        f"发起批次 {tid} 用空目录", session_id=sid))
    assert r["status"] == "pending_confirmation", r
    assert r["action"]["kind"] == "dispatcher_tool", r
    assert r["action"]["tool"] == "create_batch", r
    assert r["action"]["args"]["thread_id"] == tid, r
    assert "请确认是否执行" in r["message"], r
    # session 留存了 pending action，批次绝未执行
    sess = sessions.get_session(sid)
    assert sess.pending_action is not None
    assert sess.pending_action["tool"] == "create_batch"
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert state["exists"] is False, f"批次不应已创建: {state}"
    print(f"  ✓ 确认门：pending_confirmation，{tid} 未创建，"
          f"session.pending_action 已存")
    # confirm 执行（EXTRACTION_MOCK 下 create_batch execute 可跑）
    r2 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r2["status"] == "applied", r2
    assert r2["tool"] == "create_batch", r2
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert state["exists"] is True, f"批次应已创建: {state}"
    print(f"  ✓ confirm：status=applied，批次 {tid} 已创建挂起")
    return tid


def case_3_soft_confirm_three_turns(tid: str) -> None:
    """黄灯三轮：request_clarification 布防 → 「是」短路出确认卡 → confirm。"""
    sid = fresh_session_id("C3")
    # turn1：模型调 request_clarification（黄灯反问），布防 soft_pending
    lc_llm.set_script([
        {"tool_calls": [{"name": "request_clarification",
                         "args": {"target_action": "rerun",
                                  "args": {"thread_id": tid},
                                  "question": "指代不清"}}]},
    ])
    r1 = dispatcher.handle_message("把那个批次再跑一下", session_id=sid)
    assert r1["status"] == "ok", r1
    assert r1.get("intent") == "soft_confirm", r1
    sess = sessions.get_session(sid)
    assert sess.soft_pending is not None, "soft_pending 应已布防"
    assert sess.soft_pending["target_tool"] == "rerun"
    assert sess.soft_pending["slots"]["thread_id"] == tid
    print(f"  ✓ turn1 布防：intent=soft_confirm，soft_pending 已存")

    # turn2：发「是」——剧本为空，不进 LLM（lc_llm 剧本零消耗：空剧本下
    # 任何 LLM 调用都会返回「[mock] 剧本已用尽」文本，绝不可能产出
    # pending_confirmation）
    lc_llm.clear_script()
    r2 = with_lock_retry(lambda: dispatcher.handle_message("是", session_id=sid))
    assert lc_llm._SCRIPT == [], "软确认消费不得消耗 LLM 剧本"
    assert r2["status"] == "pending_confirmation", r2
    assert r2["action"]["tool"] == "rerun", r2
    assert r2["action"]["args"]["thread_id"] == tid, r2
    assert sess.soft_pending is None, "soft_pending 应已消费清空"
    print(f"  ✓ turn2 短路：不进 LLM，直接出 rerun 确认卡")

    # turn3：confirm 执行（YM-REACT-T1 已在 case 2 创建挂起，rerun 可跑）
    r3 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r3["status"] == "applied", r3
    assert r3["tool"] == "rerun", r3
    print(f"  ✓ turn3 confirm：rerun status=applied")


def case_4_soft_confirm_cancel() -> None:
    """黄灯否定：重新布防后发「算了」→ soft_cancel + soft_pending 清空。"""
    sid = fresh_session_id("C4")
    lc_llm.set_script([
        {"tool_calls": [{"name": "request_clarification",
                         "args": {"target_action": "create_batch",
                                  "args": {"thread_id": "YM-REACT-C4"},
                                  "question": "指代不清"}}]},
    ])
    r1 = dispatcher.handle_message("处理一下那个吧", session_id=sid)
    assert r1.get("intent") == "soft_confirm", r1
    sess = sessions.get_session(sid)
    assert sess.soft_pending is not None

    lc_llm.clear_script()
    r2 = dispatcher.handle_message("算了", session_id=sid)
    assert r2["status"] == "ok", r2
    assert r2.get("intent") == "soft_cancel", r2
    assert sess.soft_pending is None, "soft_pending 应已清空"
    assert sess.pending_action is None, "不应产出待确认操作"
    print("  ✓ 黄灯否定：intent=soft_cancel，soft_pending 已清空")


def case_5_parallel_double_write_single_card() -> None:
    """并行双写只出一卡：同轮 create_batch + rerun，一次一确认只成一卡。

    ToolNode 对同一条 AIMessage 的多个工具调用是线程池并发执行的
    （executor.map），两个写影子工具谁先在锁内存卡谁胜（lc_tools 的
    双重检查防线），因此本用例只断言「恰好一卡 + pending 单个」的
    一次一确认不变量，不锁定胜者是哪一个（并发语义下「先到」无定义）。
    """
    sid = fresh_session_id("C5")
    lc_llm.set_script([
        {"tool_calls": [
            {"name": "create_batch",
             "args": {"thread_id": "YM-P1",
                      "downstream_file_path": DOWNSTREAM,
                      "upstream_root": str(EMPTY_DIR)}},
            {"name": "rerun", "args": {"thread_id": "YM-P2"}},
        ]},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        "发起 YM-P1 并重跑 YM-P2", session_id=sid))
    assert r["status"] == "pending_confirmation", r
    sess = sessions.get_session(sid)
    # 一次一确认：pending_action 是单个信封，且是两者之一
    assert isinstance(sess.pending_action, dict), "pending_action 应为单个信封"
    assert sess.pending_action["tool"] in ("create_batch", "rerun"), \
        sess.pending_action
    assert r["action"] is sess.pending_action
    # 工具审计流水中恰好一条「待人工确认」（另一个被一次一确认拒绝，
    # 不写状态）
    pending_records = [t for t in sess.tool_history
                       if t["result_summary"] == "待人工确认"]
    assert len(pending_records) == 1, \
        f"应恰好一张确认卡: {sess.tool_history}"
    assert pending_records[0]["tool"] == sess.pending_action["tool"]
    print(f"  ✓ 并行双写：恰好一卡（{sess.pending_action['tool']} 胜出），"
          f"pending_action 单个（一次一确认）")


def case_6_recursion_fallback() -> None:
    """超轮兜底：剧本全是无终复的只读工具调用 → GraphRecursionError 兜底。"""
    sid = fresh_session_id("C6")
    # recursion_limit=12，剧本给够（每个 react 步消耗一条）
    lc_llm.set_script([
        {"tool_calls": [{"name": "get_usage", "args": {}}]}
        for _ in range(15)
    ])
    r = dispatcher.handle_message("反复查用量", session_id=sid)
    assert r["status"] == "ok", r
    assert r["message"] == "处理步骤过多，请把问题拆小一点再问。", r
    print("  ✓ 超轮兜底：GraphRecursionError → 拆分提示文案")


def case_7_legacy_regression() -> None:
    """legacy 回归：切 DISPATCHER_ENGINE=legacy，旧路径（triage 空剧本降级
    + loop 剧本 final_text）行为不变。"""
    sid = fresh_session_id("C7")
    os.environ["DISPATCHER_ENGINE"] = "legacy"
    try:
        # triage 空剧本 → run_triage 返回 None → 降级旧 loop 循环；
        # loop 剧本第一条即 final_text → 直接终复
        lc_llm.clear_script()
        loop._MOCK_SCRIPT.clear()
        loop._MOCK_SCRIPT.append({"final_text": "旧路兜底回复"})
        r = dispatcher.handle_message("随便问一句", session_id=sid)
        assert r["status"] == "ok", r
        assert r["message"] == "旧路兜底回复", r
        assert r.get("session_id") == sid, r
        print("  ✓ legacy 回归：triage 降级 + loop 循环行为不变")
    finally:
        os.environ["DISPATCHER_ENGINE"] = "react"


def case_8_stale_pending_no_masking() -> None:
    """陈旧 pending 不遮蔽新问题：上一轮未确认的确认卡挂在 session 上，
    本轮用户问新问题应正常回答（E2E 实证 bug 的回归——react_engine 用
    身份比较只认【本轮新建】的 pending_action）。"""
    sid = fresh_session_id("C8")
    tid = "YM-REACT-STALE"
    # turn1：出确认卡但不 confirm（pending 留置 session）
    lc_llm.set_script([
        {"tool_calls": [{"name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_DIR)}}]},
    ])
    r1 = with_lock_retry(lambda: dispatcher.handle_message(
        f"发起批次 {tid} 用空目录", session_id=sid))
    assert r1["status"] == "pending_confirmation", r1
    sess = sessions.get_session(sid)
    assert sess.pending_action is not None

    # turn2：同一 session 问新问题——应正常走只读链路回答，不被旧卡遮蔽
    lc_llm.set_script([
        {"tool_calls": [{"name": "get_usage", "args": {}}]},
        {"final_text": "用量回答如下"},
    ])
    # 同上：避开快路径句式，确保走 LLM 链路
    r2 = dispatcher.handle_message("帮我查一下当前的调用用量和失败次数", session_id=sid)
    assert r2["status"] == "ok", r2
    assert r2["message"] == "用量回答如下", r2
    # 旧 pending 原样保留（等 confirm），对话历史记的是本轮回答
    assert sess.pending_action is not None
    assert sess.pending_action["args"]["thread_id"] == tid
    assert sess.history[-1]["content"] == "用量回答如下", sess.history[-1]
    print("  ✓ 陈旧 pending 不遮蔽：新问题正常回答，旧卡保留待确认")

    # turn3：旧卡仍可 confirm（确认通道不受引擎影响）
    r3 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r3["status"] == "applied", r3
    assert r3["tool"] == "create_batch", r3
    print("  ✓ 旧卡 confirm：status=applied")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    # 预热 checkpoint 库
    with_lock_retry(lambda: get_graph().get_state(
        {"configurable": {"thread_id": "DISP-REACT-TEST-WARMUP"}}))

    results: list[tuple[str, bool, str]] = []

    def run(name, fn, *args):
        """跑一个用例并记账；返回 (是否通过, 用例返回值)。"""
        print(f"===== {name} =====")
        try:
            value = fn(*args)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print()
            return False, None
        finally:
            lc_llm.clear_script()
        print(f"[PASS] {name}\n")
        results.append((name, True, ""))
        return True, value

    run("1. 只读链路", case_1_readonly_chain)
    ok2, tid = run("2. 写工具确认门 + confirm 执行",
                   case_2_write_tool_confirm_gate)
    if ok2:
        # case 3 依赖 case 2 创建挂起的 YM-REACT-T1（rerun 目标须存在）
        run("3. 黄灯三轮（布防 → 是 → confirm）",
            case_3_soft_confirm_three_turns, tid)
    else:
        results.append(("3. 黄灯三轮（布防 → 是 → confirm）", False,
                        "依赖 case 2 的批次，已跳过"))
    run("4. 黄灯否定（算了 → soft_cancel）", case_4_soft_confirm_cancel)
    run("5. 并行双写只出一卡", case_5_parallel_double_write_single_card)
    run("6. 超轮兜底", case_6_recursion_fallback)
    run("7. legacy 回归", case_7_legacy_regression)
    run("8. 陈旧 pending 不遮蔽新问题", case_8_stale_pending_no_masking)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    if passed == len(results):
        print("🎉 调度 Agent react 引擎全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
