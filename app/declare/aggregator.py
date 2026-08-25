# -*- coding: utf-8 -*-
"""报关明细聚合器——把票内 RawItem 聚合成报关明细行（DetailRow）。

纯 Python 计算，零 LLM / 零 DB / 零 FastAPI 依赖：
品名组配置与映射索引均以参数传入。

行归属规则（与 split/engine.py 保持一致）：
- 普通整柜票 = 柜内全部行；
- 半票（is_partial, factory_filter=F）= 柜内 maker==F 的行；
- 非商检剩余票（is_partial, factory_exclude=[...]）= 柜内 maker 不在
  排除集内的行。多商检柜拆分时由 engine 追加生成（rule non_sj_remainder），
  与商检半票互补、无交集，合起来恰好覆盖全柜行。
  两种过滤互斥（TicketItem 有 model_validator 断言，此处再断言一次）。

金额守恒说明（set_split）：
- 前 N-1 个组件行 amount = split_price × 套数；
- 最后一个组件行 amount = 源行总金额 − 前 N-1 行已分摊金额（兜底残差）。
  源数据存在同一品名组多个单价变体（如 3件套同时有 21.58/26.04 两种
  单套价，与固定 split_price 总和不恒等），残差兜底保证票级金额守恒，
  且与人工样本（报关横滨B/神户B/东京A）逐分一致；数据一致时残差恰等于
  split_price × 套数。
- 净重不按组件拆（业务决定，组件单重字段存在但不用）：cartons/net/gross
  仅首组件行有值（=源行总箱数/总净重/总毛重），其余组件行 None（留空）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from pypinyin import lazy_pinyin

from app.declare.mapping import _get, lookup
from app.split.schemas import RawItem, Ticket


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------

@dataclass
class DetailRow:
    """报关单明细行。cartons/net/gross 为 None 表示该单元格留空。"""

    seq: int = 0
    name_cn: str = ""
    cartons: Optional[float] = None  # 箱数
    pieces: Optional[float] = None  # 件数
    currency: str = "USD"
    amount: Optional[float] = None  # 金额
    net: Optional[float] = None  # 净重
    gross: Optional[float] = None  # 毛重
    inspection: bool = False
    unit_code: str = ""


@dataclass
class AggregateResult:
    """aggregate_ticket 的返回。"""

    rows: list[DetailRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 票内所有 set_split 组的 (金额合计, 净重合计)，供模版 F16/G16；无组套则 None
    set_subtotal: Optional[tuple[float, float]] = None


# ---------------------------------------------------------------------------
# 行归属
# ---------------------------------------------------------------------------

def rows_for_ticket(
    ticket: Ticket,
    items: list[RawItem],
    sj_map: dict[str, bool],
) -> list[RawItem]:
    """计算一张票实际包含的源行（保持 ticket.items 的柜顺序、柜内源顺序）。

    Args:
        ticket: 分票引擎输出的票。
        items: 归一化后的全部 RawItem。
        sj_map: {工厂名: 是否商检}，与 propose() 使用的一致。
    """
    wanted = {it.kanri_no for it in ticket.items}
    by_kanri: dict[str, list[RawItem]] = defaultdict(list)
    for r in items:
        if r.kanri_no in wanted:
            by_kanri[r.kanri_no].append(r)

    rows: list[RawItem] = []
    for ti in ticket.items:
        cont_rows = by_kanri.get(ti.kanri_no, [])
        if not ti.is_partial:
            # 普通整柜票：柜内全部行
            rows.extend(cont_rows)
            continue
        # 半票两种过滤互斥（schema 层已有 validator，这里兜底断言）
        assert not (ti.factory_filter and ti.factory_exclude), (
            f"柜 {ti.kanri_no}：factory_filter 与 factory_exclude 互斥"
        )
        if ti.factory_exclude:
            # 非商检剩余票：柜内 maker 不在排除集（商检工厂）内的行
            excluded = set(ti.factory_exclude)
            rows.extend(r for r in cont_rows if r.maker not in excluded)
            continue
        # 商检半票：仅 maker==factory_filter 的行；
        # 柜内非商检行由 engine 追加的剩余票承载，不在此并入
        f = ti.factory_filter
        rows.extend(r for r in cont_rows if r.maker == f)
    return rows


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------

def normal_row_sort_key(name: str):
    """普通行排序键：数字/字母开头（ASCII）的品名排在汉字前，汉字按拼音。"""
    if name and ord(name[0]) < 128:
        return (0, name)
    return (1, tuple(lazy_pinyin(name)))


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

def _sum(values) -> float:
    return sum(v for v in values if v is not None)


def _row_currency(rows: list[RawItem], default: str) -> str:
    for r in rows:
        if r.currency:
            return r.currency
    return default


def aggregate_ticket(
    rows: list[RawItem],
    groups: list[dict],
    mapping_index: dict,
    default_currency: str = "USD",
) -> AggregateResult:
    """把票内 RawItem 聚合为报关明细行。

    Args:
        rows: rows_for_ticket 的输出（票内源行，顺序即源数据顺序）。
        groups: 品名组配置（从 DB 读出后转 dict）：
            {"name", "group_type": "set_split"|"box_share",
             "source_name_cn",
             "members": [{"product_name_cn", "display_order",
                          "split_price", "split_net_weight"}]}
        mapping_index: build_mapping_index 的输出。
        default_currency: 源行币制缺失时的默认币制。
    """
    result = AggregateResult()
    if not rows:
        return result

    group_by_source: dict[str, dict] = {g["source_name_cn"]: g for g in groups}

    # ---- 1. 按中文品名聚合（保持首行出现顺序） ----
    class _Agg:
        __slots__ = ("name", "first_idx", "cartons", "pieces", "amount",
                     "net", "gross", "currency")

        def __init__(self, name: str, first_idx: int, currency: str):
            self.name = name
            self.first_idx = first_idx
            self.cartons = 0.0
            self.pieces = 0.0
            self.amount = 0.0
            self.net = 0.0
            self.gross = 0.0
            self.currency = currency

    agg_order: list[str] = []
    agg: dict[str, _Agg] = {}
    for i, r in enumerate(rows):
        name = r.name_cn
        if name not in agg:
            agg[name] = _Agg(name, i, _row_currency(rows, default_currency))
            agg_order.append(name)
        a = agg[name]
        a.cartons += r.pcs or 0
        a.pieces += r.qty_pieces or 0
        a.amount += r.amount or 0.0
        a.net += r.net_weight or 0.0
        a.gross += r.gross_weight or 0.0
        if r.currency:
            a.currency = r.currency

    # ---- 2/3. 品名组拆分 + 生成明细行（组块 / 普通行分开收集） ----
    group_blocks: list[tuple[int, list[DetailRow]]] = []  # (首行出现序号, 组件行)
    normal_rows: list[DetailRow] = []
    set_amount_total = 0.0
    set_net_total = 0.0
    has_set_split = False

    def _enrich(row: DetailRow, src_rows_name: str) -> None:
        """映射查询：带出 inspection / unit_code；未命中记 warning 不阻断。

        命中但 unit_code 为空（None/空串/纯空白）也记 warning：后续会有
        功能自动创建「品名存在但 unit_code 为空」的映射行，届时 lookup
        命中空行、「未命中产品映射」告警消失，空单位代码会静默写进
        报关单，必须单独告警兜底。
        """
        m = lookup(mapping_index, sku="", name_cn=row.name_cn)
        if m is None:
            result.warnings.append(f"未命中产品映射：{row.name_cn}")
            row.inspection = False
            row.unit_code = ""
        else:
            row.inspection = bool(_get(m, "inspection_required", False))
            row.unit_code = _get(m, "unit_code", "") or ""
            if not str(row.unit_code).strip():
                result.warnings.append(
                    f"「{row.name_cn}」命中产品映射但单位代码为空"
                )

    for name in agg_order:
        a = agg[name]
        g = group_by_source.get(name)
        if g is None:
            # 普通行：全列有值
            row = DetailRow(
                name_cn=name,
                cartons=a.cartons,
                pieces=a.pieces,
                currency=a.currency,
                amount=a.amount,
                net=a.net,
                gross=a.gross,
            )
            _enrich(row, name)
            normal_rows.append(row)
            continue

        members = sorted(g["members"], key=lambda m: m["display_order"])
        n = len(members)
        if n == 0:
            result.warnings.append(f"品名组 {g['name']} 无成员，按普通行处理：{name}")
            row = DetailRow(
                name_cn=name, cartons=a.cartons, pieces=a.pieces,
                currency=a.currency, amount=a.amount, net=a.net, gross=a.gross,
            )
            _enrich(row, name)
            normal_rows.append(row)
            continue

        block: list[DetailRow] = []
        if g["group_type"] == "set_split":
            has_set_split = True
            set_amount_total += a.amount
            set_net_total += a.net
            # 前 N-1 行按 split_price×套数，末行兜底残差（保证票级守恒）
            distributed = 0.0
            for i, mem in enumerate(members):
                if i < n - 1:
                    amt = (mem["split_price"] or 0.0) * a.pieces
                    distributed += amt
                else:
                    amt = a.amount - distributed
                row = DetailRow(
                    name_cn=mem["product_name_cn"],
                    cartons=a.cartons if i == 0 else None,
                    pieces=a.pieces,  # 每组件行 pieces=套数
                    currency=a.currency,
                    amount=amt,
                    net=a.net if i == 0 else None,  # 净重不拆，仅首行
                    gross=a.gross if i == 0 else None,
                )
                _enrich(row, name)
                block.append(row)
        elif g["group_type"] == "box_share":
            # 金额等分；cartons/net/gross 仅首行
            share = a.amount / n
            for i, mem in enumerate(members):
                row = DetailRow(
                    name_cn=mem["product_name_cn"],
                    cartons=a.cartons if i == 0 else None,
                    pieces=a.pieces,  # 每行 pieces=源行件数
                    currency=a.currency,
                    amount=share,
                    net=a.net if i == 0 else None,
                    gross=a.gross if i == 0 else None,
                )
                _enrich(row, name)
                block.append(row)
        else:
            result.warnings.append(
                f"未知品名组类型 {g['group_type']!r}，按普通行处理：{name}"
            )
            row = DetailRow(
                name_cn=name, cartons=a.cartons, pieces=a.pieces,
                currency=a.currency, amount=a.amount, net=a.net, gross=a.gross,
            )
            _enrich(row, name)
            normal_rows.append(row)
            continue
        group_blocks.append((a.first_idx, block))

    # ---- 5. 排序：组组件在前（组间按源数据首行出现顺序，组内 display_order），
    #         普通行在后按拼音（ASCII 开头品名排在汉字前） ----
    group_blocks.sort(key=lambda t: t[0])
    normal_rows.sort(key=lambda r: normal_row_sort_key(r.name_cn))

    out: list[DetailRow] = []
    for _, block in group_blocks:
        out.extend(block)
    out.extend(normal_rows)
    for seq, row in enumerate(out, start=1):
        row.seq = seq

    result.rows = out
    if has_set_split:
        result.set_subtotal = (set_amount_total, set_net_total)
    return result
