# -*- coding: utf-8 -*-
"""调度 Agent 写工具 + 确认门端到端测试（DISPATCHER_MOCK=1 剧本注入）。

覆盖（thread_id / session_id 一律用 DISP-WRITE-TEST- 前缀，与并行运行的
dispatcher_read_test.py 隔离）：
1. 写工具拦截不执行（pending_confirmation，checkpoints 不应出现新 thread）；
2. confirm 执行成功（session 留存优先，批次已创建）；
3. 篡改 action 防护（confirm 时传不同 thread_id 应执行留存版本）；
4. 过期 action 拒绝（created_at 早 3600 秒 → expired）；
5. submit_review diff 预览（数值字段 old→new 列 lines）+ review_audits 落库；
6. rerun（挂起批次重跑，确认门+执行都走通）；
7. set_paths 经 dispatcher（临时 .env，.env.bak 备份，行为与 agent_chat 一致）；
8. chat_paths_test 回归（EXTRACTION_MOCK=1 原样跑必须全绿）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_write_test.py
  DISPATCHER_ENGINE=react EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_write_test.py

双引擎可跑：剧本经 _dual_engine.set_scripts 同注 legacy/react 两条 mock
通道（确认门拦截、篡改防护、TTL、审计落库等安全断言两引擎同一标准）。

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
防 llm_client 的 load_dotenv(override=True) 把 env 盖回真实路径，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
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
from app.graph import get_graph  # noqa: E402

from _dual_engine import set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_write_test_", alias_map_copy=True)


# ---- 真实 fixture（复用 chat_paths_test 同款数据）----
REAL_ROOT = os.environ.get("YAMATO_TEST_REAL_ROOT", "/Users/nz/Downloads/yamato/96/工厂")
DOWNSTREAM = os.environ.get(
    "YAMATO_TEST_DOWNSTREAM",
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
)
EMPTY_DIR = Path(tempfile.mkdtemp(prefix="yamato_write_empty_"))


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
    return f"DISP-WRITE-TEST-{tag}-{int(time.time()*1000)}"


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_write_tool_intercept() -> tuple[str, str]:
    """写工具拦截不执行：pending_confirmation + checkpoints 无该 thread。"""
    sid = fresh_session_id("C1")
    tid = f"DISP-WRITE-TEST-INTERCEPT-{int(time.time()*1000) % 100000}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_DIR)}}]},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        f"发起批次 {tid} 用空目录", session_id=sid))
    assert r["status"] == "pending_confirmation", r
    assert r["action"]["kind"] == "dispatcher_tool"
    assert r["action"]["tool"] == "create_batch"
    assert r["action"]["args"]["thread_id"] == tid
    assert "preview_lines" in r["action"] and r["action"]["preview_lines"]
    # 批次绝未执行
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert state["exists"] is False, f"批次不应已创建: {state}"
    # session 留存了 pending action
    sess = sessions.get_session(sid)
    assert sess.pending_action is not None
    assert sess.pending_action["tool"] == "create_batch"
    assert sess.pending_action["args"]["thread_id"] == tid
    print(f"  ✓ 拦截：pending_confirmation，checkpoints 不存在 {tid}，"
          f"session.pending_action 已存")
    return sid, tid


def case_2_confirm_executes() -> str:
    """confirm 执行：无客户端 action 时走服务端留存版本，批次已创建。"""
    sid = fresh_session_id("C2")
    tid = f"DISP-WRITE-TEST-CONFIRM-{int(time.time()*1000) % 100000}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_DIR)}}]},
    ])
    with_lock_retry(lambda: dispatcher.handle_message(
        f"发起批次 {tid}", session_id=sid))
    # 服务端留存优先：confirm 不传 action
    r = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r["status"] == "applied", r
    assert r["tool"] == "create_batch"
    # 批次已创建（挂起）
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert state["exists"] is True, f"批次应已创建: {state}"
    assert "node5_human_review" in state.get("next_nodes"), state
    print(f"  ✓ 执行：status=applied，批次 {tid} 已挂起待审")
    return tid


def case_3_tamper_action_protection() -> None:
    """篡改 action 防护：confirm 时传不同 thread_id，执行的应是留存版本。"""
    sid = fresh_session_id("C3")
    tid = f"DISP-WRITE-TEST-TAMPER-{int(time.time()*1000) % 100000}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_DIR)}}]},
    ])
    with_lock_retry(lambda: dispatcher.handle_message(
        f"发起批次 {tid}", session_id=sid))
    # 客户端伪造：传不同 thread_id
    tampered = {
        "kind": "dispatcher_tool", "tool": "create_batch",
        "args": {"thread_id": "DISP-WRITE-TEST-TAMPER-FAKE",
                 "downstream_file_path": DOWNSTREAM,
                 "upstream_root": str(EMPTY_DIR)},
        "created_at": time.time(),
    }
    r = with_lock_retry(lambda: dispatcher.confirm(sid, tampered))
    assert r["status"] == "applied", r
    # 实际执行的应是留存版本（tid），不是伪造的 FAKE
    fake_state = with_lock_retry(lambda: service.get_order_state(
        "DISP-WRITE-TEST-TAMPER-FAKE"))
    real_state = with_lock_retry(lambda: service.get_order_state(tid))
    assert fake_state["exists"] is False, \
        f"伪造版本不应被执行: {fake_state}"
    assert real_state["exists"] is True, \
        f"留存版本应被执行: {real_state}"
    print(f"  ✓ 防护：伪造 thread_id 被忽略，执行的是 session 留存版本")


def case_4_expired_action() -> None:
    """过期 action 拒绝：created_at 早 3600 秒 → expired。"""
    sid = fresh_session_id("C4")
    tid = "DISP-WRITE-TEST-EXPIRED-004"
    sess = sessions.get_session(sid)
    sess.pending_action = {
        "kind": "dispatcher_tool", "tool": "create_batch",
        "args": {"thread_id": tid,
                 "downstream_file_path": DOWNSTREAM,
                 "upstream_root": str(EMPTY_DIR)},
        "summary": "过期动作", "preview_lines": [], "warnings": [],
        "created_at": time.time() - 3600,
    }
    r = dispatcher.confirm(sid, None)
    assert r["status"] == "expired", r
    print(f"  ✓ 拒绝：status=expired，{r['message']}")


def case_5_submit_review_diff_preview() -> None:
    """submit_review diff 预览：数值字段 old→new 列 lines，review_audits 落库。"""
    # 先造一个挂起批次（复用 case 2 同款 fixture 但独立 thread，动态命名避免重跑撞）
    tid = f"DISP-WRITE-TEST-REVIEW-{int(time.time()*1000) % 100000}"
    with_lock_retry(lambda: service.create_batch(
        tid, downstream_file_path=DOWNSTREAM, upstream_root=str(EMPTY_DIR)))
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert state["exists"] and "node5_human_review" in state["next_nodes"]
    payload = with_lock_retry(lambda: service.get_review_payload(tid))
    assert payload and payload["items"], payload
    # 改第一个 item 的 total_quantity（+10）
    new_items = [dict(i) for i in payload["items"]]
    new_items[0]["extracted_data"] = dict(new_items[0]["extracted_data"])
    new_items[0]["extracted_data"]["total_quantity"] = \
        int(new_items[0]["extracted_data"].get("total_quantity") or 0) + 10
    sid = fresh_session_id("C5")
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "submit_review",
                         "args": {"thread_id": tid, "approved": True,
                                  "items": new_items}}]},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        f"改 {tid} 第一个 SKU 件数加 10 并确认", session_id=sid))
    assert r["status"] == "pending_confirmation", r
    assert r["action"]["tool"] == "submit_review"
    preview_text = "\n".join(r["action"]["preview_lines"])
    assert "修改" in preview_text or "old" in preview_text or "→" in preview_text, \
        f"预览应含 old→new diff: {preview_text[:300]}"
    print(f"  ✓ 预览含 diff：{preview_text[:80].replace(chr(10),' | ')}")
    # 确认执行
    r2 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r2["status"] == "applied", r2
    # 审计链落库（review_audits 在临时 master.db）
    from app.config import get_settings
    master_db = str(get_settings().master_db_abs)
    assert master_db.startswith(str(TMP)), f"master.db 未隔离: {master_db}"
    conn = sqlite3.connect(master_db)
    try:
        rows = conn.execute(
            "SELECT thread_id, approved, edited_count FROM review_audits "
            "WHERE thread_id = ?", (tid,)).fetchall()
    finally:
        conn.close()
    assert rows, f"review_audits 应落库 thread_id={tid}"
    approved, edited = rows[0][1], rows[0][2]
    assert approved == 1 or approved is True, rows
    assert edited >= 1, f"edited_count 应至少 1: {rows}"
    print(f"  ✓ 审计落库：approved={approved}, edited_count={edited}")


def case_6_rerun() -> None:
    """rerun：挂起批次确认门+执行都走通。"""
    tid = f"DISP-WRITE-TEST-RERUN-{int(time.time()*1000) % 100000}"
    with_lock_retry(lambda: service.create_batch(
        tid, downstream_file_path=DOWNSTREAM, upstream_root=str(EMPTY_DIR)))
    sid = fresh_session_id("C6")
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "rerun",
                         "args": {"thread_id": tid}}]},
    ])
    r = with_lock_retry(lambda: dispatcher.handle_message(
        f"重跑批次 {tid}", session_id=sid))
    assert r["status"] == "pending_confirmation", r
    assert r["action"]["tool"] == "rerun"
    # 确认
    r2 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
    assert r2["status"] == "applied", r2
    # rerun 后仍挂起
    state = with_lock_retry(lambda: service.get_order_state(tid))
    assert "node5_human_review" in state["next_nodes"], state
    print(f"  ✓ rerun 执行成功，批次仍挂起")


def case_7_set_paths_via_dispatcher() -> None:
    """set_paths 经 dispatcher：临时 .env 更新、.bak 备份、行为与 agent_chat 一致。"""
    tmp_env = Path(tempfile.mkdtemp(prefix="yamato_write_env_")) / ".env"
    shutil.copy2(APP_ROOT / ".env", tmp_env)
    before = tmp_env.read_text(encoding="utf-8")

    # 临时 monkey-patch DEFAULT_ENV_PATH：让 set_paths preview/execute 走临时 .env
    from app import agent_chat as chat
    orig_env = chat.DEFAULT_ENV_PATH
    chat.DEFAULT_ENV_PATH = tmp_env

    try:
        new_root = "/Users/nz/Downloads/yamato/96/工厂"  # 本机真实目录
        sid = fresh_session_id("C7")
        set_scripts([
            {"tool_calls": [{"id": "c1", "name": "set_paths",
                             "args": {"paths": {"upstream_root": new_root}}}]},
        ])
        r = dispatcher.handle_message(
            f"把工厂文件夹改到 {new_root}", session_id=sid)
        assert r["status"] == "pending_confirmation", r
        assert r["action"]["tool"] == "set_paths"
        assert r["action"]["args"]["paths"]["upstream_root"] == new_root
        preview_text = "\n".join(r["action"]["preview_lines"])
        assert new_root in preview_text or "修改" in preview_text, \
            f"预览应含新路径: {preview_text[:300]}"
        # 确认执行（注意：apply_paths 内部 env_path 缺省走 DEFAULT_ENV_PATH，已 patch）
        r2 = with_lock_retry(lambda: dispatcher.confirm(sid, None))
        assert r2["status"] == "applied", r2
        after = tmp_env.read_text(encoding="utf-8")
        assert f"UPSTREAM_ROOT={new_root}" in after, ".env 未写入新路径"
        # 其他行原样保留
        for line in before.splitlines():
            if line.strip().startswith("UPSTREAM_ROOT="):
                continue
            assert line in after.splitlines(), f".env 原有行被改动: {line[:50]}"
        assert (tmp_env.parent / f"{tmp_env.name}.bak").exists(), "缺 .env.bak 备份"
        print(f"  ✓ set_paths 生效：.env 已更新，.bak 已备份")
    finally:
        chat.DEFAULT_ENV_PATH = orig_env


def case_8_chat_paths_regression() -> None:
    """chat_paths_test 回归：必须全绿。"""
    import subprocess
    env = dict(os.environ)
    env["EXTRACTION_MOCK"] = "1"
    env["LLM_ENABLE_THINKING"] = "0"
    r = subprocess.run(
        [sys.executable, "validation/chat_paths_test.py"],
        cwd=str(APP_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, \
        f"chat_paths_test 回归失败：\nstdout:\n{r.stdout[-3000:]}\nstderr:\n{r.stderr[-3000:]}"
    assert "全部通过" in r.stdout, f"chat_paths_test 未全绿：\n{r.stdout[-1500:]}"
    print(f"  ✓ chat_paths_test 回归通过")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. 写工具拦截不执行", case_1_write_tool_intercept),
    ("2. confirm 执行成功", case_2_confirm_executes),
    ("3. 篡改 action 防护", case_3_tamper_action_protection),
    ("4. 过期 action 拒绝", case_4_expired_action),
    ("5. submit_review diff 预览 + 审计落库", case_5_submit_review_diff_preview),
    ("6. rerun", case_6_rerun),
    ("7. set_paths 经 dispatcher", case_7_set_paths_via_dispatcher),
    ("8. chat_paths_test 回归", case_8_chat_paths_regression),
]


def main() -> int:
    # 预热 checkpoint 库
    with_lock_retry(lambda: get_graph().get_state(
        {"configurable": {"thread_id": "DISP-WRITE-TEST-WARMUP"}}))

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
        print("🎉 调度 Agent 写工具 + 确认门全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
