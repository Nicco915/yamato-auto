# -*- coding: utf-8 -*-
"""批次终止（aborted 状态 + 协作式取消）测试。

对应实施计划《2026-10-14-批次终止与日志轮转-实施计划.md》阶段二（2.8 节）：
1. cancel_requested 标志三函数：set/clear/is 正常往返；不存在行不抛
   （set/clear 返回 False，is 返回 False）；
2. folder_router 检查点：置标志 → 抛 BatchAbortedError；清标志 → 空队列
   正常返回（检查点在弹队列之前）；
3. _mark_batch_aborted：running 行 → aborted、标志已清、completed_at 仍
   None；钉死：不触发结案备份（orchestrator.backup.trigger_async 未调用）；
4. abort_batch 四情形（monkeypatch get_graph 假 snap）：行不存在 ValueError /
   pending_review RuntimeError / running+新鲜 cooperative / running+超龄
   immediate / 已 aborted 再 abort RuntimeError；
5. pipeline_state 自愈守卫：aborted 行 + 假 checkpoint 推导 desired=running
   → 状态不被覆写；
6. reset_to_todo：aborted 行放行退回（不抛「批次正在运行」）；running 行
   同假 snap 仍抛 RuntimeError；
7. 包装层协作停止（代表一条 rerun_with_paths）：stream 抛
   BatchAbortedError → 返回 status=aborted 且批次行 aborted、不抛异常。

绝不发真实 LLM 请求（隔离 .env 无 key，假 graph 不跑真图）。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_batch_abort.py tests/test_stream_fallback.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_batch_abort_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.graph import NODE2  # noqa: E402
from app.nodes.folder_router import folder_router  # noqa: E402
from app.orchestrator import backup, board, pipeline_state  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_batch_abort_test_")


@pytest.fixture(autouse=True)
def _settings_singleton_guard():
    """红线 2：settings 单例钉根——保存/恢复全部字段，防全量混跑串味。"""
    s = get_settings()
    before = dict(s.__dict__)
    try:
        yield
    finally:
        s.__dict__.clear()
        s.__dict__.update(before)


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


# ---------------------------------------------------------------------------
# 假 graph / 假 snap：get_state 返回固定假 snap，update_state 空操作，
# stream 抛指定异常（绝不跑真图、绝不发 LLM 请求）
# ---------------------------------------------------------------------------

class _FakeSnap:
    def __init__(self, values=None, next_nodes=(), tasks=()):
        self.values = values if values is not None else {}
        self.next = tuple(next_nodes)
        self.tasks = tuple(tasks)


class _FakeGraph:
    def __init__(self, snap: _FakeSnap, stream_exc: Exception | None = None):
        self._snap = snap
        self._stream_exc = stream_exc

    def get_state(self, _cfg):
        return self._snap

    def update_state(self, *_a, **_kw):
        return None

    def stream(self, *_a, **_kw):
        if self._stream_exc is not None:
            raise self._stream_exc
        return iter(())


def _set_updated_at(tid: str, dt: datetime) -> None:
    """直接改 batches.updated_at（伪造行龄，immediate/cooperative 分野用）。"""
    from app.db.models import Batch
    from app.db.session import get_session
    with get_session() as s:
        row = s.get(Batch, tid)
        assert row is not None
        row.updated_at = dt
        s.commit()


# ---------- 1. cancel_requested 标志三函数 ----------

def test_cancel_flag_roundtrip():
    tid = "tid-flag-roundtrip"
    batch_store.upsert_batch(tid, status="running")
    assert batch_store.is_cancel_requested(tid) is False
    assert batch_store.set_cancel_requested(tid) is True
    assert batch_store.is_cancel_requested(tid) is True
    assert batch_store.get_batch(tid)["cancel_requested"] is True
    assert batch_store.clear_cancel_requested(tid) is True
    assert batch_store.is_cancel_requested(tid) is False


def test_cancel_flag_missing_row_no_raise():
    """不存在行：set/clear 返回 False，is 返回 False，均不抛。"""
    tid = "tid-flag-missing"
    assert batch_store.set_cancel_requested(tid) is False
    assert batch_store.clear_cancel_requested(tid) is False
    assert batch_store.is_cancel_requested(tid) is False


# ---------- 2. folder_router 检查点 ----------

def test_folder_router_checkpoint_aborts_then_passes():
    tid = "tid-router-abort"
    batch_store.upsert_batch(tid, status="running")
    state = {"batch_id": tid, "pending_factories": [], "deferred_factories": []}

    batch_store.set_cancel_requested(tid)
    with pytest.raises(batch_store.BatchAbortedError):
        folder_router(state)

    batch_store.clear_cancel_requested(tid)
    # 空队列走防御分支正常返回（检查点在弹队列之前）
    assert folder_router(state) == {"current_factory_data": {}}


# ---------- 3. _mark_batch_aborted（钉死：不触发结案备份） ----------

def test_mark_batch_aborted_no_backup_trigger(monkeypatch):
    tid = "tid-mark-aborted"
    batch_store.upsert_batch(tid, status="running")
    batch_store.set_cancel_requested(tid)

    backup_calls: list[str] = []
    monkeypatch.setattr(
        backup, "trigger_async", lambda reason: backup_calls.append(reason))

    service._mark_batch_aborted(tid)

    row = batch_store.get_batch(tid)
    assert row["status"] == "aborted"
    assert row["cancel_requested"] is False  # 标志已清
    assert row["completed_at"] is None       # aborted 不填 completed_at
    assert backup_calls == []                # aborted 不触发结案备份


# ---------- 4. abort_batch 四情形（假 snap） ----------

def test_abort_batch_missing_row_raises_value_error():
    with pytest.raises(ValueError, match="批次不存在"):
        service.abort_batch("tid-abort-missing")


def test_abort_batch_pending_review_raises_runtime_error():
    tid = "tid-abort-pending"
    batch_store.upsert_batch(tid, status="pending_review")
    with pytest.raises(RuntimeError, match="无需终止"):
        service.abort_batch(tid)


def test_abort_batch_already_aborted_raises_runtime_error():
    tid = "tid-abort-twice"
    batch_store.upsert_batch(tid, status="aborted")
    with pytest.raises(RuntimeError, match="已终止"):
        service.abort_batch(tid)


def test_abort_batch_cooperative_when_running_fresh(monkeypatch):
    """running + snap.next 非空 + updated_at 新鲜 → cooperative：
    标志置位、status 仍 running、message 含「当前工厂」。"""
    tid = "tid-abort-coop"
    batch_store.upsert_batch(tid, status="running")
    snap = _FakeSnap(values={"batch_id": tid}, next_nodes=(NODE2,))
    monkeypatch.setattr(service, "get_graph", lambda: _FakeGraph(snap))

    result = service.abort_batch(tid)

    assert result["ok"] is True
    assert result["mode"] == "cooperative"
    assert "当前工厂" in result["message"]
    row = batch_store.get_batch(tid)
    assert row["status"] == "running"            # 协作路径不改状态
    assert row["cancel_requested"] is True       # 标志已置位


def test_abort_batch_immediate_when_stale_no_threads(monkeypatch):
    """running + snap.next 非空 + updated_at 超龄（1 小时前）+ 无线程
    → immediate：status==aborted、message 含「已终止」。"""
    tid = "tid-abort-immediate"
    batch_store.upsert_batch(tid, status="running")
    _set_updated_at(tid, datetime.utcnow() - timedelta(hours=1))
    snap = _FakeSnap(values={"batch_id": tid}, next_nodes=(NODE2,))
    monkeypatch.setattr(service, "get_graph", lambda: _FakeGraph(snap))

    result = service.abort_batch(tid)

    assert result["ok"] is True
    assert result["mode"] == "immediate"
    assert "已终止" in result["message"]
    row = batch_store.get_batch(tid)
    assert row["status"] == "aborted"
    assert row["completed_at"] is None


# ---------- 5. pipeline_state 自愈守卫 ----------

def test_pipeline_state_does_not_overwrite_aborted(monkeypatch):
    """aborted 行 + 假 checkpoint（next=NODE2 推导 desired=running）
    → get_pipeline_state 后 status 仍 aborted（防打回）。"""
    tid = "tid-heal-aborted"
    batch_store.upsert_batch(tid, status="aborted")
    snap = _FakeSnap(
        values={"batch_id": tid, "current_factory_data": {}},
        next_nodes=(NODE2,))
    monkeypatch.setattr(pipeline_state, "get_graph", lambda: _FakeGraph(snap))

    state = pipeline_state.get_pipeline_state(tid)

    assert state["batch"]["status"] == "aborted"
    assert batch_store.get_batch(tid)["status"] == "aborted"


# ---------- 6. reset_to_todo：aborted 放行 / running 仍 409 ----------

def _fake_running_graph(monkeypatch, tid: str) -> None:
    """假 graph：values 非空（get_order_state exists=True）、next 非空、
    无 interrupt——即「活跑中」形态，专门触发 reset 的运行防护。"""
    snap = _FakeSnap(values={"batch_id": tid}, next_nodes=(NODE2,))
    monkeypatch.setattr(service, "get_graph", lambda: _FakeGraph(snap))


def test_reset_to_todo_allows_aborted(watch, monkeypatch):
    """aborted 行 + 活跑形态假 snap → reset_to_todo 放行（协作停止后
    checkpoint 残留 next 非空属预期），批次行随清理删除。"""
    tid = "tid-reset-aborted"
    (watch / "终止批次").mkdir()
    batch_store.upsert_batch(tid, watch_dir=str(watch),
                             folder_name="终止批次", status="aborted")
    _fake_running_graph(monkeypatch, tid)

    r = board.reset_to_todo("终止批次")

    assert r["ok"] is True
    assert r["thread_id"] == tid
    assert batch_store.get_batch(tid) is None


def test_reset_to_todo_running_still_rejected(watch, monkeypatch):
    """running 行 + 同假 snap → 仍抛「批次正在运行」（409 口径不松）。"""
    tid = "tid-reset-running"
    (watch / "运行批次").mkdir()
    batch_store.upsert_batch(tid, watch_dir=str(watch),
                             folder_name="运行批次", status="running")
    _fake_running_graph(monkeypatch, tid)

    with pytest.raises(RuntimeError, match="批次正在运行"):
        board.reset_to_todo("运行批次")


# ---------- 7. 包装层协作停止（代表一条：rerun_with_paths） ----------

def test_rerun_with_paths_batch_aborted_returns_aborted(monkeypatch):
    """stream 抛 BatchAbortedError → 不向上抛，返回 status=aborted，
    批次行落 aborted（六条包装层同模式，此为代表路径）。"""
    tid = "tid-rerun-abort"
    batch_store.upsert_batch(tid, status="running")
    batch_store.set_cancel_requested(tid)
    snap = _FakeSnap(
        values={"batch_id": tid, "downstream_file_path": "x.xlsx"},
        next_nodes=("node5",),
    )
    fake = _FakeGraph(snap, batch_store.BatchAbortedError(tid))
    monkeypatch.setattr(service, "get_graph", lambda: fake)

    result = service.rerun_with_paths(tid, upstream_root="/tmp/u")

    assert result["status"] == "aborted"
    assert result["thread_id"] == tid
    row = batch_store.get_batch(tid)
    assert row["status"] == "aborted"
    assert row["cancel_requested"] is False  # _mark_batch_aborted 已清标志
