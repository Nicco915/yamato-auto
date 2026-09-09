# -*- coding: utf-8 -*-
"""分票规则引擎单测（pytest）。

真实文件：ContentsOfTheContainer_202624_青島XD_20260708.xlsx
27 柜，按工厂级 sj_map 回填行级 inspection 后 10 个柜含 ≥2 家商检品工厂。

新语义（SKU 级商检）：商检判定以行级 RawItem.inspection 为准；
≥2 家实际含商检品工厂 → N 张商检半票（inspection_filter=True）
+ 柜内存在不商检行时 1 张不商检合并票（inspection_filter=False）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pytest

from app.split.engine import propose
from app.split.loader import load_filled_excel
from app.split.normalize import classify_sj_factories, normalize_maker
from app.split.schemas import RawItem

# ---- Constants ----

_REAL_FILE = Path(
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx"
)

FALLBACK_SJ = ["青島貝来", "Ｃ．正達工芸品"]

# Normalization map: factory long name → short name
NORMALIZE_MAP = {
    "青島貝来国際貿易有限公司": "青島貝来",
    "上海億鑽五金工具有限公司（青島）": "上海億鑽五金工具（青島）",
}


# ---- Helpers ----

def _count_dual_sj_containers(raw_items):
    """Return the set of kanri_no that have >=2 家实际含商检品的工厂。"""
    container_sj: dict[str, set] = defaultdict(set)
    for item in raw_items:
        if item.inspection:
            container_sj[item.kanri_no].add(item.maker)
    return {k for k, sj in container_sj.items() if len(sj) >= 2}


def _expected_partial_count(raw_items, kanri_no: str) -> int:
    """拆分柜的预期半票数：N 张商检半票 + 柜内存在不商检行时 1 张合并票。"""
    rows = [i for i in raw_items if i.kanri_no == kanri_no]
    sj_makers = {i.maker for i in rows if i.inspection}
    has_non_inspection = any(not i.inspection for i in rows)
    return len(sj_makers) + (1 if has_non_inspection else 0)


def _load_marked_raw():
    """加载真实文件并归一化；按工厂级 sj_map 回填行级 inspection。

    真实文件未经上游 SKU 级标注（inspection 全为 False 默认值），
    这里把商检工厂的全部行标为商检，复现旧「双商检柜」场景。
    """
    raw = load_filled_excel(_REAL_FILE)
    for r in raw:
        r.maker = normalize_maker(r.maker, NORMALIZE_MAP)
    sj_map = classify_sj_factories(raw, {}, FALLBACK_SJ)
    for r in raw:
        r.inspection = sj_map.get(r.maker, False)
    return raw


# ---- Fixture ----

@pytest.fixture(scope="module")
def proposal():
    """Load real file, normalize, mark row-level inspection, and run engine."""
    if not _REAL_FILE.exists():
        pytest.skip(f"Real data file not found: {_REAL_FILE}")

    raw = _load_marked_raw()
    # sj_map 形参保留兼容，引擎内部以行级 inspection 为准
    return propose(raw, {})


# ---- Fixture for raw items (to compute expected counts) ----

@pytest.fixture(scope="module")
def raw_items():
    """Load, normalize and mark row-level inspection."""
    if not _REAL_FILE.exists():
        pytest.skip(f"Real data file not found: {_REAL_FILE}")
    return _load_marked_raw()


# ---- Tests ----

class TestInvariants:
    """Business rule invariants that must hold for any valid proposal."""

    def test_no_mixed_sj_in_any_ticket(self, proposal):
        """不变量 2：任意票 sj_factories ≤ 1（至多含一种商检工厂）。"""
        violations = []
        for pg in proposal.ports:
            for ticket in pg.groups:
                if len(ticket.sj_factories) > 1:
                    violations.append(
                        f"{ticket.ticket_no}: sj_factories={ticket.sj_factories}"
                    )
        assert len(violations) == 0, (
            f"发现 {len(violations)} 张票含有多种商检工厂：\n"
            + "\n".join(violations)
        )

    def test_max_3_full_per_ticket(self, proposal):
        """不变量 1：每票整柜数 ≤ 3。"""
        violations = []
        for pg in proposal.ports:
            for ticket in pg.groups:
                if ticket.full_containers > 3:
                    violations.append(
                        f"{ticket.ticket_no}: full_containers={ticket.full_containers}"
                    )
        assert len(violations) == 0, (
            f"发现 {len(violations)} 张票整柜数超过 3：\n"
            + "\n".join(violations)
        )

    def test_dual_sj_container_yields_partial_tickets(self, proposal, raw_items):
        """不变量 3：10 个 ≥2 家商检品工厂的柜各产生 N 张商检半票
        （inspection_filter=True），柜内存在不商检行时再 +1 不商检合并票
        （inspection_filter=False）。"""
        dual_containers = _count_dual_sj_containers(raw_items)
        assert len(dual_containers) == 10, (
            f"预期 10 个 ≥2 家商检品工厂的柜，实际 {len(dual_containers)}: "
            f"{sorted(dual_containers)}"
        )

        # Collect kanri_no → count of partial ticket appearances
        partial_appearances: Counter[str] = Counter()
        for pg in proposal.ports:
            for ticket in pg.groups:
                for item in ticket.items:
                    if item.is_partial:
                        partial_appearances[item.kanri_no] += 1

        for k in dual_containers:
            expected = _expected_partial_count(raw_items, k)
            count = partial_appearances.get(k, 0)
            assert count == expected, (
                f"拆分柜 {k} 预期 {expected} 张半票，实际 {count}"
            )

    def test_partial_tickets_carry_inspection_filter(self, proposal, raw_items):
        """新口径：商检半票 inspection_filter=True + factory_filter；
        不商检合并票 inspection_filter=False + factory_exclude=全部商检厂。"""
        dual_containers = _count_dual_sj_containers(raw_items)

        for pg in proposal.ports:
            for ticket in pg.groups:
                for item in ticket.items:
                    if not item.is_partial or item.kanri_no not in dual_containers:
                        continue
                    rows = [
                        i for i in raw_items if i.kanri_no == item.kanri_no
                    ]
                    sj_makers = sorted({i.maker for i in rows if i.inspection})
                    if item.factory_filter:
                        assert item.inspection_filter is True, (
                            f"{ticket.ticket_no}: 商检半票缺 inspection_filter=True"
                        )
                        assert item.factory_filter in sj_makers
                    else:
                        assert item.inspection_filter is False, (
                            f"{ticket.ticket_no}: 不商检合并票缺 "
                            "inspection_filter=False"
                        )
                        assert item.factory_exclude == sj_makers

    def test_all_containers_covered(self, proposal, raw_items):
        """不变量 5：全部 27 柜无遗漏无重复；拆分柜出现次数 = 商检半票数
        （柜内存在不商检行时 +1 不商检合并票）。"""
        # All unique containers in data
        all_kanri = {item.kanri_no for item in raw_items}
        assert len(all_kanri) == 27, (
            f"预期 27 柜，实际 {len(all_kanri)}"
        )

        dual_containers = _count_dual_sj_containers(raw_items)
        non_dual = all_kanri - dual_containers

        # Collect all TicketItem appearances
        appearances: Counter[str] = Counter()
        for pg in proposal.ports:
            for ticket in pg.groups:
                for item in ticket.items:
                    appearances[item.kanri_no] += 1

        # Check dual-SJ: N 商检半票 +（有不商检行时）1 不商检合并票
        for k in dual_containers:
            expected = _expected_partial_count(raw_items, k)
            assert appearances.get(k, 0) == expected, (
                f"拆分柜 {k} 预期出现 {expected} 次，实际 {appearances.get(k, 0)}"
            )

        # Check non-dual-SJ: each appears exactly 1 time
        for k in non_dual:
            assert appearances.get(k, 0) == 1, (
                f"非拆分柜 {k} 预期出现 1 次，实际 {appearances.get(k, 0)}"
            )

        # No missing containers
        covered = set(appearances.keys())
        missing = all_kanri - covered
        assert len(missing) == 0, f"遗漏柜号：{missing}"

        # No extra containers
        extra = covered - all_kanri
        assert len(extra) == 0, f"多余柜号：{extra}"

    def test_same_port_and_type_per_ticket(self, proposal, raw_items):
        """不变量 4：同票柜同港口同箱型。

        方法：根据 ticket.port 和 ticket.container_type 校验即可——
        engine 构建时已保证同票同港同箱型。
        """
        # Build kanri_no → (port, ctype) lookup
        kanri_info = {}
        for item in raw_items:
            if item.kanri_no not in kanri_info:
                kanri_info[item.kanri_no] = (item.port, item.container_type)

        violations = []
        for pg in proposal.ports:
            for ticket in pg.groups:
                ticket_port = ticket.port
                ticket_ctype = ticket.container_type
                for item in ticket.items:
                    expected = kanri_info.get(item.kanri_no)
                    if expected is None:
                        continue
                    expected_port, expected_ctype = expected
                    if expected_port != ticket_port or expected_ctype != ticket_ctype:
                        violations.append(
                            f"{ticket.ticket_no}: item {item.kanri_no} "
                            f"expected ({expected_port}, {expected_ctype}), "
                            f"got ({ticket_port}, {ticket_ctype})"
                        )
        assert len(violations) == 0, (
            f"发现 {len(violations)} 个跨港口/箱型错误：\n"
            + "\n".join(violations)
        )

    def test_ticket_no_format(self, proposal):
        """不变量 7：票号 port-NN 格式，同港口内从 01 连续递增。"""
        import re
        pattern = re.compile(r"^.+?-\d{2}$")
        violations = []
        port_seqs: dict[str, list[int]] = {}

        for pg in proposal.ports:
            for ticket in pg.groups:
                if not pattern.match(ticket.ticket_no):
                    violations.append(
                        f"{ticket.ticket_no}: 不匹配 port-NN 格式"
                    )
                # Parse sequence number
                parts = ticket.ticket_no.rsplit("-", 1)
                if len(parts) == 2:
                    try:
                        seq = int(parts[1])
                        port_seqs.setdefault(ticket.port, []).append(seq)
                    except ValueError:
                        violations.append(
                            f"{ticket.ticket_no}: 序号非数字"
                        )

        # Check sequential
        for port, seqs in port_seqs.items():
            expected = list(range(1, len(seqs) + 1))
            if seqs != expected:
                violations.append(
                    f"{port}: 序号 {seqs} 不连续，预期 {expected}"
                )

        assert len(violations) == 0, (
            f"票号格式错误 {len(violations)}：\n" + "\n".join(violations)
        )

    def test_partial_tickets_are_single_item(self, proposal):
        """不变量 6：半票票内只有 1 个 item。"""
        violations = []
        for pg in proposal.ports:
            for ticket in pg.groups:
                # A ticket is partial if any item is partial
                is_partial_ticket = any(it.is_partial for it in ticket.items)
                if is_partial_ticket:
                    if len(ticket.items) != 1:
                        violations.append(
                            f"{ticket.ticket_no}: 半票含 {len(ticket.items)} 个 item"
                        )
        assert len(violations) == 0, (
            f"发现 {len(violations)} 张半票 item 数不为 1：\n"
            + "\n".join(violations)
        )


# ---- 新语义合成用例（纯内存构造，不依赖真实文件） ----

def _row(kanri: str, maker: str, sku: str, inspection: bool = False,
         port: str = "東京港") -> RawItem:
    return RawItem(
        kanri_no=kanri,
        port=port,
        container_type="40HQ",
        maker=maker,
        sku=sku,
        net_weight=1.0,
        gross_weight=1.0,
        pcs=1,
        inspection=inspection,
    )


def _all_tickets(proposal):
    return [t for pg in proposal.ports for t in pg.groups]


class TestSkuLevelInspectionSplit:
    """SKU 级商检拆分新口径：以行级 inspection 判定，与工厂名单无关。"""

    def test_two_sj_factories_split_with_remainder(self):
        """2 家含商检品工厂 + 1 家不商检厂 → 2 商检半票 + 1 不商检合并票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "B厂", "SKU-B1", inspection=True),
            _row("K001", "C厂", "SKU-C1", inspection=False),
        ]
        # sj_map 形参保留兼容：即使名单为空，引擎也以行级 inspection 为准
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 3

        half = [t for t in tickets if t.items[0].inspection_filter is True]
        remainder = [t for t in tickets if t.items[0].inspection_filter is False]
        assert len(half) == 2 and len(remainder) == 1

        # 商检半票按厂名排序，不商检合并票排最后
        assert tickets[0].items[0].factory_filter == "A厂"
        assert tickets[1].items[0].factory_filter == "B厂"
        assert tickets[2] is remainder[0]

        for t in half:
            assert t.items[0].is_partial
            assert t.sj_factories == [t.items[0].factory_filter]
        assert remainder[0].items[0].factory_exclude == ["A厂", "B厂"]
        assert remainder[0].items[0].is_partial
        assert remainder[0].sj_factories == []
        # 软警告保留 non_sj_remainder 规则标识
        assert any(w.rule == "non_sj_remainder" for w in remainder[0].warnings)

    def test_sj_factory_all_non_inspection_no_split(self):
        """贝来类工厂全是不商检品 + 另一家含商检品 → 不拆分，整柜合票。"""
        items = [
            _row("K001", "青島貝来", "SKU-1", inspection=False),
            _row("K001", "青島貝来", "SKU-2", inspection=False),
            _row("K001", "正達", "SKU-3", inspection=True),
        ]
        # 即使 sj_map 把两家都标为商检工厂，行级全不商检即不算
        tickets = _all_tickets(
            propose(items, {"青島貝来": True, "正達": True})
        )
        assert len(tickets) == 1
        assert not tickets[0].items[0].is_partial
        assert tickets[0].items[0].inspection_filter is None
        assert tickets[0].full_containers == 1
        assert tickets[0].sj_factories == ["正達"]

    def test_three_sj_factories_split(self):
        """3 家含商检品工厂同柜 → 3 商检半票 + 1 不商检合并票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "B厂", "SKU-B1", inspection=True),
            _row("K001", "C厂", "SKU-C1", inspection=True),
            _row("K001", "D厂", "SKU-D1", inspection=False),
        ]
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 4
        assert [t.items[0].factory_filter for t in tickets[:3]] == [
            "A厂", "B厂", "C厂",
        ]
        assert all(t.items[0].inspection_filter is True for t in tickets[:3])
        merged = tickets[3]
        assert merged.items[0].inspection_filter is False
        assert merged.items[0].factory_exclude == ["A厂", "B厂", "C厂"]

    def test_all_inspection_rows_no_remainder(self):
        """双商检柜但两厂全部行都商检、且无其他厂 → 无合并票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "A厂", "SKU-A2", inspection=True),
            _row("K001", "B厂", "SKU-B1", inspection=True),
        ]
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 2
        assert all(t.items[0].inspection_filter is True for t in tickets)
        assert all(t.items[0].is_partial for t in tickets)

    def test_sj_factory_mixed_rows_yield_remainder(self):
        """商检厂混有不商检行（柜内无其他厂）→ 仍须生成不商检合并票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "A厂", "SKU-A2", inspection=False),
            _row("K001", "B厂", "SKU-B1", inspection=True),
        ]
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 3
        merged = tickets[-1]
        assert merged.items[0].inspection_filter is False
        assert merged.items[0].factory_exclude == ["A厂", "B厂"]
        assert any(w.rule == "non_sj_remainder" for w in merged.warnings)

    def test_no_inspection_rows_whole_container(self):
        """柜内无任何商检行 → 整柜合票，不拆分。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=False),
            _row("K001", "B厂", "SKU-B1", inspection=False),
        ]
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 1
        assert tickets[0].items[0].is_partial is False
        assert tickets[0].sj_factories == []

    def test_beilai_mixed_zhengda_plain_end_to_end(self):
        """端到端冒烟：贝来（软木板=商检 + 写字板=不商检）+ 正达（=商检）
        + 其他厂（=不商检）同柜 → 3 票；贝来写字板行落在不商检合并票；
        rows_for_ticket 展开无交集、合起来恰好覆盖全柜。"""
        from app.declare.aggregator import rows_for_ticket

        items = [
            _row("K001", "青島貝来", "SKU-软木板", inspection=True),
            _row("K001", "青島貝来", "SKU-写字板", inspection=False),
            _row("K001", "Ｃ．正達工芸品", "SKU-正达1", inspection=True),
            _row("K001", "上海億鑽五金工具（青島）", "SKU-其他1", inspection=False),
        ]
        proposal = propose(items, {})
        tickets = _all_tickets(proposal)
        assert len(tickets) == 3

        half = {t.items[0].factory_filter: t for t in tickets
                if t.items[0].inspection_filter is True}
        merged = [t for t in tickets if t.items[0].inspection_filter is False]
        assert sorted(half) == ["青島貝来", "Ｃ．正達工芸品"]
        assert len(merged) == 1
        assert merged[0].items[0].factory_exclude == ["青島貝来", "Ｃ．正達工芸品"]

        rows_by_ticket = {
            t.ticket_no: rows_for_ticket(t, items, {}) for t in tickets
        }
        # 贝来商检半票只含软木板；写字板（贝来不商检行）落在合并票
        assert [r.sku for r in rows_by_ticket[half["青島貝来"].ticket_no]] == [
            "SKU-软木板",
        ]
        assert sorted(r.sku for r in rows_by_ticket[merged[0].ticket_no]) == [
            "SKU-其他1", "SKU-写字板",
        ]
        # 覆盖完整：无交集、合起来 = 全柜行
        seen: dict[int, str] = {}
        for no, rows in rows_by_ticket.items():
            for r in rows:
                assert id(r) not in seen, f"行被 {seen[id(r)]} 与 {no} 重复覆盖"
                seen[id(r)] = no
        assert len(seen) == len(items)


class TestPerFactoryNonInspectionMode:
    """non_inspection_mode="per_factory"：商检厂的不商检行各自成半票，
    非商检厂全部行合并一票。逐票展开互补、无交集、恰好覆盖全柜。"""

    @staticmethod
    def _assert_complementary_coverage(tickets, items):
        """关键互补不变量：逐票 rows_for_ticket 展开无重复，合起来=全柜行。"""
        from app.declare.aggregator import rows_for_ticket

        seen: dict[int, str] = {}
        rows_by_ticket = {}
        for t in tickets:
            rows = rows_for_ticket(t, items, {})
            rows_by_ticket[t.ticket_no] = rows
            for r in rows:
                assert id(r) not in seen, (
                    f"行 {r.sku} 被 {seen[id(r)]} 与 {t.ticket_no} 重复覆盖"
                )
                seen[id(r)] = t.ticket_no
        assert len(seen) == len(items), "逐票展开合起来未覆盖全柜"
        return rows_by_ticket

    def test_per_factory_beilai_zhengda_other_end_to_end(self):
        """贝来(软木板=商检+写字板=不商检)+正达(全商检)+其他厂(不商检) 同柜
        → 票序列 [贝来商检, 贝来不商检, 正达商检, 合并票(仅其他厂)]；
        正达无不商检行 → 不产正达不商检半票；逐票展开互补覆盖全柜。"""
        items = [
            _row("K001", "青島貝来", "SKU-软木板", inspection=True),
            _row("K001", "青島貝来", "SKU-写字板", inspection=False),
            _row("K001", "Ｃ．正達工芸品", "SKU-正达1", inspection=True),
            _row("K001", "上海億鑽五金工具（青島）", "SKU-其他1", inspection=False),
        ]
        proposal = propose(items, {}, non_inspection_mode="per_factory")
        tickets = _all_tickets(proposal)
        assert len(tickets) == 4

        # 票序列：按厂名排序，每家 sj 厂先商检半票、后不商检半票，合并票最后
        seq = [
            (t.items[0].factory_filter, t.items[0].inspection_filter)
            for t in tickets
        ]
        assert seq == [
            ("青島貝来", True),    # 贝来商检半票
            ("青島貝来", False),   # 贝来不商检半票
            ("Ｃ．正達工芸品", True),  # 正达商检半票（无不商检行→无不商检半票）
            ("上海億鑽五金工具（青島）", False),  # 合并票（仅非商检厂）
        ]
        for t in tickets:
            assert all(it.is_partial for it in t.items)
        # 不商检半票与合并票 sj_factories 恒为空
        assert tickets[1].sj_factories == []
        assert tickets[3].sj_factories == []
        # 合并票保留 non_sj_remainder 警告，message 区分模式
        assert any(w.rule == "non_sj_remainder" for w in tickets[3].warnings)
        assert any("per_factory" in w.message for w in tickets[3].warnings)

        # 关键互补不变量：逐票展开互补、无交集、恰好覆盖全柜
        rows_by_ticket = self._assert_complementary_coverage(tickets, items)
        # sj 厂的不商检行只出现在其 F 厂不商检半票，不在合并票
        assert [r.sku for r in rows_by_ticket[tickets[1].ticket_no]] == [
            "SKU-写字板",
        ]
        # 非 sj 厂全部行只出现在合并票
        assert [r.sku for r in rows_by_ticket[tickets[3].ticket_no]] == [
            "SKU-其他1",
        ]

    def test_per_factory_sj_factory_all_inspection_no_half_ticket(self):
        """某 sj 厂全部行商检（本柜无不商检行）→ 该厂不产不商检半票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "B厂", "SKU-B1", inspection=True),
            _row("K001", "B厂", "SKU-B2", inspection=False),
        ]
        tickets = _all_tickets(propose(items, {}, non_inspection_mode="per_factory"))
        seq = [
            (t.items[0].factory_filter, t.items[0].inspection_filter)
            for t in tickets
        ]
        # A 厂全商检→只有商检半票；B 厂有不商检行→商检半票+不商检半票；
        # 无非商检厂→无合并票
        assert seq == [("A厂", True), ("B厂", True), ("B厂", False)]
        self._assert_complementary_coverage(tickets, items)

    def test_per_factory_no_non_sj_factory_no_merged_ticket(self):
        """柜内无非 sj 厂（全部工厂都含商检品）→ 不产不商检合并票。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "A厂", "SKU-A2", inspection=False),
            _row("K001", "B厂", "SKU-B1", inspection=True),
        ]
        tickets = _all_tickets(propose(items, {}, non_inspection_mode="per_factory"))
        assert len(tickets) == 3
        assert all(t.items[0].factory_filter is not None for t in tickets)
        assert not any(
            it.factory_exclude for t in tickets for it in t.items
        ), "per_factory 且无非商检厂时不应产生合并票"
        self._assert_complementary_coverage(tickets, items)

    def test_per_factory_multiple_non_sj_factories_single_merged_ticket(self):
        """多家非商检厂 → 合并为一票（每厂一个 item），互补覆盖全柜。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "B厂", "SKU-B1", inspection=True),
            _row("K001", "C厂", "SKU-C1", inspection=False),
            _row("K001", "D厂", "SKU-D1", inspection=False),
        ]
        tickets = _all_tickets(propose(items, {}, non_inspection_mode="per_factory"))
        assert len(tickets) == 3
        merged = tickets[-1]
        assert [it.factory_filter for it in merged.items] == ["C厂", "D厂"]
        assert all(it.inspection_filter is False for it in merged.items)
        assert any(w.rule == "non_sj_remainder" for w in merged.warnings)
        rows_by_ticket = self._assert_complementary_coverage(tickets, items)
        assert sorted(r.sku for r in rows_by_ticket[merged.ticket_no]) == [
            "SKU-C1", "SKU-D1",
        ]

    def test_invalid_mode_falls_back_to_merge(self):
        """非法 non_inspection_mode 按 merge 处理（行为与默认一致）。"""
        items = [
            _row("K001", "A厂", "SKU-A1", inspection=True),
            _row("K001", "A厂", "SKU-A2", inspection=False),
            _row("K001", "B厂", "SKU-B1", inspection=True),
            _row("K001", "C厂", "SKU-C1", inspection=False),
        ]
        fallback = _all_tickets(propose(items, {}, non_inspection_mode="bogus"))
        default = _all_tickets(propose(items, {}))
        assert len(fallback) == len(default) == 3
        merged = fallback[-1]
        # merge 语义：合并票 factory_exclude=全部 sj 厂，inspection_filter=False
        assert merged.items[0].factory_exclude == ["A厂", "B厂"]
        assert merged.items[0].inspection_filter is False
        assert any(w.rule == "non_sj_remainder" for w in merged.warnings)
        self._assert_complementary_coverage(fallback, items)

    def test_merge_mode_unaffected_by_default(self):
        """默认参数（merge）：同一输入票序列与 per_factory 不同、与旧行为一致。"""
        items = [
            _row("K001", "青島貝来", "SKU-软木板", inspection=True),
            _row("K001", "青島貝来", "SKU-写字板", inspection=False),
            _row("K001", "Ｃ．正達工芸品", "SKU-正达1", inspection=True),
            _row("K001", "上海億鑽五金工具（青島）", "SKU-其他1", inspection=False),
        ]
        tickets = _all_tickets(propose(items, {}))
        assert len(tickets) == 3
        seq = [
            (t.items[0].factory_filter, t.items[0].factory_exclude,
             t.items[0].inspection_filter)
            for t in tickets
        ]
        assert seq == [
            ("青島貝来", None, True),
            ("Ｃ．正達工芸品", None, True),
            (None, ["青島貝来", "Ｃ．正達工芸品"], False),
        ]
        self._assert_complementary_coverage(tickets, items)