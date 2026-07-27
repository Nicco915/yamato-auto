# -*- coding: utf-8 -*-
"""提取结果的结构化 Schema 定义（第二阶段设计文档第 3 节）。

防幻觉铁律（第二阶段第 4 节）在 prompt 中强制执行：
1. 只抄录单据原文数值，禁止模型做任何乘除法/单位换算；
2. 单据上没写的字段一律 null；
3. 品名原样照抄，留给下游模糊匹配。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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


# 给模型看的 JSON 输出模板（写进 prompt，防幻觉规则的一部分）
OUTPUT_JSON_TEMPLATE = """{
  "items": [
    {
      "sku_name": "原样照抄的品名",
      "sku_code": "商品编码或 null",
      "total_quantity": 数字或null,
      "total_net_weight": 数字或null,
      "total_gross_weight": 数字或null,
      "weight_unit": "KG或null",
      "needs_human_review": false,
      "review_reason": null
    }
  ]
}"""
