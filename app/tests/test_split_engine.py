# -*- coding: utf-8 -*-
"""分票规则引擎单测（pytest）。

真实文件：ContentsOfTheContainer_202624_青島XD_20260708.xlsx
27 柜，10 双商检柜，17 非双商检柜。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.split.engine import propose
from app.split.loader import load_filled_excel
from app.split.normalize import classify_sj_factories, normalize_maker

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
    """Return the set of kanri_no that have >=2 SJ factories (after normalization)."""
    from collections import defaultdict
    sj_set = {"青島貝来", "Ｃ．正達工芸品"}
    container_makers = defaultdict(set)
    for item in raw_items:
        container_makers[item.kanri_no].add(item.maker)
    dual = {
        k for k, makers in container_makers.items()
        if len(makers & sj_set) >= 2
    }
    return dual


# ---- Fixture ----

@pytest.fixture(scope="module")
def proposal():
    """Load real file, normalize, classify SJ, and run engine."""
    if not _REAL_FILE.exists():
        pytest.skip(f"Real data file not found: {_REAL_FILE}")

    raw = load_filled_excel(_REAL_FILE)
    # Step 1: normalize maker names
    for r in raw:
        r.maker = normalize_maker(r.maker, NORMALIZE_MAP)
    # Step 2: classify SJ factories (master_inspection empty, rely on fallback)
    sj_map = classify_sj_factories(raw, {}, FALLBACK_SJ)
    # Step 3+: run engine
    result = propose(raw, sj_map)
    return result


# ---- Fixture for raw items (to compute expected counts) ----

@pytest.fixture(scope="module")
def raw_items():
    """Load and normalize raw items."""
    if not _REAL_FILE.exists():
        pytest.skip(f"Real data file not found: {_REAL_FILE}")
    raw = load_filled_excel(_REAL_FILE)
    for r in raw:
        r.maker = normalize_maker(r.maker, NORMALIZE_MAP)
    return raw


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

    def test_dual_sj_container_yields_2_partial_tickets(self, proposal, raw_items):
        """不变量 3：10 双商检柜各产生 2 张 is_partial 票。"""
        dual_containers = _count_dual_sj_containers(raw_items)
        assert len(dual_containers) == 10, (
            f"预期 10 个双商检柜，实际 {len(dual_containers)}: {sorted(dual_containers)}"
        )

        # Collect kanri_no → count of partial ticket appearances
        partial_appearances: Counter[str] = Counter()
        for pg in proposal.ports:
            for ticket in pg.groups:
                for item in ticket.items:
                    if item.is_partial:
                        partial_appearances[item.kanri_no] += 1

        for k in dual_containers:
            count = partial_appearances.get(k, 0)
            assert count == 2, (
                f"双商检柜 {k} 预期 2 张半票，实际 {count}"
            )

    def test_all_containers_covered(self, proposal, raw_items):
        """不变量 5：全部 27 柜无遗漏无重复，双商检柜恰好出现 2 次。"""
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

        # Check dual-SJ: each appears exactly 2 times
        for k in dual_containers:
            assert appearances.get(k, 0) == 2, (
                f"双商检柜 {k} 预期出现 2 次，实际 {appearances.get(k, 0)}"
            )

        # Check non-dual-SJ: each appears exactly 1 time
        for k in non_dual:
            assert appearances.get(k, 0) == 1, (
                f"非双商检柜 {k} 预期出现 1 次，实际 {appearances.get(k, 0)}"
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