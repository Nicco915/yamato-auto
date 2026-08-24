# -*- coding: utf-8 -*-
"""多商检柜「非商检剩余票」回归测试（修非商检行静默丢失漏洞）。

背景：规则 4 把含 ≥2 个商检工厂的柜拆成每个商检工厂一张半票，
若柜内还混装非商检工厂的行，旧实现只生成商检半票，非商检行
不进任何票（或被并入行数较多的商检方半票），报关单静默丢失。
修复：engine 追加一张 factory_exclude=商检工厂集 的剩余票，
aggregator 按排除集展开。

纯函数单测（engine.propose / aggregator.rows_for_ticket），不碰 DB。

覆盖：
1. 柜含 2 商检 + 1 非商检 → 3 张票：2 商检半票 + 1 剩余票
   （factory_exclude 正确、带 non_sj_remainder 警告、票号连续）；
2. 柜含 3 商检 + 2 非商检 → 4 张票，剩余票含全部非商检行；
3. 柜只含多商检无非商检 → 不生成剩余票（回归）；
4. 单商检 / 零商检柜 → 整柜逻辑不变（回归）；
5. aggregator 覆盖完整性：剩余票行 = 柜内非商检行，与商检半票行
   无交集，合起来 = 全柜行；
6. TicketItem schema：factory_filter 与 factory_exclude 互斥断言。

用法（在 app/ 目录下）：
  python3 tests/split_non_sj_remainder_test.py
  PYTHONPATH=. python3 -m pytest tests/split_non_sj_remainder_test.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

import pytest  # noqa: E402

from app.declare.aggregator import rows_for_ticket  # noqa: E402
from app.split.engine import propose  # noqa: E402
from app.split.schemas import RawItem, TicketItem  # noqa: E402

# ---- 测试数据 ----

SJ_A = "商检厂A"
SJ_B = "商检厂B"
SJ_C = "商检厂C"
PLAIN_X = "普通厂X"
PLAIN_Y = "普通厂Y"

SJ_MAP = {SJ_A: True, SJ_B: True, SJ_C: True, PLAIN_X: False, PLAIN_Y: False}


def _row(kanri: str, maker: str, sku: str) -> RawItem:
    """构造一行 RawItem（同港同箱型，重量/箱数从简）。"""
    return RawItem(
        kanri_no=kanri,
        port="東京港",
        container_type="40HQ",
        maker=maker,
        sku=sku,
        net_weight=1.0,
        gross_weight=1.2,
        pcs=10,
    )


def _tickets_of(proposal, kanri: str):
    """取出包含指定柜的全部票。"""
    out = []
    for pg in proposal.ports:
        for t in pg.groups:
            if any(it.kanri_no == kanri for it in t.items):
                out.append(t)
    return out


def _remainder_items(proposal, kanri: str):
    """取出指定柜的非商检剩余票 TicketItem（无则 None）。"""
    for t in _tickets_of(proposal, kanri):
        for it in t.items:
            if it.kanri_no == kanri and it.factory_exclude:
                return t, it
    return None


# ---- engine：票生成 ----

def test_dual_sj_plus_one_plain_yields_remainder_ticket():
    """2 商检 + 1 非商检 → 3 张票：2 商检半票 + 1 非商检剩余票。"""
    items = [
        _row("K1", SJ_A, "s1"),
        _row("K1", SJ_B, "s2"),
        _row("K1", PLAIN_X, "s3"),
    ]
    proposal = propose(items, SJ_MAP)

    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 3, f"预期 3 张票，实际 {len(tickets)}"

    # 2 张商检半票：factory_filter 分别为两个商检工厂
    filters = sorted(
        it.factory_filter
        for t in tickets for it in t.items
        if it.factory_filter
    )
    assert filters == sorted([SJ_A, SJ_B])

    # 1 张剩余票：factory_exclude = 全部商检工厂，无商检工厂、无整柜
    found = _remainder_items(proposal, "K1")
    assert found is not None, "未生成非商检剩余票"
    ticket, item = found
    assert item.is_partial is True
    assert item.factory_filter is None
    assert item.factory_exclude == sorted([SJ_A, SJ_B])
    assert ticket.sj_factories == []
    assert ticket.full_containers == 0

    # 剩余票带 non_sj_remainder 警告
    rules = [w.rule for w in ticket.warnings]
    assert "non_sj_remainder" in rules, f"剩余票缺 non_sj_remainder 警告: {rules}"

    # 票号港口内连续 -01/-02/-03
    nos = sorted(t.ticket_no for t in tickets)
    assert nos == ["東京港-01", "東京港-02", "東京港-03"]


def test_triple_sj_plus_two_plain_yields_four_tickets():
    """3 商检 + 2 非商检 → 4 张票，剩余票排除全部 3 个商检工厂。"""
    items = [
        _row("K1", SJ_A, "s1"),
        _row("K1", SJ_B, "s2"),
        _row("K1", SJ_C, "s3"),
        _row("K1", PLAIN_X, "s4"),
        _row("K1", PLAIN_Y, "s5"),
    ]
    proposal = propose(items, SJ_MAP)

    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 4, f"预期 4 张票，实际 {len(tickets)}"

    found = _remainder_items(proposal, "K1")
    assert found is not None
    _, item = found
    assert item.factory_exclude == sorted([SJ_A, SJ_B, SJ_C])

    nos = sorted(t.ticket_no for t in tickets)
    assert nos == [f"東京港-0{i}" for i in range(1, 5)]


def test_multi_sj_without_plain_no_remainder():
    """只含多商检、无非商检的柜 → 不生成剩余票（回归）。"""
    items = [
        _row("K1", SJ_A, "s1"),
        _row("K1", SJ_B, "s2"),
    ]
    proposal = propose(items, SJ_MAP)

    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 2, f"预期 2 张票，实际 {len(tickets)}"
    assert _remainder_items(proposal, "K1") is None
    assert all(
        not any(w.rule == "non_sj_remainder" for w in t.warnings)
        for t in tickets
    )


def test_single_sj_container_stays_whole():
    """单商检柜 → 整柜票，不拆半票（回归）。"""
    items = [
        _row("K1", SJ_A, "s1"),
        _row("K1", PLAIN_X, "s2"),
    ]
    proposal = propose(items, SJ_MAP)

    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 1
    t = tickets[0]
    assert len(t.items) == 1
    assert t.items[0].is_partial is False
    assert t.items[0].factory_filter is None
    assert t.items[0].factory_exclude is None
    assert t.full_containers == 1
    assert t.sj_factories == [SJ_A]


def test_zero_sj_container_stays_whole():
    """零商检柜 → 整柜票（回归）。"""
    items = [
        _row("K1", PLAIN_X, "s1"),
        _row("K1", PLAIN_Y, "s2"),
    ]
    proposal = propose(items, SJ_MAP)

    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 1
    assert tickets[0].items[0].is_partial is False
    assert tickets[0].sj_factories == []


# ---- aggregator：行归属 ----

def test_aggregator_remainder_rows_and_full_coverage():
    """剩余票展开 = 柜内非商检行；与商检半票无交集、合起来 = 全柜行。"""
    items = [
        _row("K1", SJ_A, "s1"),
        _row("K1", SJ_A, "s2"),
        _row("K1", SJ_B, "s3"),
        _row("K1", PLAIN_X, "s4"),
        _row("K1", PLAIN_Y, "s5"),
    ]
    proposal = propose(items, SJ_MAP)
    tickets = _tickets_of(proposal, "K1")
    assert len(tickets) == 3

    rows_by_ticket = {
        t.ticket_no: rows_for_ticket(t, items, SJ_MAP) for t in tickets
    }

    # 剩余票行 = 全部非商检行
    found = _remainder_items(proposal, "K1")
    ticket, _ = found
    rem_rows = rows_by_ticket[ticket.ticket_no]
    assert {r.maker for r in rem_rows} == {PLAIN_X, PLAIN_Y}
    assert len(rem_rows) == 2

    # 商检半票行 = 各自工厂行
    for t in tickets:
        f = t.items[0].factory_filter
        if f:
            assert {r.maker for r in rows_by_ticket[t.ticket_no]} == {f}

    # 覆盖完整性：无交集、合起来 = 全柜行（按对象 id 计）
    seen: dict[int, str] = {}
    for no, rows in rows_by_ticket.items():
        for r in rows:
            assert id(r) not in seen, f"行被 {seen[id(r)]} 与 {no} 重复覆盖"
            seen[id(r)] = no
    assert len(seen) == len(items), (
        f"覆盖行数 {len(seen)} != 全柜行数 {len(items)}"
    )


# ---- schema：互斥断言 ----

def test_ticket_item_filter_exclude_mutex():
    """factory_filter 与 factory_exclude 同时设置 → ValidationError。"""
    with pytest.raises(ValueError, match="互斥"):
        TicketItem(
            kanri_no="K1",
            factory_filter=SJ_A,
            factory_exclude=[SJ_B],
            is_partial=True,
        )


def test_ticket_item_factory_exclude_backward_compat():
    """旧落库记录无 factory_exclude 键 → 解析为 None。"""
    item = TicketItem(**{"kanri_no": "K1", "factory_filter": None,
                         "is_partial": False})
    assert item.factory_exclude is None


# ---- 脚本直跑入口 ----

def main():
    test_dual_sj_plus_one_plain_yields_remainder_ticket()
    test_triple_sj_plus_two_plain_yields_four_tickets()
    test_multi_sj_without_plain_no_remainder()
    test_single_sj_container_stays_whole()
    test_zero_sj_container_stays_whole()
    test_aggregator_remainder_rows_and_full_coverage()
    test_ticket_item_filter_exclude_mutex()
    test_ticket_item_factory_exclude_backward_compat()
    print("\nsplit_non_sj_remainder_test: PASS")


if __name__ == "__main__":
    main()
