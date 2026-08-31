# -*- coding: utf-8 -*-
"""调度 Agent 只读链路测试（DISPATCHER_MOCK=1 剧本注入，不调真实 LLM）。

覆盖（thread_id / session_id 一律用 DISP-READ-TEST- 前缀，与并行运行的
写测试 agent（DISP-WRITE-TEST- 前缀）隔离）：
1. 查询问答：list_batches 工具真被调用，final_text 原样返回；
2. 多轮工具串联：get_batch_status + get_batch_detail 两步后给最终回复；
3. 未知工具：hack_system 回喂错误，不崩溃；
4. 坏参数：缺必填 thread_id，错误被回喂给第二轮 llm_step；
5. 轮数上限：工具调用不收敛 → "处理步骤过多" 兜底（legacy 6 轮全执行；
   react recursion_limit=12 下 6 次模型调用、5 次工具执行后截断）；
6. explain_errors 降级：不存在 thread 返回 {"error": ...}；真实批次
   （checkpoints 库里有则用，没有则跳过该子项）返回结构完整；
7. json 适配器回归：DISPATCHER_STEP_MODE=json 时用例 1 重跑（mock 仍生效；
   仅 legacy 适用——react 引擎固定 native tool_calls，该环境变量无感）；
8. TestClient 端点冒烟：POST /api/v1/dispatcher/chat 200 / 缺 message 400 /
   confirm 无 session 无 action 400。

双引擎可跑：剧本经 _dual_engine.set_scripts 同注 legacy/react 两条 mock
通道（各自 pop 消费，独立深拷贝），默认 legacy，
DISPATCHER_ENGINE=react 时同一套断言再跑一遍；
session_id 每用例唯一，避免历史串扰。sqlite checkpoint 为共享文件，
遇 database lock 类瞬时错误 sleep 1s 重试一次再断言失败。

用法（在 app/ 目录下）：
  python3 validation/dispatcher_read_test.py                        # legacy 引擎
  DISPATCHER_ENGINE=react python3 validation/dispatcher_read_test.py  # react 引擎

隔离（血泪红线）：checkpoint/master db、output、sessions 目录全部指向
临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ.setdefault("EXTRACTION_MOCK", "1")   # 提取走 mock（本测试不跑图，防御性设置）
os.environ["DISPATCHER_MOCK"] = "1"             # 调度循环走剧本，不调真实 LLM
os.environ["EXPLAIN_MOCK"] = "1"                # explain_errors 走模板降级（确定性输出）

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.dispatcher import lc_llm, loop, sessions  # noqa: E402
from app.dispatcher.explain import explain_errors  # noqa: E402
from app.graph import get_graph  # noqa: E402

from _dual_engine import active_script, engine, set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_read_test_")

GHOST_THREAD = "DISP-READ-TEST-GHOST-000"   # 保证不存在的批次号


def with_lock_retry(fn):
    """sqlite 共享文件瞬时 lock：sleep 1s 重试一次，再失败才抛出。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        if "lock" in str(e).lower():
            time.sleep(1)
            return fn()
        raise


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_query() -> None:
    """查询问答：list_batches 被真实调用（结果入 tool_history），回复为 final_text。"""
    sid = "DISP-READ-TEST-C1"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
        {"final_text": "当前有 N 个批次…"},
    ])
    # 「现在有哪些批次」类短查询现在走快路径（fastpath，不进 LLM）；
    # 本用例验证 LLM 只读链路，故用快路径不放行的问法
    r = with_lock_retry(lambda: dispatcher.handle_message("帮我看看目前都有哪些批次在跑",
                                                          session_id=sid))
    assert r["status"] == "ok", r
    assert r["message"] == "当前有 N 个批次…", r
    assert r.get("session_id") == sid, r
    hist = sessions.get_session(sid).tool_history
    assert any(h["tool"] == "list_batches" and h["confirmed"] is None
               for h in hist), f"list_batches 未入 tool_history: {hist}"
    # 剧本应被恰好用尽（只认当前引擎的通道——另一条的注入副本原样留存）
    assert active_script() == [], "剧本应被恰好用尽"


def case_2_multi_tool_chain() -> None:
    """多轮工具串联：get_batch_status + get_batch_detail 两步只读后给最终回复。"""
    sid = "DISP-READ-TEST-C2"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "get_batch_status",
                         "args": {"thread_id": GHOST_THREAD}}]},
        {"tool_calls": [{"id": "c2", "name": "get_batch_detail",
                         "args": {"thread_id": GHOST_THREAD}}]},
        {"final_text": "该批次不存在，请核对批次号。"},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        f"批次 {GHOST_THREAD} 状态如何？给我看看详情", session_id=sid))
    assert r["status"] == "ok", r
    assert r["message"] == "该批次不存在，请核对批次号。", r
    hist = sessions.get_session(sid).tool_history
    names = [h["tool"] for h in hist]
    assert names == ["get_batch_status", "get_batch_detail"], \
        f"应有 2 条只读记录: {names}"
    assert all(h["confirmed"] is None for h in hist), "只读工具 confirmed 应为 None"


def case_3_unknown_tool() -> None:
    """未知工具：回喂错误，不执行、不崩溃，第二轮正常给最终回复。"""
    sid = "DISP-READ-TEST-C3"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "hack_system", "args": {}}]},
        {"final_text": "没有这个工具"},
    ])
    r = dispatcher.handle_message("帮我 hack 一下", session_id=sid)
    assert r["status"] == "ok", r
    assert r["message"] == "没有这个工具", r
    hist = sessions.get_session(sid).tool_history
    assert not hist, f"未知工具绝不应执行/入审计流水: {hist}"


def case_4_bad_args() -> None:
    """坏参数：缺必填 thread_id → 校验拦截，错误回喂第二轮，模型自我修正。

    回喂路径两引擎不同（断言按引擎条件，语义不变量相同——坏参数绝不执行、
    错误回文进第二轮 LLM 输入）：
    - legacy：loop 内 validate_args 拦截，回喂中文「参数错误」tool 消息
      （spy 包 loop.llm_step 抓 messages dict）；
    - react：ToolNode 对 args_schema 做 pydantic 校验，失败回喂英文
      ValidationError ToolMessage（spy 包 lc_llm._generate 抓 BaseMessage）。
    """
    sid = "DISP-READ-TEST-C4"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "get_batch_status", "args": {}}]},
        {"final_text": "参数不足"},
    ])
    captured: list[list] = []
    if engine() == "react":
        # 包一层 spy：断言第二轮 _generate 收到的消息里含校验失败回喂
        orig = lc_llm.QwenDispatcherChatModel._generate

        def spy(self, messages, stop=None, run_manager=None, **kwargs):
            captured.append(list(messages))
            return orig(self, messages, stop=stop, run_manager=run_manager,
                        **kwargs)

        lc_llm.QwenDispatcherChatModel._generate = spy
        try:
            r = dispatcher.handle_message("看看批次状态", session_id=sid)
        finally:
            lc_llm.QwenDispatcherChatModel._generate = orig
    else:
        # 包一层 spy：断言第二轮 llm_step 收到的 messages 里含参数错误回喂
        orig = loop.llm_step

        def spy(messages, *, phase):
            captured.append([dict(m) for m in messages])
            return orig(messages, phase=phase)

        loop.llm_step = spy
        try:
            r = dispatcher.handle_message("看看批次状态", session_id=sid)
        finally:
            loop.llm_step = orig
    assert r["status"] == "ok", r
    assert r["message"] == "参数不足", r
    assert len(captured) == 2, f"应走 2 轮 LLM 步进: {len(captured)}"
    second = captured[1]
    if engine() == "react":
        # ToolNode 校验失败的回喂：ToolMessage 含 pydantic 错误（指明缺
        # thread_id 字段；英文文案来自 pydantic/langgraph，不做中文断言）
        assert any("thread_id" in str(m.content)
                   and ("validation error" in str(m.content)
                        or "Field required" in str(m.content))
                   for m in second
                   if m.__class__.__name__ == "ToolMessage"), \
            f"第二轮 messages 应含校验失败回喂: {second}"
    else:
        assert any(m.get("role") == "tool" and "参数错误" in str(m.get("content"))
                   for m in second), \
            f"第二轮 messages 应含参数错误回喂: {second}"
    hist = sessions.get_session(sid).tool_history
    assert not hist, f"参数校验失败的工具不应执行/入审计流水: {hist}"


def case_5_max_rounds() -> None:
    """轮数上限：工具调用仍不收敛 → 返回「处理步骤过多」兜底提示。

    截断口径两引擎不同（断言按引擎条件，不变量相同——必走兜底文案）：
    - legacy：MAX_ROUNDS=6 轮全部执行后超轮兜底，tool_history 恰 6 条；
    - react：recursion_limit=12，每个工具调用耗 2 个图步（agent+tools），
      第 6 次模型调用后 remaining_steps<2 以截断哨终止（实测口径，
      与 dispatcher_react_engine_test 超轮用例一致），tool_history 恰 5 条。
    """
    sid = "DISP-READ-TEST-C5"
    if engine() == "react":
        from app.dispatcher.react_engine import RECURSION_LIMIT
        rounds, expected_tools = RECURSION_LIMIT, 5
    else:
        rounds, expected_tools = loop.MAX_ROUNDS, loop.MAX_ROUNDS
    set_scripts([
        {"tool_calls": [{"id": f"c{i}", "name": "list_batches", "args": {}}]}
        for i in range(rounds)
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message("反复查", session_id=sid))
    assert r["status"] == "ok", r
    assert "处理步骤过多" in r["message"], r
    hist = sessions.get_session(sid).tool_history
    assert len(hist) == expected_tools, \
        f"截断前应执行 {expected_tools} 轮工具: {len(hist)}"


def case_6_explain_degraded() -> None:
    """explain_errors：不存在 thread 返回 error；真实批次返回结构完整。"""
    r = explain_errors(GHOST_THREAD)
    assert "error" in r and GHOST_THREAD in r["error"], \
        f"不存在的 thread 应返回 error dict: {r}"
    print(f"  ✓ 不存在 thread 返回 error: {r['error']}")

    batches = with_lock_retry(lambda: service.list_batches()).get("batches", [])
    if not batches:
        print("  （跳过真实批次子项：checkpoints 库中暂无批次）")
        return
    tid = batches[0]["thread_id"]
    r2 = with_lock_retry(lambda: explain_errors(tid))
    for key in ("summary", "causes", "suggestions", "degraded"):
        assert key in r2, f"返回缺字段 {key}: {list(r2)}"
    assert isinstance(r2["degraded"], bool), r2["degraded"]
    assert isinstance(r2["causes"], list) and isinstance(r2["suggestions"], list), r2
    # 有异常时 EXPLAIN_MOCK=1 必走模板降级；无异常时为非降级的正常空报告
    if r2["raw"]["issue_count"] or r2["raw"]["missing_skus"] or r2["raw"]["error_skus"]:
        assert r2["degraded"] is True, f"EXPLAIN_MOCK=1 有异常时应 degraded: {r2}"
    print(f"  ✓ 真实批次 {tid}: degraded={r2['degraded']}, "
          f"causes={len(r2['causes'])}, suggestions={len(r2['suggestions'])}")


def case_7_json_mode_regression() -> None:
    """json 适配器回归：DISPATCHER_STEP_MODE=json 时用例 1 重跑（mock 仍生效）。"""
    sid = "DISP-READ-TEST-C7"
    os.environ["DISPATCHER_STEP_MODE"] = "json"
    try:
        set_scripts([
            {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
            {"final_text": "json 模式回复"},
        ])
        r = with_lock_retry(lambda: dispatcher.handle_message(
            "列一下批次", session_id=sid))
        assert r["status"] == "ok", r
        assert r["message"] == "json 模式回复", r
        hist = sessions.get_session(sid).tool_history
        assert any(h["tool"] == "list_batches" for h in hist), hist
    finally:
        os.environ.pop("DISPATCHER_STEP_MODE", None)


def case_8_endpoint_smoke() -> None:
    """TestClient 端点冒烟：200 / 缺 message 400 / confirm 无凭据 400。"""
    client = TestClient(app)
    sid = "DISP-READ-TEST-C8"
    set_scripts([{"final_text": "端点正常"}])
    r = client.post("/api/v1/dispatcher/chat",
                    json={"message": "你好", "session_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["message"] == "端点正常", body
    assert body.get("session_id") == sid, body

    r2 = client.post("/api/v1/dispatcher/chat", json={})
    assert r2.status_code == 400, f"缺 message 应 400: {r2.status_code} {r2.text}"

    r3 = client.post("/api/v1/dispatcher/chat", json={"confirm": True})
    assert r3.status_code == 400, \
        f"confirm 无 session 无 action 应 400: {r3.status_code} {r3.text}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. 查询问答（list_batches 真被调用）", case_1_query),
    ("2. 多轮工具串联（status + detail）", case_2_multi_tool_chain),
    ("3. 未知工具不崩溃", case_3_unknown_tool),
    ("4. 坏参数被回喂", case_4_bad_args),
    ("5. 轮数上限兜底", case_5_max_rounds),
    ("6. explain_errors 模板降级", case_6_explain_degraded),
    ("7. json 适配器回归", case_7_json_mode_regression),
    ("8. TestClient 端点冒烟", case_8_endpoint_smoke),
]


def main() -> int:
    # 预热：全新环境 checkpoints.db 不存在时，service 以 mode=ro 打开会失败，
    # 先触发建库建表（与 ui_api_test 同款规避；共享库已存在时是无害 no-op）
    with_lock_retry(lambda: get_graph().get_state(
        {"configurable": {"thread_id": "DISP-READ-TEST-WARMUP"}}))

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
        print("🎉 调度 Agent 只读链路全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
