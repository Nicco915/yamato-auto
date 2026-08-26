# -*- coding: utf-8 -*-
"""一次性合并脚本 scripts/merge_duplicate_mappings.py 测试。

覆盖：
- plan_merges：同（品名+供应商）多行且字段一致 → 进可合并计划（保 id 最小行、
  SKU 收拢去重保序）；字段冲突 → 进冲突清单不进计划；单行组不进任何清单
- apply_plans：SKU 并入保留行子表、多余行删除（子表关联级联清除）、
  旧列 sku_code 同步为列表第一个、is_incomplete 按 hs_code 重推导
- 幂等：合并后再扫一次无计划
- 供应商不同 → 不合并（分组键含供应商）

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/merge_duplicate_mappings_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))
sys.path.insert(0, str(APP_ROOT / "scripts"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.db.models import ProductMapping, ProductMappingSku  # noqa: E402
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_merge_dup_mappings_test_")

import merge_duplicate_mappings as mdm  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_tables():
    """每用例前清空映射两表（同进程共享临时库，防用例间串扰）。"""
    with get_session() as s:
        s.query(ProductMappingSku).delete()
        s.query(ProductMapping).delete()
        s.commit()
    yield


def _add_mapping(name, supplier=None, hs="9617009000", unit="007",
                 name_en="BOARD", inspection=False, skus=()):
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=name, supplier_name=supplier, hs_code=hs,
            unit_code=unit, name_en=name_en, inspection_required=inspection,
            is_incomplete=not bool(hs),
            sku_code=skus[0] if skus else None,
        )
        s.add(m)
        s.flush()
        for code in skus:
            s.add(ProductMappingSku(mapping_id=m.id, sku_code=code))
        s.commit()
        return m.id


def _links(mapping_id):
    with get_session() as s:
        return sorted(
            l.sku_code for l in
            s.query(ProductMappingSku)
            .filter(ProductMappingSku.mapping_id == mapping_id).all())


def _mapping(mapping_id):
    with get_session() as s:
        return s.get(ProductMapping, mapping_id)


def test_plan_groups_identical_rows():
    """字段一致的同键三行 → 一个合并计划，保 id 最小，SKU 收拢。"""
    ids = [_add_mapping("写字板", "供应商A", skus=[sku])
           for sku in ("111", "222", "333")]
    plans, conflicts = mdm.plan_merges()
    assert conflicts == []
    assert len(plans) == 1
    p = plans[0]
    assert p["keep_id"] == ids[0]
    assert sorted(p["drop_ids"]) == sorted(ids[1:])
    assert p["skus"] == ["111", "222", "333"]


def test_plan_conflict_fields_not_merged():
    """单位代码不一致 → 进冲突清单（带双方值），不产生合并计划。"""
    _add_mapping("坐垫", "供应商B", unit="007", skus=["444"])
    _add_mapping("坐垫", "供应商B", unit="008", skus=["555"])
    plans, conflicts = mdm.plan_merges()
    assert plans == []
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["key"] == ("坐垫", "供应商B")
    assert c["fields"] == ["unit_code"]
    assert {r["unit_code"] for r in c["rows"]} == {"007", "008"}


def test_plan_different_supplier_not_grouped():
    """品名相同但供应商不同 → 不合并。"""
    _add_mapping("剪刀", "供应商X", skus=["666"])
    _add_mapping("剪刀", "供应商Y", skus=["777"])
    plans, conflicts = mdm.plan_merges()
    assert plans == [] and conflicts == []


def test_apply_merges_rows_and_links():
    """执行合并：SKU 收拢到保留行、多余行级联删除、旧列同步、幂等。"""
    keep = _add_mapping("打孔器", "供应商C", skus=["888"])
    drop = _add_mapping("打孔器", "供应商C", skus=["999"])
    plans, _ = mdm.plan_merges()
    assert len(plans) == 1

    result = mdm.apply_plans(plans)
    assert result == {"merged_groups": 1, "moved_skus": 1}

    assert _links(keep) == ["888", "999"]
    assert _mapping(drop) is None
    assert _links(drop) == []            # 级联清除
    m = _mapping(keep)
    assert m.sku_code == "888"           # 旧列=列表第一个
    assert m.is_incomplete is False      # hs_code 有值

    # 幂等：再扫无计划
    plans2, _ = mdm.plan_merges()
    assert [p for p in plans2 if p["key"] == ("打孔器", "供应商C")] == []


def test_apply_merged_row_incomplete_when_hs_blank():
    """合并组 hs_code 为空 → 保留行 is_incomplete=True。"""
    keep = _add_mapping("无税号品", "供应商D", hs="", skus=["aaa"])
    _add_mapping("无税号品", "供应商D", hs="", skus=["bbb"])
    plans, _ = mdm.plan_merges()
    mdm.apply_plans(plans)
    assert _mapping(keep).is_incomplete is True
