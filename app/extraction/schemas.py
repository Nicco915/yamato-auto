# -*- coding: utf-8 -*-
"""提取结果的结构化 Schema 定义（第二阶段设计文档第 3 节）。

防幻觉铁律（第二阶段第 4 节）在 prompt 中强制执行：
1. 只抄录单据原文数值，禁止模型做任何乘除法/单位换算；
2. 单据上没写的字段一律 null；
3. 品名原样照抄，留给下游模糊匹配。
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ExtractedItem(BaseModel):
    """单个 SKU 的提取结果。数值字段允许为空：空 = 单据上确实没写，交由下游/人工处理。"""

    sku_name: Optional[str] = Field(
        default=None, description="原样照抄的品名（不得翻译/改写），缺失为 null"
    )
    sku_code: Optional[str] = Field(
        default=None,
        description="商品编码/JAN CODE/货号（如 13 位数字），单据上有才填，没有为 null",
    )
    total_quantity: Optional[float] = Field(
        default=None,
        description="该 SKU 的包装件数（箱数/CTNS/PACKAGES/件数），单据未印出则为 null",
    )
    total_net_weight: Optional[float] = Field(
        default=None, description="该 SKU 印出的合计净重，未印出为 null，禁止用单重×件数推算"
    )
    total_gross_weight: Optional[float] = Field(
        default=None, description="该 SKU 印出的合计毛重，未印出为 null"
    )
    weight_basis: str = Field(
        default="total",
        description="重量口径：total=该行印出的就是合计重量；per_carton=该行印出的是每箱/每件重量"
        "（此种情况下数值仍照抄，换算由下游纯 Python 完成）",
    )
    weight_unit: Optional[str] = Field(
        default=None, description="重量单位，照抄单据（KG/LB/g 等），未标明为 null"
    )
    needs_human_review: bool = Field(
        default=False,
        description="单据模糊/印章遮挡/关键字段缺失/行列对应关系无法确定时为 true",
    )
    review_reason: Optional[str] = Field(
        default=None, description="触发人工审核的原因简述"
    )
    source_file: str = Field(
        default="", description="来源文件路径（由 pipeline 填充，模型不需要输出）"
    )


class ExtractionPayload(BaseModel):
    """模型单次调用的输出包裹：{\"items\": [...]}"""

    items: list[ExtractedItem] = Field(default_factory=list)


def apply_weight_basis(items: list[ExtractedItem]) -> list[ExtractedItem]:
    """把 per_carton 口径的重量由纯 Python 换算为合计（单箱重 × 件数）。

    计算隔离铁律不变：LLM 只做语义判断（"这列是每箱重"），乘法在这里发生。
    件数缺失无法换算时置 needs_human_review，绝不静默丢数。
    （2026-07-27 亿钻案例：通用箱单只印每箱毛/净重，如 13.00/12.00 × 50 CTNS。）
    """
    converted = 0
    for it in items:
        if it.weight_basis != "per_carton":
            continue
        if it.total_quantity:
            if it.total_net_weight is not None:
                it.total_net_weight = round(it.total_net_weight * it.total_quantity, 3)
            if it.total_gross_weight is not None:
                it.total_gross_weight = round(it.total_gross_weight * it.total_quantity, 3)
            it.weight_basis = "total"
            converted += 1
        else:
            it.needs_human_review = True
            it.review_reason = (it.review_reason or "") + "重量为每箱口径但件数缺失，无法换算合计"
            # 「绝不静默丢数」铁律执行点：无法换算必须留痕（WARNING）
            logger.warning(
                "每箱口径重量无法换算，已强制人工审核 | SKU=%s（%s）| 件数缺失 | 文件=%s",
                it.sku_code or "（无编码）", it.sku_name or "（无品名）",
                it.source_file or "（未知文件）")
    if converted:
        logger.info("每箱口径重量换算完成 | per_carton→total 条目数=%d（单箱重×件数）", converted)
    return items


# 给模型看的 JSON 输出模板（写进 prompt，防幻觉规则的一部分）
OUTPUT_JSON_TEMPLATE = """{
  "items": [
    {
      "sku_name": "原样照抄的品名",
      "sku_code": "商品编码或 null",
      "total_quantity": 数字或null,
      "total_net_weight": 数字或null,
      "total_gross_weight": 数字或null,
      "weight_basis": "total或per_carton",
      "weight_unit": "KG或null",
      "needs_human_review": false,
      "review_reason": null
    }
  ]
}"""
