# -*- coding: utf-8 -*-
"""rows_for_ticket 的 SKU 级商检维度展开口径单测（纯内存构造，不碰 DB/文件）。

覆盖：
- 商检半票只含该厂 inspection=True 的行；
- 不商检合并票含排除厂的 inspection=False 行 + 非排除厂全部行；
- 半票 + 合并票互补覆盖全柜（无遗漏无重复）；
- 旧结构 TicketItem（无 inspection_filter）展开行为与改前一致；
- schema validator 非法组合报错。
"""

from __future__ import annotations

import pytest

from app.declare.aggregator import rows_for_ticket
from app.split.schemas import RawItem, Ticket, TicketItem


def _row(kanri: str, maker: str, sku: str, inspection: bool = False) -> RawItem:
    return RawItem(
        kanri_no=kanri,
        port="東京港",
        container_type="40HQ",
        maker=maker,
        sku=sku,
        net_weight=1.0,
        gross_weight=1.0,
        pcs=1,
        inspection=inspection,
    )


def _ticket(items: list[TicketItem]) -> Ticket:
    return Ticket(
        ticket_no="東京港-01",
        port="東京港",
        container_type="40HQ",
        items=items,
    )


# 一柜三厂：A 厂 2 行（1 商检 1 不商检）、B 厂 2 行（全商检）、C 厂 1 行（不商检）
ITEMS = [
    _row("K001", "A厂", "SKU-A1", inspection=True),
    _row("K001", "A厂", "SKU-A2", inspection=False),
    _row("K001", "B厂", "SKU-B1", inspection=True),
    _row("K001", "B厂", "SKU-B2", inspection=True),
    _row("K001", "C厂", "SKU-C1", inspection=False),
]


class TestInspectionHalfTicket:
    """商检半票：inspection_filter=True + factory_filter。"""

    def test_only_factory_inspection_rows(self):
        t = _ticket([
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="A厂", inspection_filter=True,
            ),
        ])
        rows = rows_for_ticket(t, ITEMS, {})
        assert [r.sku for r in rows] == ["SKU-A1"]

    def test_factory_all_inspection(self):
        t = _ticket([
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="B厂", inspection_filter=True,
            ),
        ])
        rows = rows_for_ticket(t, ITEMS, {})
        assert [r.sku for r in rows] == ["SKU-B1", "SKU-B2"]


class TestNonInspectionMergedTicket:
    """不商检合并票：inspection_filter=False + factory_exclude。"""

    def test_contains_non_excluded_all_and_excluded_non_inspection(self):
        t = _ticket([
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_exclude=["A厂", "B厂"], inspection_filter=False,
            ),
        ])
        rows = rows_for_ticket(t, ITEMS, {})
        # A 厂 inspection=False 行 + C 厂全部行；B 厂全商检 → 不贡献任何行
        assert [r.sku for r in rows] == ["SKU-A2", "SKU-C1"]


class TestComplementaryCoverage:
    """半票 + 合并票互补、无交集、合起来恰好覆盖全柜行。"""

    def test_full_coverage_no_overlap(self):
        t = _ticket([
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="A厂", inspection_filter=True,
            ),
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="B厂", inspection_filter=True,
            ),
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_exclude=["A厂", "B厂"], inspection_filter=False,
            ),
        ])
        rows = rows_for_ticket(t, ITEMS, {})
        assert sorted(r.sku for r in rows) == sorted(r.sku for r in ITEMS)
        assert len(rows) == len(ITEMS)  # 无重复


class TestLegacyBackwardCompat:
    """旧结构 TicketItem（inspection_filter=None，如老 Declaration 记录 JSON
    无此键）展开行为必须与改前完全一致。"""

    def test_legacy_factory_filter_ignores_inspection_flag(self):
        """旧商检半票：factory_filter=F 取该厂全部行，不看 inspection。"""
        ti = TicketItem(kanri_no="K001", is_partial=True, factory_filter="A厂")
        assert ti.inspection_filter is None  # 缺省即旧语义
        rows = rows_for_ticket(_ticket([ti]), ITEMS, {})
        assert [r.sku for r in rows] == ["SKU-A1", "SKU-A2"]

    def test_legacy_factory_exclude_ignores_inspection_flag(self):
        """旧非商检剩余票：排除集外全部行，不看 inspection。"""
        ti = TicketItem(
            kanri_no="K001", is_partial=True, factory_exclude=["A厂"],
        )
        rows = rows_for_ticket(_ticket([ti]), ITEMS, {})
        assert [r.sku for r in rows] == ["SKU-B1", "SKU-B2", "SKU-C1"]

    def test_full_container_unaffected(self):
        """整柜票不受 inspection_filter 影响（旧语义下本就为 None）。"""
        ti = TicketItem(kanri_no="K001")
        rows = rows_for_ticket(_ticket([ti]), ITEMS, {})
        assert len(rows) == len(ITEMS)

    def test_raw_item_default_not_inspection(self):
        """RawItem.inspection 缺省 False：旧数据无此键即不商检。"""
        r = RawItem(
            kanri_no="K", port="P", container_type="C", maker="M", sku="S",
            net_weight=None, gross_weight=None, pcs=None,
        )
        assert r.inspection is False


class TestSchemaValidator:
    """inspection_filter 与 factory_filter/factory_exclude 的非法组合报错。"""

    def test_true_without_factory_filter(self):
        with pytest.raises(ValueError, match="必须搭配 factory_filter"):
            TicketItem(kanri_no="K001", is_partial=True, inspection_filter=True)

    def test_true_with_factory_exclude(self):
        with pytest.raises(ValueError):
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_exclude=["A厂"], inspection_filter=True,
            )

    def test_false_without_factory_exclude(self):
        with pytest.raises(ValueError, match="必须搭配 factory_exclude"):
            TicketItem(kanri_no="K001", is_partial=True, inspection_filter=False)

    def test_false_with_factory_filter(self):
        with pytest.raises(ValueError):
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="A厂", inspection_filter=False,
            )

    def test_mutex_still_enforced(self):
        """旧互斥规则维持：filter 与 exclude 不得同时设置。"""
        with pytest.raises(ValueError, match="互斥"):
            TicketItem(
                kanri_no="K001", is_partial=True,
                factory_filter="A厂", factory_exclude=["B厂"],
            )

    def test_legal_combinations(self):
        TicketItem(kanri_no="K001", is_partial=True,
                   factory_filter="A厂", inspection_filter=True)
        TicketItem(kanri_no="K001", is_partial=True,
                   factory_exclude=["A厂"], inspection_filter=False)
        TicketItem(kanri_no="K001", is_partial=True, factory_filter="A厂")
        TicketItem(kanri_no="K001", is_partial=True, factory_exclude=["A厂"])
        TicketItem(kanri_no="K001")
