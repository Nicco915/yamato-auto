# -*- coding: utf-8 -*-
"""SKU 级商检口径统一下的 validate mixed_sj 与 aggregate I 列单测。

纯内存构造，不碰 DB/文件/.env。

覆盖：
- validate_confirmed_proposal 的 mixed_sj 行级口径：
  · 不商检合并票里「商检工厂的 inspection=False 行」不触发 mixed_sj；
  · 票内 >1 家工厂有 inspection=True 行才警告（sj_map 为空也照判，
    证明判定以行级为准、不依赖 sj_map）；
- aggregate_ticket 普通明细行 inspection = 贡献源行 any(inspection)：
  · 源行任一 True → True（即使映射 inspection_required=False）；
  · 源行全 False → False（即使映射 inspection_required=True）；
  · 未命中映射时 any() 仍生效（warning 原样保留）；
  · 品名组组件行保持按组件品名映射 lookup 的旧口径不动。
"""

from __future__ import annotations

from app.declare.aggregator import aggregate_ticket
from app.declare.mapping import build_mapping_index
from app.split.schemas import RawItem
from app.split.validate import validate_confirmed_proposal


def _item(kanri: str, maker: str, sku: str, inspection: bool,
          name_cn: str = "") -> dict:
    return {
        "kanri_no": kanri,
        "port": "東京港",
        "container_type": "40HQ",
        "maker": maker,
        "sku": sku,
        "net_weight": 1.0,
        "gross_weight": 1.0,
        "pcs": 1,
        "name_cn": name_cn,
        "inspection": inspection,
    }


def _proposal(*ticket_items_groups: list[dict]) -> dict:
    """每组 ticket items 成一张票，全部挂在東京港下。"""
    return {
        "ports": [
            {
                "port": "東京港",
                "groups": [
                    {
                        "ticket_no": f"東京港-{i:02d}",
                        "port": "東京港",
                        "container_type": "40HQ",
                        "items": items,
                    }
                    for i, items in enumerate(ticket_items_groups, start=1)
                ],
            }
        ]
    }


# 一柜三厂：A 厂 1 商检 1 不商检、B 厂 1 行不商检、C 厂 1 行商检
RAW_ITEMS = [
    _item("K001", "A厂", "SKU-A1", inspection=True),
    _item("K001", "A厂", "SKU-A2", inspection=False),
    _item("K001", "B厂", "SKU-B1", inspection=False),
    _item("K001", "C厂", "SKU-C1", inspection=True),
]


class TestMixedSjRowLevel:
    """mixed_sj 软警告的行级口径（只看票内 inspection=True 行的工厂集合）。"""

    def test_merged_non_inspection_ticket_no_mixed_sj(self):
        """不商检合并票含商检工厂的不商检行 + 其他商检工厂的行——
        票内没有任何 inspection=True 行，不触发 mixed_sj。

        旧口径（sj_map 按工厂）下合并票内 A厂/B厂 均为商检工厂会误报；
        新口径不应出现任何警告。
        """
        proposal = _proposal(
            [{"kanri_no": "K001", "is_partial": True,
              "factory_filter": "A厂", "inspection_filter": True}],
            [{"kanri_no": "K001", "is_partial": True,
              "factory_filter": "C厂", "inspection_filter": True}],
            [{"kanri_no": "K001", "is_partial": True,
              "factory_exclude": ["A厂", "C厂"], "inspection_filter": False}],
        )
        # sj_map 宣称三家全是商检工厂：旧口径必误报，新口径以行为准
        sj_map = {"A厂": True, "B厂": True, "C厂": True}
        errors, warnings = validate_confirmed_proposal(
            proposal, RAW_ITEMS, sj_map
        )
        assert errors == []
        assert warnings == []

    def test_two_inspection_factories_warns_without_sj_map(self):
        """整柜票内两家工厂各有 inspection=True 行 → 触发 mixed_sj；
        sj_map 传空也照判，证明判定完全以行级为准。"""
        proposal = _proposal([{"kanri_no": "K001"}])
        errors, warnings = validate_confirmed_proposal(proposal, RAW_ITEMS, {})
        assert errors == []
        assert len(warnings) == 1
        assert "多种商检工厂" in warnings[0]
        assert "A厂" in warnings[0] and "C厂" in warnings[0]

    def test_single_inspection_factory_no_warning(self):
        """票内只有一家工厂有 inspection=True 行 → 不警告。"""
        items = [
            _item("K001", "A厂", "SKU-A1", inspection=True),
            _item("K001", "B厂", "SKU-B1", inspection=False),
        ]
        proposal = _proposal([{"kanri_no": "K001"}])
        errors, warnings = validate_confirmed_proposal(proposal, items, {})
        assert errors == []
        assert warnings == []


# ---------------------------------------------------------------------------
# aggregate_ticket 明细行 I 列口径
# ---------------------------------------------------------------------------

def _row(maker: str, sku: str, name_cn: str, inspection: bool) -> RawItem:
    return RawItem(
        kanri_no="K001",
        port="東京港",
        container_type="40HQ",
        maker=maker,
        sku=sku,
        net_weight=1.0,
        gross_weight=1.0,
        pcs=1,
        name_cn=name_cn,
        qty_pieces=1,
        amount=10.0,
        inspection=inspection,
    )


def _mapping(name: str, inspection_required: bool, unit_code: str = "个"):
    return {
        "product_name_cn": name,
        "factory_id": None,
        "inspection_required": inspection_required,
        "unit_code": unit_code,
    }


class TestDetailRowInspectionAny:
    """普通明细行 inspection = 贡献源行 any(inspection)，不再按品名查映射。"""

    def test_any_true_wins_over_mapping_false(self):
        """同名源行一真一假 → True（映射 inspection_required=False 也压不住）。"""
        rows = [
            _row("A厂", "SKU-1", "陶瓷杯", inspection=True),
            _row("A厂", "SKU-2", "陶瓷杯", inspection=False),
        ]
        idx = build_mapping_index([_mapping("陶瓷杯", False)])
        res = aggregate_ticket(rows, [], idx)
        assert len(res.rows) == 1
        assert res.rows[0].inspection is True
        assert res.rows[0].unit_code == "个"  # 映射仍负责 unit_code

    def test_all_false_overrides_mapping_true(self):
        """源行全 False → False（映射 inspection_required=True 不再生效）。"""
        rows = [
            _row("A厂", "SKU-1", "陶瓷杯", inspection=False),
            _row("B厂", "SKU-2", "陶瓷杯", inspection=False),
        ]
        idx = build_mapping_index([_mapping("陶瓷杯", True)])
        res = aggregate_ticket(rows, [], idx)
        assert res.rows[0].inspection is False

    def test_mapping_miss_any_still_applies(self):
        """未命中映射：warning 原样保留，inspection 仍取源行 any()。"""
        rows = [_row("A厂", "SKU-1", "无映射品", inspection=True)]
        res = aggregate_ticket(rows, [], build_mapping_index([]))
        assert res.rows[0].inspection is True
        assert any("未命中产品映射" in w for w in res.warnings)

    def test_group_member_inherits_source_sku_inspection(self):
        """品名组组件行商检继承源行 SKU 级 any() 口径（2026-09-08 起），
        与组件映射行的 inspection_required 彻底解耦：
        源行 True → 全部组件行 True（即使映射行 False）。"""
        groups = [{
            "name": "两件套组",
            "group_type": "set_split",
            "source_name_cn": "两件套",
            "members": [
                {"product_name_cn": "组件甲", "display_order": 1,
                 "split_price": 5.0, "split_net_weight": None},
                {"product_name_cn": "组件乙", "display_order": 2,
                 "split_price": 5.0, "split_net_weight": None},
            ],
        }]
        idx = build_mapping_index([
            _mapping("组件甲", False),
            _mapping("组件乙", True),
        ])
        rows = [_row("A厂", "SKU-1", "两件套", inspection=True)]
        res = aggregate_ticket(rows, groups, idx)
        by_name = {r.name_cn: r for r in res.rows}
        assert by_name["组件甲"].inspection is True   # 继承源行，不吃映射 False
        assert by_name["组件乙"].inspection is True   # 继承源行，不吃映射 True
