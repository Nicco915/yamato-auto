# -*- coding: utf-8 -*-
"""核心规则引擎——纯函数，零 LLM / 零 DB 依赖。

按 9 条规则把柜号拆分为票（Ticket）并生成 SplitProposal。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from app.split.normalize import normalize_maker
from app.split.schemas import (
    PortGroup,
    RawItem,
    SplitProposal,
    Ticket,
    TicketItem,
    Warning,
)


def _collect_container_info(
    items: list[RawItem],
    sj_map: dict[str, bool],
) -> list[dict]:
    """Step 3：收集每柜的工厂全集、商检工厂、港口、箱型、行数。

    Returns:
        List of container info dicts, sorted by (port, container_type, kanri_no).
        Each dict: kanri_no, port, container_type, makers, sj_factories,
                   row_count, maker_row_counts.
    """
    raw_containers: dict[str, dict] = {}
    for item in items:
        k = item.kanri_no
        if k not in raw_containers:
            raw_containers[k] = dict(
                kanri_no=k,
                port=item.port,
                container_type=item.container_type,
                makers=set(),
                sj_factories=set(),
                row_count=0,
                maker_row_counts=defaultdict(int),
                m3=item.m3,          # 柜级属性，每行重复，取首行
                pcs_total=0,         # 箱数合计（SOTOBAKO_D_HACCHU_SU）
            )
        c = raw_containers[k]
        c["makers"].add(item.maker)
        if sj_map.get(item.maker, False):
            c["sj_factories"].add(item.maker)
        c["row_count"] += 1
        c["maker_row_counts"][item.maker] += 1
        if c["m3"] is None and item.m3 is not None:
            c["m3"] = item.m3
        if item.pcs is not None:
            c["pcs_total"] += item.pcs
        # 一柜恒属一港口一箱型
        if c["port"] != item.port:
            raise ValueError(
                f"柜 {k} 跨港口：已有 {c['port']}，又出现 {item.port}"
            )
        if c["container_type"] != item.container_type:
            raise ValueError(
                f"柜 {k} 跨箱型：已有 {c['container_type']}，又出现 {item.container_type}"
            )

    # Sort by (port, container_type, kanri_no)
    result = sorted(
        raw_containers.values(),
        key=lambda c: (c["port"], c["container_type"], c["kanri_no"]),
    )
    return result


def _propose_tickets(
    containers: list[dict],
    sj_map: dict[str, bool],
) -> list[Ticket]:
    """Steps 4-9：拆分、合票、票号。返回按港口排序的票列表。"""
    tickets: list[Ticket] = []

    # Group containers by (port, container_type)
    groups: list[tuple[tuple[str, str], list[dict]]] = []
    current_key = None
    current_group: list[dict] = []
    for c in containers:
        key = (c["port"], c["container_type"])
        if key != current_key:
            if current_group:
                groups.append((current_key, current_group))
            current_key = key
            current_group = [c]
        else:
            current_group.append(c)
    if current_group:
        groups.append((current_key, current_group))

    for (port, ctype), group in groups:
        pending_whole: list[dict] = []  # 待合并的整柜
        pending_sj: set[str] = set()  # 当前待合并票的商检工厂集

        for c in group:
            sj_set = c["sj_factories"]

            # Rule 4: dual-SJ container → split into 2 partial tickets
            if len(sj_set) >= 2:
                # Flush pending whole containers first
                if pending_whole:
                    tickets.append(
                        _build_whole_ticket(
                            pending_whole, port, ctype, ""
                        )
                    )
                    pending_whole = []
                    pending_sj = set()

                # Create 2 partial tickets, ordered by SJ factory name
                sj_list = sorted(sj_set)
                maker_counts = c["maker_row_counts"]

                for sj_factory in sj_list:
                    tickets.append(
                        _build_partial_ticket(
                            kanri_no=c["kanri_no"],
                            port=port,
                            container_type=ctype,
                            factory_filter=sj_factory,
                            ticket_no="",  # to be numbered later
                        )
                    )
                continue

            # Non-dual-SJ container → whole container, check merge compatibility
            container_sj = sj_set  # 0 or 1 element

            # SJ conflict check: both have SJ AND they differ
            if pending_sj and container_sj and pending_sj != container_sj:
                # Flush current pending ticket
                tickets.append(
                    _build_whole_ticket(pending_whole, port, ctype, "")
                )
                pending_whole = [c]
                pending_sj = container_sj
            else:
                pending_whole.append(c)
                if container_sj:
                    pending_sj = pending_sj | container_sj

                # Rule 6: cap at 3 containers per ticket
                if len(pending_whole) >= 3:
                    tickets.append(
                        _build_whole_ticket(pending_whole, port, ctype, "")
                    )
                    pending_whole = []
                    pending_sj = set()

        # Flush remaining whole containers
        if pending_whole:
            tickets.append(
                _build_whole_ticket(pending_whole, port, ctype, "")
            )

    # ---- Ticket numbering (rule 7): per port, sequential ----
    port_counter: dict[str, int] = defaultdict(int)
    for t in tickets:
        port_counter[t.port] += 1
        t.ticket_no = f"{t.port}-{port_counter[t.port]:02d}"

    # ---- Soft warnings (rule 8) ----
    for t in tickets:
        if t.full_containers > 3:
            t.warnings.append(Warning(
                rule="over_3_full",
                message=f"票内整柜超过 3 个：{t.full_containers}",
            ))
        if len(t.sj_factories) > 1:
            t.warnings.append(Warning(
                rule="mixed_sj",
                message=f"票内含多种商检工厂：{t.sj_factories}",
            ))
        # Cross port/type check
        ports_in_ticket = {item.kanri_no: "" for item in t.items}
        types_in_ticket = set()
        for item in t.items:
            # We don't have per-item port/type here, but we can check the container info
            pass
        # Since we build tickets per (port, type) group, cross_port_or_type
        # should never fire. We skip this warning — it's structurally impossible.

    # ---- Rule 9: single-container port → 1 ticket (already handled by algorithm) ----
    # The algorithm naturally produces exactly 1 ticket for a port with 1 non-dual-SJ
    # container, and exactly 2 partial tickets for a port with only 1 dual-SJ container.

    return tickets


def _build_whole_ticket(
    containers: list[dict],
    port: str,
    container_type: str,
    ticket_no: str,
) -> Ticket:
    """Build a Ticket from whole (non-dual-SJ) containers."""
    items: list[TicketItem] = []
    sj_factories: set[str] = set()
    for c in containers:
        items.append(TicketItem(
            kanri_no=c["kanri_no"],
            factory_filter=None,
            is_partial=False,
        ))
        sj_factories.update(c["sj_factories"])

    return Ticket(
        ticket_no=ticket_no,
        port=port,
        container_type=container_type,
        items=items,
        sj_factories=sorted(sj_factories),
        full_containers=len(items),
    )


def _build_partial_ticket(
    kanri_no: str,
    port: str,
    container_type: str,
    factory_filter: str,
    ticket_no: str,
) -> Ticket:
    """Build a partial Ticket for one SJ factory from a dual-SJ container."""
    return Ticket(
        ticket_no=ticket_no,
        port=port,
        container_type=container_type,
        items=[TicketItem(
            kanri_no=kanri_no,
            factory_filter=factory_filter,
            is_partial=True,
        )],
        sj_factories=[factory_filter],
        full_containers=0,
    )


def propose(
    items: list[RawItem],
    sj_map: dict[str, bool],
    normalize_map: dict[str, str] | None = None,
    fallback_sj: list[str] | None = None,
) -> SplitProposal:
    """规则引擎主函数。

    Args:
        items: 归一化后的 RawItem 列表。
        sj_map: {factory_name: is_sj}，来自 normalize.classify_sj_factories。
        normalize_map: 可选，若提供则对 items 原位归一化。
        fallback_sj: 可选，仅用于日志/文档目的，不参与核心逻辑。

    Returns:
        SplitProposal（不含 split_thread_id/source_file，由上层填充）。

    Rules applied in order:
        1. 归一化（若提供 normalize_map）
        2. 商检判定（使用 sj_map）
        3. 收集柜的工厂全集
        4. 拆分（双商检柜 → 2 半票）
        5. 按港口→箱型→柜号排序分组
        6. 合票（至多 3 整柜，至多 1 种商检工厂）
        7. 票号
        8. 软校验
        9. 单柜端口
    """
    # Step 1: Normalize (if map provided)
    if normalize_map:
        for item in items:
            item.maker = normalize_maker(item.maker, normalize_map)

    # Step 2: SJ classification is already in sj_map
    # (If needed, fallback_sj could be used here, but caller pre-computes sj_map.)

    # Steps 3-9
    containers = _collect_container_info(items, sj_map)
    tickets = _propose_tickets(containers, sj_map)

    # Build PortGroups
    port_order: list[str] = []
    port_tickets: dict[str, list[Ticket]] = defaultdict(list)
    for t in tickets:
        if t.port not in port_tickets:
            port_order.append(t.port)
        port_tickets[t.port].append(t)

    port_groups: list[PortGroup] = []
    for port in port_order:
        port_groups.append(PortGroup(port=port, groups=port_tickets[port]))

    # 柜级统计（供 UI 左栏展示 M3 / 箱数）
    container_stats = {
        c["kanri_no"]: {"m3": c["m3"], "pcs": c["pcs_total"]}
        for c in containers
    }

    return SplitProposal(
        status="pending_review",
        ports=port_groups,
        container_stats=container_stats,
    )