# -*- coding: utf-8 -*-
"""分票确认前的零容错校验（人工修改后的 proposal）。

confirm 前在 router 层早失败：
- 硬错误（HTTP 400，force 也不能过）：
  · 覆盖完整性——按 rows_for_ticket 同口径展开每张票，源数据每一行
    必须恰好被一张票覆盖（无遗漏、无重复）；
  · 空票（items 为空，或展开后不含任何行）；
  · 票级合法性——票内柜与票同港口同箱型、柜号必须存在于源数据；
  · 票结构无法解析（schema 校验失败，如 factory_filter/exclude 互斥）。
- 软警告（force=true 才放行，与引擎规则 8 语义一致）：
  · over_3_full——票内整柜超过 3 个；
  · mixed_sj——票内含多种商检工厂。

行展开复用 app.declare.aggregator.rows_for_ticket（报关生成的同一份实现），
杜绝「校验一套口径、生成一套口径」的实现漂移。

票号重编 renumber_tickets：confirm 落库前按港口内数组顺序重编
（与引擎规则 7 一致，{port}-{i:02d}），不信任前端传来的 ticket_no。
"""

from __future__ import annotations

from collections import defaultdict

from app.declare.aggregator import rows_for_ticket
from app.split.schemas import RawItem, Ticket


def renumber_tickets(proposal: dict) -> None:
    """按港口内数组顺序重编票号（与引擎规则 7 一致），原位修改 proposal。"""
    for pg in proposal.get("ports") or []:
        port = pg.get("port", "")
        for i, td in enumerate(pg.get("groups") or [], start=1):
            td["ticket_no"] = f"{port}-{i:02d}"


def _ticket_label(td: dict, fallback: str) -> str:
    """错误信息里的票标识：优先票号，缺失时用位置描述。"""
    no = (td.get("ticket_no") or "").strip()
    return no or fallback


def validate_confirmed_proposal(
    proposal: dict,
    raw_items: list[dict],
    sj_map: dict[str, bool],
) -> tuple[list[str], list[str]]:
    """校验人工确认的分票方案。

    Args:
        proposal: 人工修改后的 SplitProposal dict。
        raw_items: state['raw_items']（load_filled 产物，RawItem dict 列表）。
        sj_map: {工厂名: 是否商检}。

    Returns:
        (errors, warnings)：errors 非空必须拒收（400）；
        warnings 非空需 force=true 才放行。
    """
    errors: list[str] = []
    warnings: list[str] = []

    items = [RawItem(**d) for d in raw_items]
    id2idx = {id(r): i for i, r in enumerate(items)}

    # 每柜的权威港口/箱型（源数据口径；一柜恒属一港口一箱型，引擎已断言）
    kanri_meta: dict[str, tuple[str, str]] = {}
    for r in items:
        kanri_meta.setdefault(r.kanri_no, (r.port, r.container_type))

    coverage = [0] * len(items)  # 每行被覆盖次数，必须全为 1

    ticket_seq = 0
    for pg in proposal.get("ports") or []:
        pg_port = pg.get("port", "")
        for td in pg.get("groups") or []:
            ticket_seq += 1
            label = _ticket_label(td, f"第 {ticket_seq} 张票")

            # ---- 结构解析（含 factory_filter/exclude 互斥断言） ----
            try:
                ticket = Ticket.model_validate(td)
            except Exception as e:  # noqa: BLE001 解析失败即硬错误
                errors.append(f"票 {label} 数据结构非法：{e}")
                continue

            # ---- 空票 ----
            if not ticket.items:
                errors.append(f"票 {label} 为空票（无任何柜），请删除或拖入柜")
                continue

            # ---- 票级合法性：柜号存在、同港口同箱型 ----
            illegal = False
            for it in ticket.items:
                meta = kanri_meta.get(it.kanri_no)
                if meta is None:
                    errors.append(f"票 {label} 含源数据中不存在的柜号：{it.kanri_no}")
                    illegal = True
                    continue
                if meta[0] != ticket.port or meta[1] != ticket.container_type:
                    errors.append(
                        f"票 {label} 的柜 {it.kanri_no} 与票不符："
                        f"柜属 {meta[0]}/{meta[1]}，票为 "
                        f"{ticket.port}/{ticket.container_type}"
                    )
                    illegal = True
            if pg_port and ticket.port != pg_port:
                errors.append(
                    f"票 {label} 港口 {ticket.port} 与所在分组 {pg_port} 不一致"
                )
                illegal = True
            if illegal:
                continue

            # ---- 行展开（与报关生成同口径） ----
            rows = rows_for_ticket(ticket, items, sj_map)
            if not rows:
                errors.append(
                    f"票 {label} 展开后不含任何行（过滤条件与柜内工厂不匹配）"
                )
                continue
            for r in rows:
                coverage[id2idx[id(r)]] += 1

            # ---- 软警告（按展开结果重算，不信任前端字段） ----
            full_n = sum(1 for it in ticket.items if not it.is_partial)
            if full_n > 3:
                warnings.append(f"票 {label} 内整柜超过 3 个：{full_n}")
            sj = sorted({r.maker for r in rows if sj_map.get(r.maker, False)})
            if len(sj) > 1:
                warnings.append(
                    f"票 {label} 内含多种商检工厂：{'、'.join(sj)}"
                )

    # ---- 覆盖完整性：每行恰好一次 ----
    missing: dict[str, int] = defaultdict(int)   # kanri -> 遗漏行数
    dup: dict[str, int] = defaultdict(int)       # kanri -> 重复行数
    for i, r in enumerate(items):
        if coverage[i] == 0:
            missing[r.kanri_no] += 1
        elif coverage[i] > 1:
            dup[r.kanri_no] += 1
    for k in sorted(missing):
        errors.append(f"柜 {k} 有 {missing[k]} 行未被任何票覆盖（遗漏）")
    for k in sorted(dup):
        errors.append(f"柜 {k} 有 {dup[k]} 行被多张票重复覆盖")

    return errors, warnings
