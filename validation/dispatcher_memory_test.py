# -*- coding: utf-8 -*-
"""L2 操作记忆测试（SQLite 持久化）。

覆盖：
1. 基本读写：load/update 正确；
2. 自动更新：auto_update_after_write 按 tool 路由（create_batch→last_thread_id,
   set_paths→recent_paths）；
3. 操作摘要：record_operation 追加最近 10 次操作；
4. 上下文注入：get_context_for_prompt 生成人话上下文；
5. 持久化验证：重启后（新实例）数据仍在；
6. 集成测试：handle_message + confirm 端到端，L2 记忆自动更新。

用法：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_memory_test.py
  DISPATCHER_ENGINE=react EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_memory_test.py

双引擎可跑：case 6 经 handle_message/confirm 端到端（入口按
DISPATCHER_ENGINE 分流），剧本经 _dual_engine.set_scripts 同注两条
mock 通道。

隔离（血泪红线）：L2 记忆落 master.db、case 6 建批次落 checkpoints.db，
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言
守卫，见 validation/_test_isolation.py）。
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

from app.dispatcher.memory import OperationMemory  # noqa: E402
from app import dispatcher  # noqa: E402

from _dual_engine import set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_mem_test_", alias_map_copy=True)


def case_1_basic_readwrite() -> None:
    """基本读写：load/update 正确。"""
    sid = f"MEM-TEST-C1-{int(time.time()*1000)}"
    mem = OperationMemory(sid)

    # 初始为空
    r = mem.load()
    assert r["last_thread_id"] is None
    assert r["last_factory"] is None
    assert r["recent_paths"] == []
    assert r["operation_summary"] == []
    print("  ✓ 初始为空")

    # update
    mem.update(last_thread_id="ETD001", last_factory="中地")
    r = mem.load()
    assert r["last_thread_id"] == "ETD001"
    assert r["last_factory"] == "中地"
    print("  ✓ update 成功")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


def case_2_auto_update() -> None:
    """自动更新：auto_update_after_write 按 tool 路由。"""
    sid = f"MEM-TEST-C2-{int(time.time()*1000)}"
    mem = OperationMemory(sid)

    # create_batch → last_thread_id
    mem.auto_update_after_write("create_batch", {"thread_id": "ETD002"}, {"status": "ok"})
    r = mem.load()
    assert r["last_thread_id"] == "ETD002"
    print("  ✓ create_batch → last_thread_id 更新")

    # set_paths → recent_paths
    mem.auto_update_after_write(
        "set_paths",
        {"paths": {"upstream_root": "/xxx/factory"}},
        {"status": "applied"},
    )
    r = mem.load()
    assert len(r["recent_paths"]) == 1
    assert r["recent_paths"][0]["path"] == "/xxx/factory"
    assert r["recent_paths"][0]["category"] == "upstream_root"
    print("  ✓ set_paths → recent_paths 更新")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


def case_3_operation_summary() -> None:
    """操作摘要：record_operation 追加最近 10 次操作。"""
    sid = f"MEM-TEST-C3-{int(time.time()*1000)}"
    mem = OperationMemory(sid)

    # 追加 12 次操作
    for i in range(12):
        mem.record_operation("test_tool", f"args_{i}", f"result_{i}")

    r = mem.load()
    # 保留最近 10 次
    assert len(r["operation_summary"]) == 10
    # 最新的是第 11 次（i=11），在最后一个位置（append 追加到尾部）
    assert r["operation_summary"][-1]["args_summary"] == "args_11"
    print(f"  ✓ 保留最近 10 次操作，最新的是 args_11（在最后一个位置）")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


def case_4_context_for_prompt() -> None:
    """上下文注入：get_context_for_prompt 生成人话上下文。"""
    sid = f"MEM-TEST-C4-{int(time.time()*1000)}"
    mem = OperationMemory(sid)

    # 空记忆
    ctx = mem.get_context_for_prompt()
    assert ctx == ""
    print("  ✓ 空记忆返回空字符串")

    # 有记忆
    mem.auto_update_after_write("create_batch", {"thread_id": "ETD003"}, {"status": "ok"})
    ctx = mem.get_context_for_prompt()
    assert "ETD003" in ctx
    assert "create_batch" in ctx
    print(f"  ✓ 有记忆生成上下文: {ctx[:60]}...")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


def case_5_persistence() -> None:
    """持久化验证：重启后（新实例）数据仍在。"""
    sid = f"MEM-TEST-C5-{int(time.time()*1000)}"

    # 写入
    mem1 = OperationMemory(sid)
    mem1.update(last_thread_id="ETD004")

    # 新实例（模拟重启）
    mem2 = OperationMemory(sid)
    r = mem2.load()
    assert r["last_thread_id"] == "ETD004"
    print("  ✓ 重启后数据仍在")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


def case_6_integration() -> None:
    """集成测试：handle_message + confirm 端到端，L2 记忆自动更新。"""
    sid = f"MEM-TEST-C6-{int(time.time()*1000)}"
    tid = f"MEM-TEST-TID-{int(time.time()*1000)}"

    # 发起批次（写操作）
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
                                  "upstream_root": "/Users/nz/Downloads/yamato/96/工厂"}}]},
    ])

    # 对话发起 → pending_confirmation
    r1 = dispatcher.handle_message(f"发起批次 {tid}", session_id=sid)
    assert r1["status"] == "pending_confirmation", r1

    # 确认执行
    r2 = dispatcher.confirm(sid, None)
    assert r2["status"] == "applied", r2

    # 检查 L2 记忆是否自动更新
    mem = OperationMemory(sid)
    r = mem.load()
    assert r["last_thread_id"] == tid, f"L2 记忆未自动更新: {r}"
    print(f"  ✓ 写操作确认后 L2 记忆自动更新：last_thread_id={r['last_thread_id']}")

    # 下一轮对话应该知道"刚才"的批次
    set_scripts([{"final_text": "好的，我帮你重跑"}])
    r3 = dispatcher.handle_message("重跑刚才的批次", session_id=sid)
    assert r3["status"] == "ok", r3
    print(f"  ✓ 下一轮对话可以引用'刚才'的批次")

    # 清理
    from app.db.session import get_session as get_db_session
    from app.db.models import DispatcherMemory
    with get_db_session() as db:
        db.query(DispatcherMemory).filter_by(session_id=sid).delete()
        db.commit()


CASES = [
    ("1. 基本读写", case_1_basic_readwrite),
    ("2. 自动更新路由", case_2_auto_update),
    ("3. 操作摘要保留最近 10 次", case_3_operation_summary),
    ("4. 上下文注入", case_4_context_for_prompt),
    ("5. 持久化验证", case_5_persistence),
    ("6. 集成：端到端自动更新", case_6_integration),
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
        print("🎉 L2 操作记忆测试全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
