# -*- coding: utf-8 -*-
"""分票规则引擎的数据模型定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RawItem(BaseModel):
    """filled Excel 的一行——即一个 SKU 条目（归一化后）。"""

    kanri_no: str  # 虚拟柜号
    port: str  # 港口 MINATO_MEI_KJ
    container_type: str  # 箱型 CONTAINER_MEI
    maker: str  # 工厂（已归一化）
    sku: str  # SHOHIN_CD
    net_weight: float | None  # 净重
    gross_weight: float | None  # 毛重
    pcs: int | None  # SOTOBAKO_D_HACCHU_SU 箱数
    # ---- 报关生成扩展字段（追加式，均有默认值，不影响分票构造调用） ----
    name_cn: str = ""  # 中文品名（32 列）
    qty_pieces: int | None = None  # 件数 D_HACCHU_SU（35 列）
    amount: float | None = None  # 金额 KAKAKUKEI（51 列）
    currency: str = ""  # 币制 TSUKA_MEI（47 列）
    m3: float | None = None  # 体积 M3（14 列，柜级属性，每行重复）


class TicketItem(BaseModel):
    """票内的一条——整柜或半柜。"""

    kanri_no: str
    factory_filter: Optional[str] = None  # None=整柜，非空=该半票只含此工厂部分
    is_partial: bool = False


class Warning(BaseModel):
    """规则违反警告（软校验，不阻止成票）。"""

    rule: str  # 违反的规则标识，如 "mixed_sj" / "over_3_full"
    message: str  # 中文警告文字


class Ticket(BaseModel):
    """一张票——同一港口、至多一种商检工厂、至多 3 个整柜。"""

    ticket_no: str  # 「東京港-01」
    port: str
    container_type: str
    items: list[TicketItem] = Field(default_factory=list)  # 按柜号排序
    sj_factories: list[str] = Field(default_factory=list)  # 票内商检工厂（去重，≤1 合法）
    full_containers: int = 0  # 整柜数量（用于 ≤3 校验）
    warnings: list[Warning] = Field(default_factory=list)


class PortGroup(BaseModel):
    """港口分组，持有该港口下所有票。"""

    port: str
    groups: list = Field(default_factory=list)  # 先不严格类型约束，简化（实际为 list[Ticket]）


class SplitProposal(BaseModel):
    """最终抛给中断页的 payload。"""

    split_thread_id: str = ""
    source_file: str = ""
    status: str = "pending_review"  # pending_review / confirmed / reset
    ports: list[PortGroup] = Field(default_factory=list)
    # 柜级统计（供 UI 展示）：{kanri_no: {"m3": float|None, "pcs": int|None}}
    container_stats: dict[str, dict] = Field(default_factory=dict)