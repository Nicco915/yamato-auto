# -*- coding: utf-8 -*-
"""监控目录看板服务测试（app/orchestrator/board.py）。

覆盖：
- board_state 三档分类（已完成/执行中/未执行候选）+ 候选装箱单探测；
- mark_done：正常标记、幂等、已占用文件夹查重、文件夹不存在；
- start_from_board：预写 running 行、后台线程参数透传、重复启动查重。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/board_test.py -v
"""
from __future__ import annotations

import os
import sys
import threading
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
from app.orchestrator import board  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_board_test_")


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


# ---------- board_state ----------

def test_board_state_three_tiers(watch):
    (watch / "已完成批次").mkdir()
    (watch / "进行中批次").mkdir()
    cand = watch / "候选批次"
    cand.mkdir()
    _make_xlsx(cand / "ContentsOfTheContainer_001.xlsx")

    batch_store.upsert_batch("done-1", watch_dir=str(watch),
                             folder_name="已完成批次", status="completed")
    batch_store.update_status("done-1", "completed")  # 填充 completed_at
    batch_store.upsert_batch("run-1", watch_dir=str(watch),
                             folder_name="进行中批次", status="pending_review")

    state = board.board_state()
    assert state["total"] == 3
    assert _names(state["done"]) == {"已完成批次"}
    assert _names(state["in_progress"]) == {"进行中批次"}
    assert _names(state["candidates"]) == {"候选批次"}

    done_row = state["done"][0]
    assert done_row["thread_id"] == "done-1"
    assert done_row["completed_at"]  # update_status 已填充

    cand_row = state["candidates"][0]
    assert cand_row["has_content"] is True
    assert cand_row["default_thread_id"] == "候选批次"
    assert len(cand_row["downstream_candidates"]) == 1


def test_board_state_candidate_without_content(watch):
    (watch / "空文件夹").mkdir()
    state = board.board_state()
    assert _names(state["candidates"]) == {"空文件夹"}
    assert state["candidates"][0]["has_content"] is False


def test_board_state_watch_dir_missing():
    original = get_settings().watch_dir
    get_settings().watch_dir = str(TMP / "不存在的监控目录")
    try:
        with pytest.raises(ValueError):
            board.board_state()
    finally:
        get_settings().watch_dir = original


# ---------- mark_done ----------

def test_mark_done_happy_and_idempotent(watch):
    (watch / "历史批次").mkdir()

    r = board.mark_done("历史批次")
    assert r["ok"] is True
    assert r["thread_id"] == "历史批次"

    row = batch_store.get_batch("历史批次")
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_at"]

    # 标记后从候选挪到已完成
    state = board.board_state()
    assert _names(state["done"]) == {"历史批次"}
    assert _names(state["candidates"]) == set()

    # 重复标记：幂等返回 ok，不报错
    r2 = board.mark_done("历史批次")
    assert r2["ok"] is True


def test_mark_done_rejects_occupied_folder(watch):
    (watch / "占用批次").mkdir()
    batch_store.upsert_batch("run-x", watch_dir=str(watch),
                             folder_name="占用批次", status="running")
    with pytest.raises(FileExistsError):
        board.mark_done("占用批次")


def test_mark_done_missing_folder(watch):
    with pytest.raises(FileNotFoundError):
        board.mark_done("不存在")
    with pytest.raises(ValueError):
        board.mark_done("  ")


# ---------- mark_done 关联已有批次 ----------

def test_mark_done_bind_existing_batch(watch):
    """文件夹其实就是已完成的 test-1：绑定后详情/对话落到真批次。"""
    (watch / "旧文件夹").mkdir()
    batch_store.upsert_batch("bind-t1", status="completed")
    batch_store.update_status("bind-t1", "completed")

    r = board.mark_done("旧文件夹", thread_id="bind-t1")
    assert r["ok"] is True
    assert r["linked"] is True
    assert r["thread_id"] == "bind-t1"

    row = batch_store.get_batch("bind-t1")
    assert row["folder_name"] == "旧文件夹"
    assert row["watch_dir"] == str(watch)
    assert row["status"] == "completed"  # 状态不动

    # 看板配对到真批次（规则② folder_name），已完成档批次号是 bind-t1
    state = board.board_state()
    assert _names(state["done"]) == {"旧文件夹"}
    assert state["done"][0]["thread_id"] == "bind-t1"
    assert state["done"][0]["has_checkpoint"] is False  # 无 checkpoint（未跑图）

    # 重复标记：幂等
    r2 = board.mark_done("旧文件夹", thread_id="bind-t1")
    assert r2["ok"] is True


def test_mark_done_bind_validation(watch):
    (watch / "文件夹A").mkdir()
    # 批次不存在 → 404 语义
    with pytest.raises(FileNotFoundError):
        board.mark_done("文件夹A", thread_id="不存在批次")
    # 批次未完成 → 422 语义
    batch_store.upsert_batch("bind-run9", status="running")
    with pytest.raises(ValueError):
        board.mark_done("文件夹A", thread_id="bind-run9")
    # 批次已绑定别的文件夹 → 409 语义
    batch_store.upsert_batch("bind-done9", status="completed",
                             folder_name="别的文件夹")
    with pytest.raises(FileExistsError):
        board.mark_done("文件夹A", thread_id="bind-done9")
    # 校验失败不留下任何改动
    assert batch_store.get_batch("bind-done9")["folder_name"] == "别的文件夹"
    assert batch_store.get_batch("文件夹A") is None


# ---------- has_checkpoint ----------

def test_board_state_has_checkpoint_and_orphan(watch):
    """有 checkpoint 的行 has_checkpoint=True；无 checkpoint 的残留行
    has_checkpoint=False 且不被自愈误改状态。"""
    (watch / "真完成").mkdir()
    (watch / "残留").mkdir()
    batch_store.upsert_batch("cp-1", watch_dir=str(watch),
                             folder_name="真完成", status="completed")
    # 造终态 checkpoint（隔离后 checkpoint_db_abs 指向临时库）
    from app.graph import NODE7, get_graph
    graph = get_graph()
    graph.update_state({"configurable": {"thread_id": "cp-1"}},
                       {"final_output_path": "/tmp/o.xlsx"}, as_node=NODE7)
    batch_store.upsert_batch("orphan-1", watch_dir=str(watch),
                             folder_name="残留", status="pending_review")

    state = board.board_state()
    done = {d["folder_name"]: d for d in state["done"]}
    prog = {d["folder_name"]: d for d in state["in_progress"]}
    assert done["真完成"]["has_checkpoint"] is True
    # 卡片操作字段透传（checkpoint 里造的 final_output_path）
    assert done["真完成"]["final_output_path"] == "/tmp/o.xlsx"
    assert done["真完成"]["split_thread_id"] is None
    assert done["真完成"]["declarations_ready"] is False
    assert prog["残留"]["has_checkpoint"] is False
    # 残留行跳过自愈：状态不被误标 running，数据库行也不被回写
    assert prog["残留"]["status"] == "pending_review"
    assert batch_store.get_batch("orphan-1")["status"] == "pending_review"


# ---------- start_from_board ----------

def test_start_from_board_prewrite_and_dispatch(watch, monkeypatch):
    cand = watch / "新批次"
    cand.mkdir()
    _make_xlsx(cand / "ContentsOfTheContainer_001.xlsx")

    called = {}
    done_event = threading.Event()

    def fake_start(folder_name, thread_id=None, downstream_file_path=None, **kw):
        called.update(folder_name=folder_name, thread_id=thread_id,
                      downstream_file_path=downstream_file_path)
        done_event.set()
        return {"status": "pending_human_review"}

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)

    r = board.start_from_board("新批次", thread_id="tid-001")
    assert r["ok"] is True
    assert r["thread_id"] == "tid-001"

    # 预写 running 行：HTTP 返回时看板即可把文件夹挪到「执行中」
    row = batch_store.get_batch("tid-001")
    assert row is not None
    assert row["status"] == "running"
    assert row["folder_name"] == "新批次"

    # 后台线程参数透传
    assert done_event.wait(timeout=10)
    assert called == {"folder_name": "新批次", "thread_id": "tid-001",
                      "downstream_file_path": None}

    # 文件夹从候选消失（三重匹配之 folder_name 命中）
    state = board.board_state()
    assert _names(state["candidates"]) == set()
    assert _names(state["in_progress"]) == {"新批次"}


def test_start_from_board_default_thread_id(watch, monkeypatch):
    (watch / "批次 95").mkdir()
    monkeypatch.setattr(
        service, "start_batch_from_scan",
        lambda *a, **kw: {"status": "pending_human_review"})
    r = board.start_from_board("批次 95")
    assert r["thread_id"] == "批次_95"  # pick_default_thread_id 消毒空格
    assert batch_store.get_batch("批次_95")["status"] == "running"


def test_start_from_board_rejects_duplicates(watch, monkeypatch):
    (watch / "已占用").mkdir()
    batch_store.upsert_batch("other-tid", watch_dir=str(watch),
                             folder_name="已占用", status="running")
    monkeypatch.setattr(
        service, "start_batch_from_scan",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应被调用")))
    with pytest.raises(FileExistsError):
        board.start_from_board("已占用", thread_id="new-tid")
    # 预写行不应残留
    assert batch_store.get_batch("new-tid") is None


def test_start_from_board_missing_folder(watch):
    with pytest.raises(FileNotFoundError):
        board.start_from_board("不存在")
