# -*- coding: utf-8 -*-
"""数据备份触发点接线测试（阶段二）。

对应《2026-10-12-数据备份机制-实施计划.md》阶段二 §2.5：
1. update_status 结案触发：running 不触发 → completed 触发一次（reason 含
   thread_id）→ 重复 completed 不重复触发 → completed_with_errors 不触发；
2. 触发函数内部异常时 update_status 仍返回 True（warning 兜底）；
3. 端点 POST /api/v1/backup 函数级真跑一次（tmp 隔离环境）→ ok + 快照目录
   存在；create_snapshot 抛 RuntimeError → HTTPException 409；
4. lifespan 冒烟：上下文管理器正常进出，启动备份检查被调用且不阻断。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_backup.py tests/test_backup_triggers.py tests/test_board_error_recovery.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
  备份默认目录挂在 master.db 同级 backups/ 下，随隔离自动落 tmp；
  本文件绝不显式把 BACKUP_DIR 指向真实目录。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_backup_trig_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from fastapi import HTTPException  # noqa: E402

from app.api import main  # noqa: E402  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.orchestrator import backup  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_backup_trig_test_")


@pytest.fixture(autouse=True)
def _backup_tmp_root(tmp_path):
    """每个用例独立备份根目录（tmp_path 下）；结束后恢复 settings 单例字段
    并清 backup 模块级状态（红线 2：settings 单例钉根），防全量混跑串味。
    """
    s = get_settings()
    original = s.backup_dir
    root = tmp_path / "backups"
    root.mkdir()
    s.backup_dir = str(root)
    with backup._state_lock:
        backup._running = False
        backup._last_success_ts = 0.0
    try:
        yield root
    finally:
        s.backup_dir = original
        with backup._state_lock:
            backup._running = False
            backup._last_success_ts = 0.0


# ---- 1. update_status 结案触发 ----

def test_update_status_completed_triggers_backup(monkeypatch):
    tid = "backup-trig-t1"
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "trigger_async", lambda reason: calls.append(reason) or True)

    assert batch_store.upsert_batch(tid, status="pending") is True
    try:
        # running：不触发
        assert batch_store.update_status(tid, "running") is True
        assert calls == []

        # 首次转 completed：触发一次，reason 含 thread_id
        assert batch_store.update_status(tid, "completed") is True
        assert calls == [f"batch_completed:{tid}"]

        # 重复 completed（old==completed）：不重复触发
        assert batch_store.update_status(tid, "completed") is True
        assert calls == [f"batch_completed:{tid}"]

        # completed_with_errors：不触发（规格只认 completed）
        assert batch_store.update_status(tid, "completed_with_errors") is True
        assert calls == [f"batch_completed:{tid}"]
    finally:
        batch_store.delete_batch(tid)


def test_update_status_trigger_exception_still_true(monkeypatch):
    tid = "backup-trig-t2"

    def _boom(reason):
        raise RuntimeError("模拟触发失败")

    monkeypatch.setattr(backup, "trigger_async", _boom)
    assert batch_store.upsert_batch(tid, status="running") is True
    try:
        # 触发函数内部异常被 warning 兜底，不影响状态更新返回 True
        assert batch_store.update_status(tid, "completed") is True
        row = batch_store.get_batch(tid)
        assert row is not None and row["status"] == "completed"
    finally:
        batch_store.delete_batch(tid)


def test_update_status_missing_row_no_trigger(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "trigger_async", lambda reason: calls.append(reason) or True)
    assert batch_store.update_status("backup-trig-no-such-row",
                                     "completed") is False
    assert calls == []


# ---- 2. 端点 POST /api/v1/backup（函数级） ----

def test_backup_endpoint_real_run(_backup_tmp_root, tmp_path, monkeypatch):
    # kb_extension.json 真实路径不隔离，monkeypatch 到 tmp（只读真文件也
    # 无害，但保持用例密闭）
    kb = tmp_path / "kb_extension.json"
    kb.write_text('{"kb": 1}\n', encoding="utf-8")
    monkeypatch.setattr(backup, "_kb_extension_path", lambda: kb)

    result = asyncio.run(main.backup_now())

    assert result["ok"] is True
    snap = Path(result["snapshot"])
    assert snap.is_dir()
    assert snap.parent == _backup_tmp_root
    assert backup.SNAPSHOT_NAME_RE.match(snap.name)
    assert (snap / "manifest.json").is_file()
    assert (snap / "external_paths.json").is_file()
    assert ".env" not in {p.name for p in snap.iterdir()}


def test_backup_endpoint_conflict_409(monkeypatch):
    def _busy(reason):
        raise RuntimeError("已有备份任务进行中")

    monkeypatch.setattr(backup, "create_snapshot", _busy)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.backup_now())
    assert exc_info.value.status_code == 409
    assert "已有备份任务进行中" in str(exc_info.value.detail)


def test_backup_endpoint_error_500(monkeypatch):
    def _boom(reason):
        raise ValueError("模拟意外失败")

    monkeypatch.setattr(backup, "create_snapshot", _boom)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.backup_now())
    assert exc_info.value.status_code == 500


# ---- 3. lifespan 冒烟 ----

def test_lifespan_smoke_calls_startup_backup_check(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "maybe_backup_on_startup",
        lambda: calls.append("startup") or False)

    async def _enter_exit():
        async with main.lifespan(main.app):
            pass

    asyncio.run(_enter_exit())
    assert calls == ["startup"]


def test_lifespan_smoke_backup_check_failure_not_blocking(monkeypatch):
    def _boom():
        raise RuntimeError("模拟启动备份检查崩溃")

    monkeypatch.setattr(backup, "maybe_backup_on_startup", _boom)

    async def _enter_exit():
        async with main.lifespan(main.app):
            pass

    # 失败只 warning，不阻断启动（上下文管理器正常进出，不外抛）
    asyncio.run(_enter_exit())
