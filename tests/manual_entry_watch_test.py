# -*- coding: utf-8 -*-
"""第七阶段：factory_watch 对手动录入厂的隔离（§2.6）。

覆盖：
1. 只有 manual_skip_factories 中的厂缺资料时，资料到达也不自动触发补充；
2. 同一批次里非手动的缺口厂仍照常触发（隔离不误伤）。

隔离（血泪红线 2026-08-11）：import app 前设 YAMATO_TEST_MODE +
YAMATO_DOTENV_PATH 临时空 .env；import 后 isolate_to_tmp 指向临时目录，
并清空监控模块级状态，绝不碰真实上游目录/库。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

_TMP = Path(tempfile.mkdtemp(prefix="yamato_manual_watch_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402
from app.orchestrator import factory_watch  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_manual_watch_test_")

_CREATED_TIDS: list[str] = []


@pytest.fixture(autouse=True)
def _clean_watch_state(tmp_path):
    factory_watch.stop_factory_watch()
    with factory_watch._state_lock:
        factory_watch._snapshots.clear()
        factory_watch._cooldowns.clear()
        factory_watch._last_triggered.clear()
    s = get_settings()
    original = s.upstream_root
    empty = tmp_path / "empty_upstream"
    empty.mkdir()
    s.upstream_root = str(empty)
    try:
        yield
    finally:
        s.upstream_root = original
        factory_watch.stop_factory_watch()
        with factory_watch._state_lock:
            factory_watch._snapshots.clear()
            factory_watch._cooldowns.clear()
            factory_watch._last_triggered.clear()
        for tid in _CREATED_TIDS:
            batch_store.delete_batch(tid)
        _CREATED_TIDS.clear()


def _make_awaiting_batch(tid: str, upstream: Path, *,
                         requirements: list[str],
                         manual_skip: list[str]) -> None:
    batch_store.upsert_batch(tid, status="awaiting_factories",
                             folder_name=tid)
    _CREATED_TIDS.append(tid)
    get_graph().update_state({"configurable": {"thread_id": tid}}, {
        "batch_id": tid,
        "downstream_requirements": {f: ["4900000009571"] for f in requirements},
        "factory_outputs": {},
        "manual_skip_factories": manual_skip,
        "upstream_root": str(upstream),
        "final_output_path": "/tmp/out.xlsx",
    }, as_node=NODE7)


def _drop_doc(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "packing.pdf").write_bytes(b"x" * 128)


def _record_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_add(tid: str):
        calls.append(tid)
        return {"added": 1, "factories": [], "status": "pending_human_review"}

    monkeypatch.setattr(service, "add_factories_to_batch", fake_add)
    return calls


def _only_batch(monkeypatch, tid: str) -> None:
    """隔离同进程其他测试残留的 awaiting 批次，只扫本用例这一条。"""
    def fake_list(status: str):
        return [batch_store.get_batch(tid)] if status == "awaiting_factories" else []

    monkeypatch.setattr(batch_store, "list_by_status", fake_list)


def test_manual_factory_not_triggered_by_new_documents(tmp_path, monkeypatch):
    tid = "MANUAL-WATCH-SKIP"
    upstream = tmp_path / "upstream_skip"
    _make_awaiting_batch(
        tid, upstream,
        requirements=["手动厂"], manual_skip=["手动厂"])
    _drop_doc(upstream / "手动厂")
    _only_batch(monkeypatch, tid)
    calls = _record_calls(monkeypatch)

    factory_watch._scan_once()
    factory_watch._scan_once()

    assert calls == []
    assert not any(k[1] == "手动厂" for k in factory_watch._snapshots)


def test_non_manual_gap_still_triggers(tmp_path, monkeypatch):
    tid = "MANUAL-WATCH-MIXED"
    upstream = tmp_path / "upstream_mixed"
    _make_awaiting_batch(
        tid, upstream,
        requirements=["手动厂", "正常厂"], manual_skip=["手动厂"])
    _drop_doc(upstream / "手动厂")
    _drop_doc(upstream / "正常厂")
    _only_batch(monkeypatch, tid)
    calls = _record_calls(monkeypatch)

    factory_watch._scan_once()
    factory_watch._scan_once()

    assert calls == [tid]
    assert not any(k[1] == "手动厂" for k in factory_watch._last_triggered)