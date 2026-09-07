# -*- coding: utf-8 -*-
"""提交时「是否更新历史单重」回归测试（2026-09 用户需求）。

背景：单重波动 Warning（与主库历史单重差异超阈值）可能是真实长期变化
（换包材等）。原设计只有人工编辑过数值才刷新主库历史单重，导致合理波动
每批都重复告警。新增：提交前差异弹窗的单重波动专区里逐个/全选勾选，
勾选项携带 update_history_weight=True，Node6 即使数值未人工编辑也刷新
主库历史单重；不勾选则保持旧基准（防误识别污染历史数据）。

覆盖：
- _merge_human_items：update_history_weight 标志透传进 calculated_items
- writer._upsert_db：带标志且未人工编辑的老 SKU → 重量刷新；
  不带标志且未编辑 → 不刷新（防污染默认）
- service._prepare_audit / _prepare_audit_changes_from_items：
  勾选生成扁平结构「历史单重」审计条目

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/weight_history_update_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from sqlalchemy import select  # noqa: E402

from app.api import service  # noqa: E402
from app.db.models import Factory, FactorySKU  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.nodes import human_review as hr  # noqa: E402
from app.nodes import writer as writer_mod  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_weight_update_test_")

FACTORY = "波动测试厂"
SKU = "4909876543210"


# ---------------------------------------------------------------------------
# 1. _merge_human_items：标志透传
# ---------------------------------------------------------------------------

def _orig_item():
    return {
        "sku": SKU,
        "extracted_data": {"total_quantity": 10, "total_net_weight": 200.0,
                           "total_gross_weight": 220.0},
        "calculation": {"calculated_unit_net": 20.0,
                        "calculated_unit_gross": 22.0},
        "db_record": {"unit_net_weight": 16.8, "unit_gross_weight": 18.0},
        "status": "Warning",
    }


def test_merge_passthrough_update_history_weight():
    human = [{"sku": SKU,
              "extracted_data": {"total_net_weight": 200.0},
              "update_history_weight": True}]
    merged = hr._merge_human_items(human, [_orig_item()], approved=True,
                                   factory_name=FACTORY)
    assert merged[0].get("update_history_weight") is True


def test_merge_no_flag_no_passthrough():
    human = [{"sku": SKU, "extracted_data": {"total_net_weight": 200.0}}]
    merged = hr._merge_human_items(human, [_orig_item()], approved=True,
                                   factory_name=FACTORY)
    assert "update_history_weight" not in merged[0]


# ---------------------------------------------------------------------------
# 2. writer._upsert_db：标志触发刷新 / 默认不刷新
# ---------------------------------------------------------------------------

def _seed_sku():
    """幂等播种：清掉同厂同 SKU 旧行再插（用例共用隔离库，顺序无关）。"""
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == FACTORY))
        if factory is None:
            factory = Factory(factory_name=FACTORY)
            session.add(factory)
            session.flush()
        for old in session.scalars(select(FactorySKU).where(
                FactorySKU.factory_id == factory.factory_id,
                FactorySKU.sku_code == SKU)).all():
            session.delete(old)
        session.flush()
        session.add(FactorySKU(factory_id=factory.factory_id, sku_code=SKU,
                               name_cn="基准品",
                               unit_net_weight=16.8, unit_gross_weight=18.0))
        session.commit()


def _db_weights():
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == FACTORY))
        r = session.scalar(select(FactorySKU).where(
            FactorySKU.factory_id == factory.factory_id,
            FactorySKU.sku_code == SKU))
        return float(r.unit_net_weight), float(r.unit_gross_weight)


def _state(item):
    return {"batch_id": "TID-WEIGHT",
            "current_factory_data": {"factory_name": FACTORY,
                                     "calculated_items": [item]}}


def _calc_item(**over):
    item = {
        "sku": SKU,
        "calculation": {"calculated_unit_net": 20.0,
                        "calculated_unit_gross": 22.0},
        "is_human_edited": False,   # 关键：数值未经人工编辑
    }
    item.update(over)
    return item


def test_upsert_flagged_item_refreshes_weight():
    _seed_sku()
    inserted, updated = writer_mod._upsert_db(
        _state(_calc_item(update_history_weight=True)))
    assert (inserted, updated) == (0, 1)
    assert _db_weights() == (20.0, 22.0)


def test_upsert_unflagged_unedited_keeps_history():
    _seed_sku()
    inserted, updated = writer_mod._upsert_db(_state(_calc_item()))
    assert (inserted, updated) == (0, 0)
    assert _db_weights() == (16.8, 18.0)  # 历史基准不动（防污染默认）


# ---------------------------------------------------------------------------
# 3. 审计留痕：「历史单重」扁平条目
# ---------------------------------------------------------------------------

def test_prepare_audit_history_weight_entry(monkeypatch):
    monkeypatch.setattr(service, "get_review_payload", lambda tid: {
        "factory_name": FACTORY,
        "items": [_orig_item()],
    })
    resume = {"approved": True, "items": [
        {"sku": SKU,
         "extracted_data": {"total_net_weight": 200.0},
         "calculation": {"calculated_unit_net": 20.0},
         "update_history_weight": True},
    ]}
    prepared = service._prepare_audit("TID-WEIGHT", resume)
    assert prepared is not None
    hw = [c for c in prepared["changes"] if c["field"] == "历史单重"]
    assert len(hw) == 1
    assert hw[0]["sku"] == SKU
    assert hw[0]["old"] == 16.8
    assert hw[0]["new"] == 20.0
    assert set(hw[0]) == {"sku", "field", "old", "new"}  # 扁平结构契约
    assert prepared["edited_count"] >= 1


def test_prepare_audit_no_flag_no_entry(monkeypatch):
    monkeypatch.setattr(service, "get_review_payload", lambda tid: {
        "factory_name": FACTORY,
        "items": [_orig_item()],
    })
    resume = {"approved": True, "items": [
        {"sku": SKU, "extracted_data": {"total_net_weight": 200.0}},
    ]}
    prepared = service._prepare_audit("TID-WEIGHT", resume)
    assert [c for c in prepared["changes"] if c["field"] == "历史单重"] == []


def test_reopen_audit_history_weight_entry():
    """reopen 路径（_prepare_audit_changes_from_items）同口径留痕。"""
    old_items = [_orig_item()]
    new_items = [{**_orig_item(),
                  "calculation": {"calculated_unit_net": 20.0},
                  "update_history_weight": True}]
    changes = service._prepare_audit_changes_from_items(new_items, old_items)
    hw = [c for c in changes if c["field"] == "历史单重"]
    assert len(hw) == 1
    assert hw[0]["old"] == 16.8 and hw[0]["new"] == 20.0
