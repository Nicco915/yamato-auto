# -*- coding: utf-8 -*-
"""第七阶段：service 层手动录入测试（预检 / 补充工厂 reopen / 状态透出）。

覆盖：
1. precheck_manual_completeness：三列全填 complete=True、部分填
   complete=False、MX 行不计入、无三列 complete=False、子文件夹/装箱单
   异常抛 ValueError；
2. add_factories_to_batch reopen_factories：手动厂默认不自动补回；显式
   reopen 后从 manual_skip_factories 移除、进入 pending、返回 reopened；
   非手动厂/不在装箱单的 reopen 请求被忽略；
3. pipeline_state / board：manual_factories 透出、手动厂不计
   factories_failed；
4. get_batch_detail：manual 厂 source=manual / status=manual_entry。

隔离（血泪红线 2026-08-11）：import app 前设 YAMATO_TEST_MODE +
YAMATO_DOTENV_PATH 临时空 .env；import 后 isolate_to_tmp 指向临时目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

_TMP = Path(tempfile.mkdtemp(prefix="yamato_manual_service_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402
from app.orchestrator import board, pipeline_state  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_manual_service_test_")

KEY_HEADERS = ["MAKER_MEI_KJ", "SHOHIN_CD", "PURCHASE_ORDER",
               "SOTOBAKO_D_HACCHU_SU", "中文品名", "净重", "毛重"]
THREE_COLS = ["中文品名", "净重", "毛重"]
COL = {h: KEY_HEADERS.index(h) + 1 for h in KEY_HEADERS}


@pytest.fixture(autouse=True)
def _pin_paths(monkeypatch):
    s = get_settings()
    orig_out, orig_watch = s.output_dir, s.watch_dir
    s.output_dir = str(TMP / "output")
    # 结案自动备份会起后台线程，与备份测试撞锁——统一打成空函数
    from app.orchestrator import backup
    monkeypatch.setattr(backup, "trigger_async", lambda *a, **k: True)
    try:
        yield
    finally:
        s.output_dir = orig_out
        s.watch_dir = orig_watch


@pytest.fixture()
def watch(tmp_path):
    w = tmp_path / "watch"
    w.mkdir()
    get_settings().watch_dir = str(w)
    return w


def _write_book(folder: Path, name: str, rows: list[dict],
                headers: list[str] | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    wb = Workbook()
    ws = wb.active
    hs = headers or KEY_HEADERS
    ws.append(hs)
    for row in rows:
        ws.append([row.get(h) for h in hs])
    wb.save(path)
    return path


def _by_name(result: dict) -> dict[str, dict]:
    return {f["name"]: f for f in result["factories"]}


# ---------------------------------------------------------------------------
# 1. precheck_manual_completeness
# ---------------------------------------------------------------------------

def test_precheck_complete_partial_and_mx_excluded(watch):
    folder = watch / "预检批次"
    _write_book(folder, "ContentsOfTheContainer_001.xlsx", [
        # 完整厂：两行三列全填
        {"MAKER_MEI_KJ": "完整厂", "SHOHIN_CD": "4900000009501",
         "中文品名": "钳子", "净重": 1.2, "毛重": 2.3},
        {"MAKER_MEI_KJ": "完整厂", "SHOHIN_CD": "4900000009502",
         "中文品名": "扳手", "净重": 3.4, "毛重": 4.5},
        # 部分厂：第二行只有品名
        {"MAKER_MEI_KJ": "部分厂", "SHOHIN_CD": "4900000009503",
         "中文品名": "螺丝刀", "净重": 5.6, "毛重": 6.7},
        {"MAKER_MEI_KJ": "部分厂", "SHOHIN_CD": "4900000009504",
         "中文品名": "锤子"},
        # MX 行不计入：普通行完整 + MX 行三列空
        {"MAKER_MEI_KJ": "MX厂", "SHOHIN_CD": "4900000009505",
         "中文品名": "MX品名", "净重": 7.8, "毛重": 8.9},
        {"MAKER_MEI_KJ": "MX厂", "SHOHIN_CD": "MXSKU001",
         "PURCHASE_ORDER": "MX2-268510-001"},
    ])

    result = service.precheck_manual_completeness("预检批次")
    rows = _by_name(result)

    assert rows["完整厂"] == {
        "name": "完整厂", "complete": True,
        "filled_rows": 2, "total_rows": 2}
    assert rows["部分厂"]["complete"] is False
    assert rows["部分厂"]["filled_rows"] == 1
    assert rows["部分厂"]["total_rows"] == 2
    assert rows["MX厂"] == {
        "name": "MX厂", "complete": True,
        "filled_rows": 1, "total_rows": 1}


def test_precheck_without_three_columns_is_incomplete(watch):
    folder = watch / "无三列批次"
    _write_book(folder, "ContentsOfTheContainer.xlsx", [
        {"MAKER_MEI_KJ": "甲厂", "SHOHIN_CD": "4900000009511"},
        {"MAKER_MEI_KJ": "甲厂", "SHOHIN_CD": "4900000009512"},
    ], headers=["MAKER_MEI_KJ", "SHOHIN_CD", "SOTOBAKO_D_HACCHU_SU"])

    result = service.precheck_manual_completeness("无三列批次")
    assert result["factories"] == [{
        "name": "甲厂", "complete": False,
        "filled_rows": 0, "total_rows": 2}]


def test_precheck_errors_are_value_error(watch):
    with pytest.raises(ValueError, match="子文件夹不存在"):
        service.precheck_manual_completeness("不存在的批次")

    (watch / "空批次").mkdir()
    with pytest.raises(ValueError, match="未找到"):
        service.precheck_manual_completeness("空批次")

    multi = watch / "多装箱单"
    _write_book(multi, "ContentsOfTheContainer_001.xlsx", [])
    _write_book(multi, "ContentsOfTheContainer_002.xlsx", [])
    with pytest.raises(ValueError, match="发现多个"):
        service.precheck_manual_completeness("多装箱单")

    bad = watch / "坏装箱单"
    _write_book(bad, "ContentsOfTheContainer.xlsx",
                [{"中文品名": "只有三列"}], headers=THREE_COLS)
    with pytest.raises(ValueError, match="缺少关键列"):
        service.precheck_manual_completeness("坏装箱单")


def test_precheck_accepts_explicit_downstream_path(watch):
    """多装箱单：显式指定文件可预检（面板按厂勾选依赖），路径越界拒绝。"""
    multi = watch / "多装箱单指定"
    chosen = _write_book(multi, "ContentsOfTheContainer_001.xlsx", [
        {"MAKER_MEI_KJ": "指定厂", "SHOHIN_CD": "4900000009521",
         "中文品名": "钳子", "净重": 1.0, "毛重": 2.0},
    ])
    _write_book(multi, "ContentsOfTheContainer_002.xlsx", [])

    # 不指定：仍按既有语义报「发现多个」
    with pytest.raises(ValueError, match="发现多个"):
        service.precheck_manual_completeness("多装箱单指定")

    result = service.precheck_manual_completeness(
        "多装箱单指定", str(chosen))
    assert result["factories"] == [{
        "name": "指定厂", "complete": True,
        "filled_rows": 1, "total_rows": 1}]

    outside = watch / "别处.xlsx"
    outside.write_bytes(chosen.read_bytes())
    with pytest.raises(ValueError, match="不在候选文件夹内"):
        service.precheck_manual_completeness("多装箱单指定", str(outside))
    with pytest.raises(ValueError, match="不存在或不是文件"):
        service.precheck_manual_completeness(
            "多装箱单指定", str(multi / "no_such.xlsx"))


# ---------------------------------------------------------------------------
# 2. add_factories_to_batch reopen_factories
# ---------------------------------------------------------------------------

class _FakeGraph:
    """最小 graph 替身：只提供 get_state/update_state/stream，不跑真图。"""

    def __init__(self, values: dict):
        self.values = dict(values)
        self.updates: list[tuple[dict, str | None]] = []

    def get_state(self, cfg):
        return SimpleNamespace(values=dict(self.values), next=[], tasks=[])

    def update_state(self, cfg, update, as_node=None):
        self.updates.append((dict(update), as_node))
        self.values.update(update)

    def stream(self, *args, **kwargs):
        return iter(())


def _fake_batch(monkeypatch, tmp_path, batch: str, *,
                requirements: dict[str, list[str]],
                manual_skip: list[str],
                outputs: dict | None = None) -> _FakeGraph:
    src = tmp_path / f"{batch}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SOTOBAKO_D_HACCHU_SU"])
    for factory, skus in requirements.items():
        for sku in skus:
            ws.append([factory, sku, 10])
    wb.save(src)

    upstream = tmp_path / f"upstream_{batch}"
    upstream.mkdir()
    service._write_batch_config(batch, {
        "thread_id": batch,
        "downstream_file_path": str(src),
        "upstream_root": str(upstream),
        "created_at": "2026-10-28T00:00:00",
        "last_run_at": "2026-10-28T00:00:00",
        "run_count": 1,
    })
    batch_store.upsert_batch(batch, status="completed", folder_name=batch)

    values = {
        "batch_id": batch,
        "downstream_file_path": str(src),
        "upstream_root": str(upstream),
        "downstream_requirements": requirements,
        "downstream_row_map": {f: {s: [2] for s in skus}
                               for f, skus in requirements.items()},
        "factory_outputs": outputs or {},
        "pending_factories": [],
        "manual_skip_factories": list(manual_skip),
    }
    fake = _FakeGraph(values)
    monkeypatch.setattr(service, "get_graph", lambda: fake)
    return fake


def test_add_factories_does_not_readd_manual_by_default(monkeypatch, tmp_path):
    batch = "MANUAL-ADD-DEFAULT"
    fake = _fake_batch(
        monkeypatch, tmp_path, batch,
        requirements={"手动厂": ["4900000009521"]},
        manual_skip=["手动厂"])

    result = service.add_factories_to_batch(batch)

    assert result["added"] == 0
    assert result["reopened"] == []
    assert result["factories"] == []
    assert fake.updates == []
    assert fake.values["manual_skip_factories"] == ["手动厂"]


def test_add_factories_reopen_moves_manual_to_pending(monkeypatch, tmp_path):
    batch = "MANUAL-ADD-REOPEN"
    fake = _fake_batch(
        monkeypatch, tmp_path, batch,
        requirements={
            "手动厂": ["4900000009531"],
            "正常厂": ["4900000009532"],
        },
        manual_skip=["手动厂"])

    result = service.add_factories_to_batch(
        batch, reopen_factories=["手动厂"])

    assert result["reopened"] == ["手动厂"]
    assert "手动厂" in result["factories"]
    update, _anchor = fake.updates[-1]
    assert "手动厂" in update["pending_factories"]
    assert update["manual_skip_factories"] == []
    assert fake.values["manual_skip_factories"] == []


def test_add_factories_ignores_non_manual_and_unknown_reopen(
        monkeypatch, tmp_path):
    batch = "MANUAL-ADD-IGNORE"
    fake = _fake_batch(
        monkeypatch, tmp_path, batch,
        requirements={
            "手动厂": ["4900000009541"],
            "正常厂": ["4900000009542"],
        },
        manual_skip=["手动厂"])

    result = service.add_factories_to_batch(
        batch, reopen_factories=["正常厂", "装箱单外的厂"])

    assert result["reopened"] == []
    update, _anchor = fake.updates[-1]
    assert update["pending_factories"] == ["正常厂"]
    assert "手动厂" not in update["pending_factories"]
    assert "manual_skip_factories" not in update
    assert fake.values["manual_skip_factories"] == ["手动厂"]


# ---------------------------------------------------------------------------
# 3./4. pipeline_state / board / get_batch_detail
# ---------------------------------------------------------------------------

def test_pipeline_state_and_board_expose_manual_factories(tmp_path):
    tid = "MANUAL-STATE-BOARD"
    folder_name = "手动批次"
    watch = tmp_path / "watch_state"
    (watch / folder_name).mkdir(parents=True)
    batch_store.upsert_batch(
        tid, watch_dir=str(watch), folder_name=folder_name,
        status="completed")

    graph = get_graph()
    graph.update_state({"configurable": {"thread_id": tid}}, {
        "batch_id": tid,
        "downstream_file_path": str(tmp_path / "manifest.xlsx"),
        "downstream_requirements": {
            "已完成厂": ["4900000009551"],
            "手动厂": ["4900000009552"],
        },
        "factory_outputs": {"已完成厂": {"calculated_items": []}},
        "manual_skip_factories": ["手动厂"],
        "final_output_path": str(tmp_path / "out.xlsx"),
    }, as_node=NODE7)

    pref = get_settings().watch_dir
    get_settings().watch_dir = str(watch)
    try:
        state = pipeline_state.get_pipeline_state(tid)
        extract = state["extract"]
        assert extract["manual_factories"] == ["手动厂"]
        assert extract["factories_failed"] == 0
        assert extract["failed_factories"] == []

        board_state = board.board_state()
    finally:
        get_settings().watch_dir = pref

    done_row = next(r for r in board_state["done"]
                    if r["folder_name"] == folder_name)
    assert done_row["manual_factories"] == ["手动厂"]
    assert done_row["factories_failed"] == 0


def test_batch_detail_marks_manual_source_and_status():
    tid = "MANUAL-DETAIL"
    graph = get_graph()
    graph.update_state({"configurable": {"thread_id": tid}}, {
        "batch_id": tid,
        "downstream_file_path": "/tmp/manifest.xlsx",
        "downstream_requirements": {
            "已完成厂": ["4900000009561"],
            "手动厂": ["4900000009562"],
        },
        "factory_outputs": {"已完成厂": {"calculated_items": []}},
        "manual_skip_factories": ["手动厂"],
        "final_output_path": "/tmp/out.xlsx",
    }, as_node=NODE7)

    detail = service.get_batch_detail(tid)
    by_name = {f["factory"]: f for f in detail["factories"]}

    assert by_name["手动厂"]["role"] == "manual_entry"
    assert detail["progress"]["done"] == detail["progress"]["total"] == 2
    assert service._gap_factories(graph.get_state({"configurable": {"thread_id": tid}}).values) == []
    assert by_name["手动厂"]["source"] == "manual"
    assert by_name["手动厂"]["status"] == "manual_entry"
    assert by_name["已完成厂"]["source"] == "auto"
    assert by_name["已完成厂"]["status"] == "done"


def test_gate_checkpoint_does_not_report_write_complete():
    """确认清单持久化的 Node6 自循环不能误报写回完成。"""
    from app.graph import NODE6

    events = []
    emit = service._make_progress_emitter(events.append, seed={
        "downstream_requirements": {"自动厂": []},
        "current_factory_data": {"factory_name": "自动厂"},
    })
    emit({NODE6: {"overwrite_gate": {"factory": "自动厂", "conflicts": []}}})
    assert events == []
    emit({NODE6: {"overwrite_gate": None, "factory_outputs": {"自动厂": {}}}})
    assert events[0]["message"] == "自动厂 写回完成"
