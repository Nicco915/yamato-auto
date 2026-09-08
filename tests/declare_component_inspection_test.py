# -*- coding: utf-8 -*-
"""品名组组件行商检与映射行 inspection_required 解耦测试（2026-09-08 决策）。

背景：报关单 I 列商检统一取源行 SKU 级 any(inspection)。普通行早已如此
（wt-split-sku 统一），此前品名组组件行（set_split/box_share 的 members）
仍保留映射 lookup 的 inspection_required——组件品名是拆分后的虚拟品名，
其映射行商检字段此后只用于审核页失焦带出，不进报关链路。本测试把组员
映射行的 inspection_required 故意设成与源行 SKU 级口径**相反**，证明
组件行商检完全跟随源行、与映射行解耦；同时回归 unit_code 仍正常从组员
映射行带出。

纯函数层测试（build_mapping_index + aggregate_ticket），不跑图、不起
服务、不碰 DB。

隔离（血泪红线 2026-08-11，与 tests/declare_unit_code_warning_test.py
同模式）：先 import 全部 app 模块，再调 isolate_to_tmp。

用法（在 app/ 目录下）：
  python3 tests/declare_component_inspection_test.py
  PYTHONPATH=. python3 -m pytest tests/declare_component_inspection_test.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"                      # 提取走 mock，不调 LLM
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from app.declare.aggregator import aggregate_ticket  # noqa: E402
from app.declare.mapping import build_mapping_index  # noqa: E402
from app.split.schemas import RawItem  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_declare_component_inspection_test_")

# ---- 测试数据 ----

SOURCE_NAME = "床品三件套"
MEMBER_A = "床单"
MEMBER_B = "被套"


def _row(name_cn: str, inspection: bool, sku: str, kanri: str = "K1") -> RawItem:
    """构造一行最小 RawItem（inspection/SKU 可变，其余从简）。"""
    return RawItem(
        kanri_no=kanri,
        port="東京港",
        container_type="40HQ",
        maker="测试工厂",
        sku=sku,
        name_cn=name_cn,
        net_weight=1.0,
        gross_weight=1.2,
        pcs=10,
        qty_pieces=100,
        amount=500.0,
        currency="USD",
        inspection=inspection,
    )


def _mapping(name_cn: str, inspection_required: bool, unit_code: str) -> dict:
    """构造一条产品映射记录（dict 形式，_get 兼容）。"""
    return {
        "product_name_cn": name_cn,
        "sku_code": "",
        "factory_id": None,
        "inspection_required": inspection_required,
        "unit_code": unit_code,
    }


def _set_split_group() -> dict:
    """两成员 set_split 组：源品名 SOURCE_NAME → 床单/被套。"""
    return {
        "name": "床品三件套组",
        "group_type": "set_split",
        "source_name_cn": SOURCE_NAME,
        "members": [
            {"product_name_cn": MEMBER_A, "display_order": 1,
             "split_price": 3.0, "split_net_weight": 0.6},
            {"product_name_cn": MEMBER_B, "display_order": 2,
             "split_price": 2.0, "split_net_weight": 0.4},
        ],
    }


def _box_share_group() -> dict:
    """两成员 box_share 组：源品名 SOURCE_NAME → 床单/被套。"""
    return {
        "name": "床品三件套组",
        "group_type": "box_share",
        "source_name_cn": SOURCE_NAME,
        "members": [
            {"product_name_cn": MEMBER_A, "display_order": 1},
            {"product_name_cn": MEMBER_B, "display_order": 2},
        ],
    }


# ---- 用例 ----

def test_set_split_component_inherits_source_inspection_true():
    """源行任一 SKU inspection=True、组员映射行 inspection=False
    → 组件行 inspection 全 True（与映射行解耦）。"""
    rows = [
        _row(SOURCE_NAME, inspection=False, sku="4900000000001"),
        _row(SOURCE_NAME, inspection=True, sku="4900000000002"),
    ]
    index = build_mapping_index([
        _mapping(SOURCE_NAME, inspection_required=False, unit_code="001"),
        _mapping(MEMBER_A, inspection_required=False, unit_code="011"),
        _mapping(MEMBER_B, inspection_required=False, unit_code="012"),
    ])
    res = aggregate_ticket(rows, groups=[_set_split_group()], mapping_index=index)
    comp = [r for r in res.rows if r.name_cn in (MEMBER_A, MEMBER_B)]
    assert len(comp) == 2, f"应有 2 行组件行: {[(r.name_cn, r.inspection) for r in res.rows]}"
    assert all(r.inspection is True for r in comp), (
        f"源行 SKU 级 any=True，组件行应全 True: "
        f"{[(r.name_cn, r.inspection) for r in comp]}"
    )


def test_set_split_component_inherits_source_inspection_false():
    """源行 SKU 全 False、组员映射行 inspection=True
    → 组件行 inspection 全 False（与映射行解耦）。"""
    rows = [
        _row(SOURCE_NAME, inspection=False, sku="4900000000001"),
        _row(SOURCE_NAME, inspection=False, sku="4900000000002"),
    ]
    index = build_mapping_index([
        _mapping(SOURCE_NAME, inspection_required=True, unit_code="001"),
        _mapping(MEMBER_A, inspection_required=True, unit_code="011"),
        _mapping(MEMBER_B, inspection_required=True, unit_code="012"),
    ])
    res = aggregate_ticket(rows, groups=[_set_split_group()], mapping_index=index)
    comp = [r for r in res.rows if r.name_cn in (MEMBER_A, MEMBER_B)]
    assert len(comp) == 2, f"应有 2 行组件行: {[(r.name_cn, r.inspection) for r in res.rows]}"
    assert all(r.inspection is False for r in comp), (
        f"源行 SKU 级 any=False，组件行应全 False: "
        f"{[(r.name_cn, r.inspection) for r in comp]}"
    )


def test_set_split_unit_code_still_from_member_mapping():
    """unit_code 仍正常从组员映射行带出（回归不退化）。"""
    rows = [_row(SOURCE_NAME, inspection=True, sku="4900000000001")]
    index = build_mapping_index([
        _mapping(MEMBER_A, inspection_required=False, unit_code="011"),
        _mapping(MEMBER_B, inspection_required=False, unit_code="012"),
    ])
    res = aggregate_ticket(rows, groups=[_set_split_group()], mapping_index=index)
    by_name = {r.name_cn: r for r in res.rows}
    assert by_name[MEMBER_A].unit_code == "011", (
        f"{MEMBER_A} unit_code 应为 011: {by_name[MEMBER_A].unit_code!r}"
    )
    assert by_name[MEMBER_B].unit_code == "012", (
        f"{MEMBER_B} unit_code 应为 012: {by_name[MEMBER_B].unit_code!r}"
    )


def test_box_share_component_inherits_source_inspection():
    """box_share 分支同口径：映射行相反值不影响组件行商检。"""
    rows = [_row(SOURCE_NAME, inspection=True, sku="4900000000001")]
    index = build_mapping_index([
        _mapping(MEMBER_A, inspection_required=False, unit_code="011"),
        _mapping(MEMBER_B, inspection_required=False, unit_code="012"),
    ])
    res = aggregate_ticket(rows, groups=[_box_share_group()], mapping_index=index)
    comp = [r for r in res.rows if r.name_cn in (MEMBER_A, MEMBER_B)]
    assert len(comp) == 2
    assert all(r.inspection is True for r in comp), (
        f"box_share 组件行应继承源行 SKU 级 any=True: "
        f"{[(r.name_cn, r.inspection) for r in comp]}"
    )

    rows2 = [_row(SOURCE_NAME, inspection=False, sku="4900000000001")]
    index2 = build_mapping_index([
        _mapping(MEMBER_A, inspection_required=True, unit_code="011"),
        _mapping(MEMBER_B, inspection_required=True, unit_code="012"),
    ])
    res2 = aggregate_ticket(rows2, groups=[_box_share_group()], mapping_index=index2)
    comp2 = [r for r in res2.rows if r.name_cn in (MEMBER_A, MEMBER_B)]
    assert all(r.inspection is False for r in comp2), (
        f"box_share 组件行应继承源行 SKU 级 any=False: "
        f"{[(r.name_cn, r.inspection) for r in comp2]}"
    )


# ---- 脚本直跑入口 ----

def main():
    test_set_split_component_inherits_source_inspection_true()
    test_set_split_component_inherits_source_inspection_false()
    test_set_split_unit_code_still_from_member_mapping()
    test_box_share_component_inherits_source_inspection()
    print("\ndeclare_component_inspection_test: PASS")


if __name__ == "__main__":
    main()
