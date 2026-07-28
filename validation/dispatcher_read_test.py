# -*- coding: utf-8 -*-
"""调度 Agent 只读链路测试（DISPATCHER_MOCK=1 剧本注入，不调真实 LLM）。

覆盖（thread_id / session_id 一律用 DISP-READ-TEST- 前缀，与并行运行的
写测试 agent（DISP-WRITE-TEST- 前缀）隔离）：
1. 查询问答：list_batches 工具真被调用，final_text 原样返回；
2. 多轮工具串联：get_batch_status + get_batch_detail 两步后给最终回复；
3. 未知工具：hack_system 回喂错误，不崩溃；
4. 坏参数：缺必填 thread_id，错误被回喂给第二轮 llm_step；
5. 轮数上限：6 轮工具调用不收敛 → "处理步骤过多" 兜底；
6. explain_errors 降级：不存在 thread 返回 {"error": ...}；真实批次
   （checkpoints 库里有则用，没有则跳过该子项）返回结构完整；
7. json 适配器回归：DISPATCHER_STEP_MODE=json 时用例 1 重跑（mock 仍生效）；
8. TestClient 端点冒烟：POST /api/v1/dispatcher/chat 200 / 缺 message 400 /
   confirm 无 session 无 action 400。

剧本队列是模块级全局（loop._MOCK_SCRIPT），每个用例前 clear 再 extend；
session_id 每用例唯一，避免历史串扰。sqlite checkpoint 为共享文件，
遇 database lock 类瞬时错误 sleep 1s 重试一次再断言失败。

用法（在 app/ 目录下）：
  python3 validation/dispatcher_read_test.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ---- env 前置（必须在 import app 之前）----
os.environ.setdefault("EXTRACTION_MOCK", "1")   # 提取走 mock（本测试不跑图，防御性设置）
os.environ["DISPATCHER_MOCK"] = "1"             # 调度循环走剧本，不调真实 LLM
os.environ["EXPLAIN_MOCK"] = "1"                # explain_errors 走模板降级（确定性输出）

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.dispatcher import loop, sessions  # noqa: E402
from app.dispatcher.explain import explain_errors  # noqa: E402
from app.graph import get_graph  # noqa: E402

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


def set_script(items: list[dict]) -> None:
    """清空剧本队列再注入（模块级全局，用例间必须隔离）。"""
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(items)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_query() -> None:
    """查询问答：list_batches 被真实调用（结果入 tool_history），回复为 final_text。"""
    sid = "DISP-READ-TEST-C1"
    set_script([
        {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
        {"final_text": "当前有 N 个批次…"},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message("现在有哪些批次？",
                                                          session_id=sid))
    assert r["status"] == "ok", r
    assert r["message"] == "当前有 N 个批次…", r
    assert r.get("session_id") == sid, r
    hist = sessions.get_session(sid).tool_history
    assert any(h["tool"] == "list_batches" and h["confirmed"] is None
               for h in hist), f"list_batches 未入 tool_history: {hist}"
    assert loop._MOCK_SCRIPT == [], "剧本应被恰好用尽"


def case_2_multi_tool_chain() -> None:
    """多轮工具串联：get_batch_status + get_batch_detail 两步只读后给最终回复。"""
    sid = "DISP-READ-TEST-C2"
    set_script([
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
    set_script([
        {"tool_calls": [{"id": "c1", "name": "hack_system", "args": {}}]},
        {"final_text": "没有这个工具"},
    ])
    r = dispatcher.handle_message("帮我 hack 一下", session_id=sid)
    assert r["status"] == "ok", r
    assert r["message"] == "没有这个工具", r
    hist = sessions.get_session(sid).tool_history
    assert not hist, f"未知工具绝不应执行/入审计流水: {hist}"


def case_4_bad_args() -> None:
    """坏参数：缺必填 thread_id → validate_args 拦截，错误回喂第二轮 llm_step。"""
    sid = "DISP-READ-TEST-C4"
    set_script([
        {"tool_calls": [{"id": "c1", "name": "get_batch_status", "args": {}}]},
        {"final_text": "参数不足"},
    ])
    # 包一层 spy：断言第二轮 llm_step 收到的 messages 里含参数错误回喂
    captured: list[list[dict]] = []
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
    assert len(captured) == 2, f"应走 2 轮 llm_step: {len(captured)}"
    second = captured[1]
    assert any(m.get("role") == "tool" and "参数错误" in str(m.get("content"))
               for m in second), \
        f"第二轮 messages 应含参数错误回喂: {second}"
    hist = sessions.get_session(sid).tool_history
    assert not hist, f"参数校验失败的工具不应执行/入审计流水: {hist}"


def case_5_max_rounds() -> None:
    """轮数上限：6 轮工具调用仍不收敛 → 返回「处理步骤过多」兜底提示。"""
    sid = "DISP-READ-TEST-C5"
    set_script([
        {"tool_calls": [{"id": f"c{i}", "name": "list_batches", "args": {}}]}
        for i in range(loop.MAX_ROUNDS)
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message("反复查", session_id=sid))
    assert r["status"] == "ok", r
    assert "处理步骤过多" in r["message"], r
    hist = sessions.get_session(sid).tool_history
    assert len(hist) == loop.MAX_ROUNDS, f"6 轮工具都应执行: {len(hist)}"


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
        set_script([
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
    set_script([{"final_text": "端点正常"}])
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
