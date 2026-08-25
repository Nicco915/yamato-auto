# -*- coding: utf-8 -*-
"""reopen 提交后回写 factory_outputs 快照测试（service.apply_reopen_payload）。

背景 bug：reopen 只写 Excel/DB、不回写 LangGraph 快照，而 reopen 读取数据源
优先级是 快照 > Excel 兜底 → 提交成功后重新打开仍显示改前的值。

修复：批次已结束（无挂起 interrupt）时把提交值回写进 factory_outputs 快照；
批次仍挂起时跳过（update_state 会销毁 Node5 interrupt 任务，实测）。

测试方式：monkeypatch get_graph 返回可控假 state（避开真实跑图到 completed
的高成本路径），writer 三件套（_ensure_output_copy/_write_excel/_upsert_db）
与 _write_audit 同样打桩——本测试聚焦「何时回写、回写什么」。

覆盖：
- completed + dict 快照 → 回写 calculated_items，其余快照字段保留
- completed + 旧格式 list 快照 → 整表替换
- completed + 无快照（Excel 兜底场景）→ 不写（Excel 自一致）
- pending_review（next 非空）→ 绝不回写（防 interrupt 销毁）

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/reopen_snapshot_writeback_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

import pytest  # noqa: E402

from app.api import service  # noqa: E402
from app.nodes import writer as writer_mod  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_reopen_snap_test_")

TID = "TEST-REOPEN-SNAPSHOT"
FACTORY = "中地"

ITEMS = [{
    "sku": "1234567890123",
    "extracted_data": {"total_quantity": 100, "total_net_weight": 1680.0,
                       "total_gross_weight": 1800.0, "weight_unit": "KG"},
    "calculation": {"calculated_unit_net": 16.8, "calculated_unit_gross": 18.0},
    "is_human_edited": True,
}]


class _FakeGraph:
    """可控假 graph：get_state 返回构造的 state，update_state 记录调用。"""

    def __init__(self, values, next_nodes=()):
        self._values = values
        self._next = tuple(next_nodes)
        self.update_calls: list[dict] = []

    def get_state(self, _config):
        return SimpleNamespace(values=self._values, next=self._next, tasks=[])

    def update_state(self, _config, values, as_node=None):
        self.update_calls.append({"values": values, "as_node": as_node})
        self._values.update(values)


def _make_values(snapshot):
    return {
        "batch_id": TID,
        "downstream_file_path": "/tmp/downstream.xlsx",
        "downstream_row_map": {},
        "factory_outputs": {FACTORY: snapshot} if snapshot is not None else {},
        "current_factory_data": {},
    }


@pytest.fixture
def patched(monkeypatch):
    """打桩 writer 三件套 + _write_audit，返回可控的 fake graph 工厂。"""
    monkeypatch.setattr(writer_mod, "_ensure_output_copy",
                        lambda state: TMP / "out.xlsx")
    monkeypatch.setattr(writer_mod, "_write_excel", lambda state, path: 3)
    monkeypatch.setattr(writer_mod, "_upsert_db", lambda state: (0, 0))
    monkeypatch.setattr(service, "_write_audit", lambda *a, **k: None)

    def install(values, next_nodes=()):
        fake = _FakeGraph(values, next_nodes)
        monkeypatch.setattr(service, "get_graph", lambda: fake)
        return fake
    return install


def test_completed_dict_snapshot_written_back(patched):
    snapshot = {"factory_name": FACTORY,
                "calculated_items": [{"sku": "1234567890123",
                                      "extracted_data": {"total_net_weight": 1679.99999}}],
                "folder_path": "/upstream/中地"}
    fake = patched(_make_values(snapshot), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert len(fake.update_calls) == 1
    written = fake.update_calls[0]["values"]["factory_outputs"][FACTORY]
    assert written["calculated_items"] == ITEMS
    # 快照其余字段保留
    assert written["folder_path"] == "/upstream/中地"


def test_completed_list_snapshot_replaced(patched):
    fake = patched(_make_values([{"sku": "old"}]), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["values"]["factory_outputs"][FACTORY] == ITEMS


def test_completed_no_snapshot_no_write(patched):
    fake = patched(_make_values(None), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert fake.update_calls == []


def test_pending_review_never_writes(patched):
    """批次仍挂起 Node5 interrupt：绝不 update_state（防销毁 interrupt）。"""
    snapshot = {"factory_name": FACTORY, "calculated_items": [{"sku": "old"}]}
    fake = patched(_make_values(snapshot), next_nodes=("node5_human_review",))
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert fake.update_calls == []
