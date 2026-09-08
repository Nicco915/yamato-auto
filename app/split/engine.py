# -*- coding: utf-8 -*-
"""核心规则引擎——纯函数，零 LLM / 零 DB 依赖。

按 9 条规则把柜号拆分为票（Ticket）并生成 SplitProposal。

商检判定以行级 RawItem.inspection 为准（SKU 级商检维度）：
柜内"实际含商检品（任一行 inspection==True）的工厂"≥2 家时触发拆分，
拆出 N 张商检半票（inspection_filter=True）+ 柜内存在不商检行时
1 张不商检合并票（inspection_filter=False）。
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
) -> list[dict]:
    """Step 3：收集每柜的工厂全集、实际含商检品的工厂、港口、箱型、行数。

    商检判定以行级 inspection 为准：某厂在本柜有任一行 inspection==True
    即计入 sj_factories；柜内有任一行 inspection==False 则
    has_non_inspection=True（决定拆分时是否需要不商检合并票）。

    Returns:
        List of container info dicts, sorted by (port, container_type, kanri_no).
        Each dict: kanri_no, port, container_type, makers, sj_factories,
                   has_non_inspection, row_count, maker_row_counts.
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
                has_non_inspection=False,
                row_count=0,
                maker_row_counts=defaultdict(int),
                m3=item.m3,          # 柜级属性，每行重复，取首行
                pcs_total=0,         # 箱数合计（SOTOBAKO_D_HACCHU_SU）
            )
        c = raw_containers[k]
        c["makers"].add(item.maker)
        # 行级商检判定：任一行 inspection=True → 该厂计入本柜商检工厂集；
        # 任一行 inspection=False → 本柜存在不商检行
        if item.inspection:
            c["sj_factories"].add(item.maker)
        else:
            c["has_non_inspection"] = True
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

            # Rule 4: 柜内实际含商检品的工厂 ≥2 家 → 每家一张商检半票，
            # 柜内存在不商检行时再追加 1 张不商检合并票
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

                # 每家含商检品的工厂一张商检半票，按厂名排序
                sj_list = sorted(sj_set)

                for sj_factory in sj_list:
                    tickets.append(
                        _build_partial_ticket(
                            kanri_no=c["kanri_no"],
                            port=port,
                            container_type=ctype,
                            factory_filter=sj_factory,
                            inspection_filter=True,
                            ticket_no="",  # to be numbered later
                        )
                    )

                # 柜内存在任何不商检行（非商检厂的行，或商检厂的
                # inspection=False 行）时，追加一张「不商检合并票」——
                # 否则这些行不进任何票，报关单静默丢失
                if c["has_non_inspection"]:
                    remainder = _build_remainder_ticket(
                        kanri_no=c["kanri_no"],
                        port=port,
                        container_type=ctype,
                        factory_exclude=sj_list,
                        inspection_filter=False,
                        ticket_no="",  # to be numbered later
                    )
                    non_sj = sorted(c["makers"] - sj_set)
                    detail = (
                        f"混装非商检工厂（{'、'.join(non_sj)}）"
                        if non_sj
                        else "商检工厂含不商检品"
                    )
                    remainder.warnings.append(Warning(
                        rule="non_sj_remainder",
                        message=(
                            f"柜 {c['kanri_no']} {detail}，"
                            "不商检行（含商检厂的不商检品）合并单独成票"
                        ),
                    ))
                    tickets.append(remainder)
                continue

            # 0 或 1 家含商检品工厂的柜 → 整柜，检查合票兼容性
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
    # The algorithm naturally produces exactly 1 ticket for a port with 1 container
    # having <2 SJ factories, and N 商检半票（+1 不商检合并票）for a ≥2-SJ-factory
    # container.

    return tickets


def _build_whole_ticket(
    containers: list[dict],
    port: str,
    container_type: str,
    ticket_no: str,
) -> Ticket:
    """Build a Ticket from whole containers（各柜实际含商检品的工厂 <2 家）."""
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
    inspection_filter: Optional[bool] = None,
) -> Ticket:
    """Build a partial Ticket for one SJ factory from a multi-SJ container.

    inspection_filter=True 时为 SKU 级商检半票：仅含该厂 inspection==True
    的行；None 为旧语义（含该厂全部行），保留向后兼容。
    """
    return Ticket(
        ticket_no=ticket_no,
        port=port,
        container_type=container_type,
        items=[TicketItem(
            kanri_no=kanri_no,
            factory_filter=factory_filter,
            is_partial=True,
            inspection_filter=inspection_filter,
        )],
        sj_factories=[factory_filter],
        full_containers=0,
    )


def _build_remainder_ticket(
    kanri_no: str,
    port: str,
    container_type: str,
    factory_exclude: list[str],
    ticket_no: str,
    inspection_filter: Optional[bool] = None,
) -> Ticket:
    """Build a remainder Ticket for the non-SJ part of a multi-SJ container.

    factory_exclude 记录被排除的商检工厂（即该柜全部实际含商检品的工厂）。
    inspection_filter=False 时为 SKU 级不商检合并票：柜内 (maker 不在排除集)
    或 (maker 在排除集但 inspection==False) 的行，与各商检半票互补覆盖全柜；
    None 为旧语义（仅非排除厂的行），保留向后兼容。sj_factories 恒为空。
    """
    return Ticket(
        ticket_no=ticket_no,
        port=port,
        container_type=container_type,
        items=[TicketItem(
            kanri_no=kanri_no,
            factory_exclude=factory_exclude,
            is_partial=True,
            inspection_filter=inspection_filter,
        )],
        sj_factories=[],
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
        items: 归一化后的 RawItem 列表（inspection 字段由上游预标注）。
        sj_map: {factory_name: is_sj}。保留兼容旧调用方签名；引擎内部不再
            使用，商检判定一律以行级 RawItem.inspection 为准。
        normalize_map: 可选，若提供则对 items 原位归一化。
        fallback_sj: 可选，仅用于日志/文档目的，不参与核心逻辑。

    Returns:
        SplitProposal（不含 split_thread_id/source_file，由上层填充）。

    Rules applied in order:
        1. 归一化（若提供 normalize_map）
        2. 商检判定（行级 RawItem.inspection，上游预标注）
        3. 收集柜的工厂全集与实际含商检品的工厂
        4. 拆分（≥2 家实际含商检品工厂 → N 张商检半票 + 1 张不商检合并票）
        5. 按港口→箱型→柜号排序分组
        6. 合票（至多 3 整柜，至多 1 种实际含商检品的工厂）
        7. 票号
        8. 软校验
        9. 单柜端口
    """
    # Step 1: Normalize (if map provided)
    if normalize_map:
        for item in items:
            item.maker = normalize_maker(item.maker, normalize_map)

    # Step 2: 商检判定以行级 RawItem.inspection 为准（上游预标注）；
    # sj_map 形参仅保留兼容，不再参与引擎内部逻辑。

    # Steps 3-9
    containers = _collect_container_info(items)
    tickets = _propose_tickets(containers)

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