# -*- coding: utf-8 -*-
"""分票规则引擎的数据模型定义。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


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
    # SKU 级商检标志：由上游从 factory_skus/product_mappings 解析后标注；
    # 默认 False = 不商检。追加式字段，旧数据无此键即 False，向后兼容。
    inspection: bool = False


class TicketItem(BaseModel):
    """票内的一条——整柜或半柜。

    三种形态（由 factory_filter / factory_exclude / inspection_filter 组合定义）：
    1. 整柜（is_partial=False）：柜内全部行，三个过滤字段均不生效。
    2. 旧语义半票（inspection_filter=None，向后兼容）：
       - factory_filter=F：柜内 maker==F 的全部行（不分商检与否）；
       - factory_exclude=[...]：柜内 maker 不在排除集内的全部行。
    3. SKU 级商检半票/合并票（inspection_filter 非 None）：
       - inspection_filter=True 搭配 factory_filter=F：柜内 maker==F
         且 inspection==True 的行（商检半票）；
       - inspection_filter=False 搭配 factory_filter=F：柜内 maker==F
         且 inspection==False 的行（F 厂不商检半票，per_factory 模式）；
       - inspection_filter=False 搭配 factory_exclude=[...]：柜内
         (maker 不在排除集) 或 (maker 在排除集但 inspection==False) 的行
         （不商检合并票，与各商检半票互补、合起来恰好覆盖全柜）。
    """

    kanri_no: str
    factory_filter: Optional[str] = None  # None=整柜/剩余票，非空=该半票只含此工厂部分
    # 与 factory_filter 互斥：非空=该半票排除这些工厂（多商检柜的非商检剩余票）。
    # 追加式字段，旧落库记录无此键即 None，向后兼容。
    factory_exclude: Optional[list[str]] = None
    is_partial: bool = False
    # SKU 级商检维度过滤：None=旧语义（见类 docstring 形态 2）；
    # True=商检半票（须搭配 factory_filter）；
    # False=不商检票：搭配 factory_filter=F 为 F 厂不商检半票（per_factory 模式），
    # 搭配 factory_exclude=[...] 为不商检合并票。
    inspection_filter: Optional[bool] = None

    @model_validator(mode="after")
    def _filter_exclude_mutex(self) -> "TicketItem":
        """校验 factory_filter / factory_exclude / inspection_filter 的合法组合。"""
        if self.factory_filter and self.factory_exclude:
            raise ValueError(
                f"柜 {self.kanri_no}：factory_filter 与 factory_exclude 互斥，"
                "只能设置其一"
            )
        if self.inspection_filter is True and not self.factory_filter:
            raise ValueError(
                f"柜 {self.kanri_no}：inspection_filter=True（商检半票）"
                "必须搭配 factory_filter 指定商检工厂"
            )
        if (self.inspection_filter is False
                and not (self.factory_exclude or self.factory_filter)):
            raise ValueError(
                f"柜 {self.kanri_no}：inspection_filter=False（不商检票）"
                "必须搭配 factory_exclude（不商检合并票）或 factory_filter"
                "（F 厂不商检半票）"
            )
        return self


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