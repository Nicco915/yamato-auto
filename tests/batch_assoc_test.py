# -*- coding: utf-8 -*-
"""建批显式关联 + 删批次清行测试（service.py）。

覆盖：
- create_batch 路径落进监控目录一级子文件夹 → batches 行写 folder_name/
  watch_dir 显式关联（与扫描建批同口径）；在监控目录外/未配置 → 不写；
- delete_batch 清 checkpoint 后同步删除 batches 业务行（不再留孤儿行
  被监控看板配对成死链接）；review_audits 留痕不受影响。

隔离（血泪红线）：先 import 全部 app 模块，再 isolate_to_tmp。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/batch_assoc_test.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_batch_assoc_test_")


@pytest.fixture()
def watch(tmp_path):
    """独立监控目录；结束还原 settings 单例。"""
    w = tmp_path / "watch"
    w.mkdir()
    original = get_settings().watch_dir
    get_settings().watch_dir = str(w)
    try:
        yield w
    finally:
        get_settings().watch_dir = original


def _fake_run_ok(result=None):
    """替代 run_until_interrupt：关联/删除逻辑不需要真跑图。"""
    return result or {"status": "pending_human_review"}


# ---------- create_batch 显式关联 ----------

def test_create_batch_writes_watch_association(watch, tmp_path, monkeypatch):
    folder = watch / "批次X"
    upstream = folder / "上游"
    upstream.mkdir(parents=True)
    manifest = folder / "ContentsOfTheContainer_X.xlsx"
    manifest.touch()

    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: _fake_run_ok())
    service.create_batch("assoc-1", str(manifest), str(upstream))

    row = batch_store.get_batch("assoc-1")
    assert row is not None
    assert row["folder_name"] == "批次X"
    assert row["watch_dir"] == str(watch.resolve())


def test_create_batch_outside_watch_no_association(watch, tmp_path, monkeypatch):
    upstream = tmp_path / "别的上游"
    upstream.mkdir()
    manifest = tmp_path / "ContentsOfTheContainer_Y.xlsx"
    manifest.touch()

    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: _fake_run_ok())
    service.create_batch("assoc-2", str(manifest), str(upstream))

    row = batch_store.get_batch("assoc-2")
    assert row is not None
    assert not row["folder_name"]
    assert not row["watch_dir"]


def test_create_batch_no_watch_dir_no_association(tmp_path, monkeypatch):
    original = get_settings().watch_dir
    get_settings().watch_dir = None
    try:
        upstream = tmp_path / "上游"
        upstream.mkdir()
        manifest = tmp_path / "m.xlsx"
        manifest.touch()
        monkeypatch.setattr(service, "run_until_interrupt",
                            lambda *a, **kw: _fake_run_ok())
        service.create_batch("assoc-3", str(manifest), str(upstream))
        row = batch_store.get_batch("assoc-3")
        assert row is not None
        assert not row["folder_name"]
    finally:
        get_settings().watch_dir = original


def test_create_batch_assoc_failure_never_blocks(tmp_path, monkeypatch):
    """监控目录配置指向不存在路径：静默跳过关联，建批照常。"""
    original = get_settings().watch_dir
    get_settings().watch_dir = str(tmp_path / "不存在的监控目录")
    try:
        upstream = tmp_path / "上游2"
        upstream.mkdir()
        manifest = tmp_path / "m2.xlsx"
        manifest.touch()
        monkeypatch.setattr(service, "run_until_interrupt",
                            lambda *a, **kw: _fake_run_ok())
        service.create_batch("assoc-4", str(manifest), str(upstream))
        row = batch_store.get_batch("assoc-4")
        assert row is not None
        assert not row["folder_name"]
    finally:
        get_settings().watch_dir = original


# ---------- delete_batch 同步清 batches 行 ----------

def test_delete_batch_clears_batch_row():
    tid = "del-assoc-1"
    batch_store.upsert_batch(tid, watch_dir="/w", folder_name="批次Z",
                             status="completed")
    # 造终态 checkpoint（NODE7 之后 next 为空 → completed，允许删除）
    graph = get_graph()
    cfg = {"configurable": {"thread_id": tid}}
    graph.update_state(cfg, {"final_output_path": "/tmp/out.xlsx"},
                       as_node=NODE7)

    result = service.delete_batch(tid)
    assert result["deleted"] == tid

    # batches 行业务行一并删除，不再留孤儿行
    assert batch_store.get_batch(tid) is None
    # checkpoint 已清
    assert not graph.get_state(cfg).values
