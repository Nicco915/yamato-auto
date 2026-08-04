# -*- coding: utf-8 -*-
"""W2/W3 调度对话 UI 契约测试（确定性中文总结 + 对话历史端点）。

覆盖：
1. summarize_applied：create_batch 模板（挂起文案+链接）/ error 文案 /
   未知工具 fallback / result=None 不抛；
2. confirm 响应含 message/summary_lines/links/args；record_turn 写中文摘要
   且不含 review_data 原文（不再把 300 字 JSON 糊进 LLM 历史）；
3. error result → status="error"（重名 thread 执行失败）；
4. history 端点：found / not found / pending 裁剪无 args / TTL 清尸 /
   过期 found:false / peek_session 不创建不续 TTL / 空 session_id 400。

隔离（血泪红线）：checkpoint/master db、output、sessions 全部指向临时目录
（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_chat_ui_test.py
  DISPATCHER_ENGINE=react EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_chat_ui_test.py

双引擎可跑：case 2/3/4 经 handle_message/confirm（入口按
DISPATCHER_ENGINE 分流），剧本经 _dual_engine.set_scripts 同注两条
mock 通道；confirm 通道两引擎共用（execute_confirmed），断言同一标准。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.dispatcher import sessions  # noqa: E402
from app.dispatcher.summarize import summarize_applied  # noqa: E402

from _dual_engine import set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_chatui_test_", alias_map_copy=True)

client = TestClient(app)

DOWNSTREAM = os.environ.get(
    "YAMATO_TEST_DOWNSTREAM",
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
)
EMPTY_UPSTREAM = TMP / "empty_upstream"
EMPTY_UPSTREAM.mkdir(exist_ok=True)


def _create_batch_args(tid: str) -> dict:
    return {"thread_id": tid,
            "downstream_file_path": DOWNSTREAM,
            "upstream_root": str(EMPTY_UPSTREAM),
            "factory_filter": ["山東中地"]}


def case_1_summarize_templates() -> None:
    """summarize_applied：模板 / error / fallback / None 不抛。"""
    s = summarize_applied("create_batch", {}, {
        "thread_id": "T-SUM-1", "status": "pending_human_review",
        "review_data": {"factory_name": "中地",
                        "items": [{"sku": "S1"}, {"sku": "S2"}],
                        "missing_skus": ["S3"]},
    })
    assert "挂起等待人工审核" in s["message"] and "T-SUM-1" in s["message"], s
    assert "中地" in s["message"], s
    labels = {l["label"]: l["href"] for l in s["links"]}
    assert labels.get("去审核") == "/review?thread_id=T-SUM-1", s["links"]
    assert labels.get("看批次") == "/batch/T-SUM-1", s["links"]
    text = "\n".join(s["summary_lines"])
    assert "待审核工厂：中地" in text and "2 个 SKU" in text and "缺失 SKU 1 个" in text, text
    print("  ✓ create_batch 模板：挂起文案 + 明细行 + 去审核/看批次链接")

    s = summarize_applied("create_batch", {}, {"error": "批次已存在，请换一个 thread_id: X"})
    assert s["message"].startswith("执行失败：") and "批次已存在" in s["message"], s
    assert s["links"] == [] and s["summary_lines"] == [], s
    print("  ✓ error 结果 → 「执行失败：…」文案")

    s = summarize_applied("unknown_tool", {}, {"alpha": 1, "beta": "x",
                                             "nested": {"skip": True}})
    assert s["message"] == "已执行 unknown_tool", s
    assert "alpha: 1" in s["summary_lines"] and "beta: x" in s["summary_lines"], s
    assert all("nested" not in l for l in s["summary_lines"]), s
    s = summarize_applied("create_batch", None, None)
    assert s["message"] == "已执行 create_batch", s
    print("  ✓ 未知工具 fallback（标量 key 前 5）/ result=None 不抛")


def case_2_confirm_fields_and_record_turn() -> None:
    """confirm 响应含 message/summary_lines/links/args；record_turn 中文摘要。"""
    tid = f"W2-OK-{int(time.time()*1000) % 100000}"
    sid = f"W2-SID-OK-{int(time.time()*1000)}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": _create_batch_args(tid)}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid}", session_id=sid)
    assert r["status"] == "pending_confirmation", r
    r2 = dispatcher.confirm(sid, None)
    assert r2["status"] == "applied", r2
    for key in ("message", "summary_lines", "links", "args"):
        assert key in r2, f"confirm 响应缺 {key}: {list(r2)}"
    assert r2["args"]["thread_id"] == tid, r2["args"]
    assert "挂起等待人工审核" in r2["message"], r2["message"]
    hrefs = [l["href"] for l in r2["links"]]
    assert f"/review?thread_id={tid}" in hrefs, r2["links"]
    print(f"  ✓ confirm 响应四字段齐全：message={r2['message'][:40]}…")

    hist = sessions.get_session(sid).history
    assert hist[-2]["content"] == "[确认执行]", hist[-2:]
    assistant = hist[-1]["content"]
    assert assistant.startswith("已确认并执行 create_batch"), assistant
    assert "review_data" not in assistant, \
        f"中文摘要不应含 review_data 原文: {assistant[:200]}"
    assert len(assistant) <= 500, f"摘要应限 500 字符: {len(assistant)}"
    print("  ✓ record_turn 写中文摘要（无 review_data 原文，≤500 字符）")


def case_3_error_result_status() -> None:
    """error result → status="error"。"""
    tid = f"W2-DUP-{int(time.time()*1000) % 100000}"
    # 先直接建一个批次（挂起），再让 dispatcher 发起同名 → 执行失败
    service.create_batch(tid, downstream_file_path=DOWNSTREAM,
                         upstream_root=str(EMPTY_UPSTREAM),
                         factory_filter=["山東中地"])
    sid = f"W2-SID-DUP-{int(time.time()*1000)}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": _create_batch_args(tid)}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid}", session_id=sid)
    assert r["status"] == "pending_confirmation", r
    r2 = dispatcher.confirm(sid, None)
    assert r2["status"] == "error", f"重名执行应 status=error: {r2}"
    assert r2["message"].startswith("执行失败："), r2["message"]
    assert "已存在" in r2["message"], r2["message"]
    print(f"  ✓ error result → status=error：{r2['message'][:50]}…")


def case_4_history_endpoint() -> None:
    """history 端点：found/not found/pending 裁剪/TTL 清尸/过期/peek 语义。"""
    # 4a. 未知 session：found:false，且不创建会话
    ghost = "W3-GHOST-NEVER"
    r = client.get("/api/v1/dispatcher/history", params={"session_id": ghost})
    assert r.status_code == 200, r.text
    assert r.json() == {"found": False, "history": [], "pending_action": None}, r.json()
    assert sessions.peek_session(ghost) is None
    assert ghost not in sessions._SESSIONS, "history 端点不应创建会话"
    print("  ✓ 未知 session → found:false，且未创建会话")

    # 4b. found：有一轮对话的 session
    sid = f"W3-SID-HIST-{int(time.time()*1000)}"
    set_scripts([{"final_text": "你好，我是调度 Agent"}])
    dispatcher.handle_message("你好", session_id=sid)
    r = client.get("/api/v1/dispatcher/history", params={"session_id": sid})
    body = r.json()
    assert body["found"] is True and len(body["history"]) == 2, body
    assert body["history"][0] == {"role": "user", "content": "你好"}, body
    assert body["pending_action"] is None, body
    print("  ✓ found:true，history 两轮原文返回")

    # 4c. pending 裁剪：无 args，有 tool/summary/preview_lines/created_at
    tid = f"W3-PENDING-{int(time.time()*1000) % 100000}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": _create_batch_args(tid)}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid}", session_id=sid)
    assert r["status"] == "pending_confirmation", r
    r = client.get("/api/v1/dispatcher/history", params={"session_id": sid})
    pending = r.json()["pending_action"]
    assert pending is not None, r.json()
    assert "args" not in pending, f"pending 绝不回传 args: {list(pending)}"
    for key in ("tool", "summary", "preview_lines", "warnings", "created_at"):
        assert key in pending, f"pending 缺 {key}: {list(pending)}"
    assert pending["tool"] == "create_batch", pending
    print("  ✓ pending_action 裁剪回传（无 args，有 tool/summary/preview_lines）")

    # 4d. TTL 清尸：陈旧 pending → 端点顺手清掉，按 None 返回
    sess = sessions.get_session(sid)
    sess.pending_action["created_at"] = time.time() - 3600
    r = client.get("/api/v1/dispatcher/history", params={"session_id": sid})
    assert r.json()["pending_action"] is None, r.json()
    assert sess.pending_action is None, "陈旧 pending 应被清尸"
    print("  ✓ 陈旧 pending 清尸，按 None 返回")

    # 4e. 过期会话：found:false 且被清出会话表
    sid_old = f"W3-SID-OLD-{int(time.time()*1000)}"
    set_scripts([{"final_text": "旧会话"}])
    dispatcher.handle_message("hi", session_id=sid_old)
    sessions.get_session(sid_old).updated_at = time.time() - 3 * 3600
    r = client.get("/api/v1/dispatcher/history", params={"session_id": sid_old})
    assert r.json()["found"] is False, r.json()
    assert sid_old not in sessions._SESSIONS, "过期会话应被清理"
    print("  ✓ 过期会话 → found:false 且清出会话表")

    # 4f. peek_session：不创建、不续 TTL
    before = dict(sessions._SESSIONS)
    assert sessions.peek_session("W3-NEVER-PEEK") is None
    assert set(sessions._SESSIONS) == set(before), "peek 不应创建会话"
    stale_ts = time.time() - 1000
    sessions.get_session(sid).updated_at = stale_ts
    peeked = sessions.peek_session(sid)
    assert peeked is not None and peeked.updated_at == stale_ts, \
        "peek 不应续 TTL"
    print("  ✓ peek_session 不创建不续 TTL")

    # 4g. 空 session_id → 400
    r = client.get("/api/v1/dispatcher/history", params={"session_id": ""})
    assert r.status_code == 400, f"空 session_id 应 400: {r.status_code}"
    print("  ✓ 空 session_id → 400")


CASES = [
    ("1. summarize_applied 模板/error/fallback", case_1_summarize_templates),
    ("2. confirm 四字段 + record_turn 中文摘要", case_2_confirm_fields_and_record_turn),
    ("3. error result → status=error", case_3_error_result_status),
    ("4. history 端点 found/裁剪/清尸/过期/peek", case_4_history_endpoint),
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
        print("🎉 W2/W3 调度对话 UI 契约全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
