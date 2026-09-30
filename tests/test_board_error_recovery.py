# -*- coding: utf-8 -*-
"""监控看板异常恢复测试（app/orchestrator/board.py + app/db/batch_store.py）。

对应设计文档《监控看板异常恢复实施计划_20260930.md》§5：
1. 启动失败留痕：_run 后台线程异常 → mark_error("Type: msg")；
2. 僵尸自愈：running + 无 checkpoint + 无线程 + 超宽容期 → 自动标 error；
3. 僵尸宽容期：updated_at 距今 1 分钟 → 不动；
4. 残留行退回：合成行 reset_to_todo → 删行 + candidates 回归 + 留痕含 error_message；
5. 退回后可重启：再次 start_from_board 建立新 running 行、不抛 FileExistsError；
6. 恢复清错误：update_status 非 error → 清 error_message/error_at。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_board_error_recovery.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_board_err_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.orchestrator import board  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_board_err_test_")


def _make_xlsx(path: Path):
    from openpyxl import Workbook
    Workbook().save(path)


@pytest.fixture()
def watch(tmp_path):
    """每个用例独立监控目录；结束后恢复 settings.watch_dir。"""
    w = tmp_path / "watch"
    w.mkdir()
    original = get_settings().watch_dir
    get_settings().watch_dir = str(w)
    try:
        yield w
    finally:
        get_settings().watch_dir = original


def _names(items):
    return {i["folder_name"] for i in items}


def _join_board_thread(tid: str, timeout: float = 10.0) -> None:
    """等 board-start-{tid} 后台线程结束（超时保护，宁 FAIL 不挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = next((x for x in threading.enumerate()
                  if x.name == f"board-start-{tid}"), None)
        if t is None:
            return
        t.join(timeout=0.2)
    raise AssertionError(f"后台线程 board-start-{tid} 超时未结束")


def _set_updated_at(tid: str, dt: datetime) -> None:
    """直接改 batches.updated_at（伪造行龄，用于僵尸自愈/宽容期用例）。"""
    from app.db.models import Batch
    from app.db.session import get_session
    with get_session() as s:
        row = s.get(Batch, tid)
        assert row is not None
        row.updated_at = dt
        s.commit()


def _audit_rows(tid: str):
    from app.db.models import ReviewAudit
    from app.db.session import get_session
    with get_session() as s:
        return (s.query(ReviewAudit)
                .filter(ReviewAudit.thread_id == tid)
                .order_by(ReviewAudit.audit_id)
                .all())


def _mk_zombie_row(watch: Path, tid: str, folder: str, *, age_minutes: float):
    """造一条 running 无 checkpoint 行，updated_at 伪造为 age_minutes 分钟前。"""
    (watch / folder).mkdir()
    batch_store.upsert_batch(tid, watch_dir=str(watch),
                             folder_name=folder, status="running")
    _set_updated_at(tid, datetime.utcnow() - timedelta(minutes=age_minutes))


# ---------- 1. 启动失败留痕 ----------

def test_start_failure_marks_error(watch, monkeypatch):
    """后台线程抛 ValueError → batches 行 status=error，error_message 含
    异常类型名与校验信息，线程不裸奔。"""
    cand = watch / "失败批次"
    cand.mkdir()
    _make_xlsx(cand / "ContentsOfTheContainer_001.xlsx")

    def boom(folder_name, thread_id=None, downstream_file_path=None, **kw):
        raise ValueError("上游工厂文件夹路径不存在")

    monkeypatch.setattr(service, "start_batch_from_scan", boom)

    r = board.start_from_board("失败批次", thread_id="err-t1")
    assert r["ok"] is True
    assert r["thread_id"] == "err-t1"
    # HTTP 返回瞬间是预写的 running 行
    assert batch_store.get_batch("err-t1")["status"] == "running"

    _join_board_thread("err-t1")

    row = batch_store.get_batch("err-t1")
    assert row["status"] == "error"
    assert "ValueError" in row["error_message"]
    assert "上游工厂文件夹路径不存在" in row["error_message"]
    assert row["error_at"]


# ---------- 2. 僵尸自愈 ----------

def test_stale_running_self_heals_to_error(watch):
    """running + 无 checkpoint + 无线程 + updated_at 11 分钟前（>600s 阈值）
    → board_state 自动标 error，载荷透出 error_message/error_at/updated_at。"""
    _mk_zombie_row(watch, "zombie-t1", "僵尸批次", age_minutes=11)

    state = board.board_state()
    prog = {i["folder_name"]: i for i in state["in_progress"]}
    card = prog["僵尸批次"]
    assert card["status"] == "error"
    assert card["has_checkpoint"] is False
    assert "任务异常中断" in card["error_message"]
    assert card["error_at"]
    assert card["updated_at"]

    row = batch_store.get_batch("zombie-t1")
    assert row["status"] == "error"
    assert "任务异常中断" in row["error_message"]
    assert row["error_at"]


# ---------- 3. 僵尸宽容期 ----------

def test_fresh_running_grace_period_untouched(watch):
    """同上但 updated_at 1 分钟前（宽容期内）→ 仍 running，不写错误字段。"""
    _mk_zombie_row(watch, "fresh-t1", "新启动批次", age_minutes=1)

    state = board.board_state()
    prog = {i["folder_name"]: i for i in state["in_progress"]}
    card = prog["新启动批次"]
    assert card["status"] == "running"
    assert card["error_message"] is None
    assert card["error_at"] is None

    row = batch_store.get_batch("fresh-t1")
    assert row["status"] == "running"
    assert row["error_message"] is None
    assert row["error_at"] is None


# ---------- 4. 残留行退回 ----------

def test_reset_error_synthetic_row_leaves_audit(watch):
    """error 无 checkpoint 残留行 → reset_to_todo：删 batches 行、文件夹回
    candidates、ReviewAudit 有 batch_reset 留痕且 changes_json 带失败原因。"""
    tid = "rst-err-t1"
    _mk_zombie_row(watch, tid, "异常批次", age_minutes=11)
    # 先走 board_state 自愈拿到 error 行（与真实用户路径一致）
    board.board_state()
    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    err_msg = row["error_message"]

    r = board.reset_to_todo("异常批次")
    assert r["ok"] is True
    assert r["thread_id"] == tid

    # 行删除；文件夹回到未执行候选
    assert batch_store.get_batch(tid) is None
    state = board.board_state()
    assert "异常批次" in _names(state["candidates"])
    assert "异常批次" not in _names(state["in_progress"])

    # batch_reset 留痕：changes_json 含 folder_name 与当时的 error_message
    audits = _audit_rows(tid)
    assert len(audits) == 1
    assert audits[0].result_status == "batch_reset"
    assert audits[0].factory_name is None
    changes = json.loads(audits[0].changes_json)
    assert changes[0]["folder_name"] == "异常批次"
    assert changes[0]["error_message"] == err_msg


# ---------- 5. 退回后可重启 ----------

def test_restart_after_reset(watch, monkeypatch):
    """接 4 的完整闭环：退回后再次 start_from_board → 新 running 行建立，
    不抛 FileExistsError（查重不再命中已删残留行）。"""
    tid = "rst-err-t2"
    _mk_zombie_row(watch, tid, "重启批次", age_minutes=11)
    board.board_state()  # 自愈标 error
    board.reset_to_todo("重启批次")
    assert batch_store.get_batch(tid) is None

    done_event = threading.Event()

    def fake_start(folder_name, thread_id=None, downstream_file_path=None, **kw):
        done_event.set()
        return {"status": "pending_human_review"}

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)

    r = board.start_from_board("重启批次", thread_id="rst-err-t2-new")
    assert r["ok"] is True
    assert r["thread_id"] == "rst-err-t2-new"

    row = batch_store.get_batch("rst-err-t2-new")
    assert row is not None
    assert row["status"] == "running"
    assert row["folder_name"] == "重启批次"
    assert row["error_message"] is None

    assert done_event.wait(timeout=10)
    _join_board_thread("rst-err-t2-new")


# ---------- 6. 恢复清错误 ----------

def test_update_status_clears_error_fields(watch):
    """error 行调 update_status 非 error → error_message/error_at 清空；
    顺带覆盖 ensure_error_columns 幂等（重复调用不报错）。"""
    batch_store.ensure_error_columns()
    batch_store.ensure_error_columns()  # 幂等：第二次直接命中标志返回

    batch_store.upsert_batch("recover-t1", watch_dir=str(watch),
                             folder_name="恢复批次", status="running")
    assert batch_store.mark_error("recover-t1", "ValueError: 某校验失败") is True
    row = batch_store.get_batch("recover-t1")
    assert row["status"] == "error"
    assert row["error_message"] == "ValueError: 某校验失败"
    assert row["error_at"]

    assert batch_store.update_status("recover-t1", "running") is True
    row = batch_store.get_batch("recover-t1")
    assert row["status"] == "running"
    assert row["error_message"] is None
    assert row["error_at"] is None
