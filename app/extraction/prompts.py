# -*- coding: utf-8 -*-
"""防幻觉 Prompt（第二阶段设计文档第 4 节 "Tight Constraints"）与 JSON 解析工具。"""
from __future__ import annotations

import json
import re

from .schemas import OUTPUT_JSON_TEMPLATE, ExtractionPayload

SYSTEM_PROMPT = """你是供应链单证数据提取员。任务：从工厂提供的装箱单/发票中提取每个商品(SKU)一行的合计数据。

【铁律——违反任何一条都视为提取失败】
1. 绝对忠于原文：只抄录单据上白纸黑字印出的数值。单据上没有明确印出的字段，一律填 null。
2. 绝对禁止数学计算：禁止做任何乘法、除法、加法、单位换算。例如单据只有"单件净重"和"件数"而没有印出"总净重"时，total_net_weight 必须填 null，严禁自行推算。所有计算由下游程序完成。
3. 品名原样照抄：sku_name 必须完整照抄单据上的原始品名文本（含规格词汇），不得翻译、改写、补全或简化。
4. total_quantity 专指包装件数（箱数），常见列名：件数/箱数/PACKAGES/CTNS/CARTONS/外箱数。若单据只有内装个数（QUANTITY/数量/PCS/SETS）而没有箱数，total_quantity 填 null。
5. total_net_weight / total_gross_weight 只提取该 SKU 行印出的"合计"净重/毛重（常见列名：NET WEIGHT/N.W./净重、GROSS WEIGHT/G.W./毛重）。若该行只有单件重量，填 null。
   特例一：若单据把毛重/净重印在同一格（如列名 "GROSS/NET WEIGHT"，单元格内容为 "106.00/90.00"），按单据标注的顺序原样拆分抄录：前一个数填 total_gross_weight，后一个数填 total_net_weight。这属于照抄，不算计算。若无法确定顺序，两个字段都填 null 并将 needs_human_review 置 true。
   特例二（重量口径）：若该行印出的重量是"每箱/每件"口径而非合计（线索：列名含 /CTN、KGS/CTN、per carton 等；或底部 Total 行的合计远大于该行数值且该行数值×件数≈合计），数值仍照抄到 total_net_weight/total_gross_weight，但必须把 weight_basis 设为 "per_carton"（合计口径则填 "total"）。换算由下游程序完成，你禁止自行相乘。无法判断口径时填 "total" 并将 needs_human_review 置 true。
6. weight_unit 照抄单据上的重量单位（如 KG、LB、g）。未标明时填 null。
7. sku_code：单据上的商品编码/JAN CODE/货号/品番（常为 13 位数字），有才照抄，没有填 null。
8. needs_human_review：出现以下任一情况必须为 true —— 影像模糊或印章遮挡导致无法辨认；表格结构混乱导致行与列的对应关系无法确定；某 SKU 的件数和重量全部缺失；对某个数值没有把握。同时在 review_reason 中简述原因。
9. 发票(Invoice)中通常没有重量数据：若文件是发票且确实没有重量/件数列，提取件数字段（如有）即可，重量填 null，不要因为缺失重量而把 needs_human_review 全部置 true——只有当你无法确定行列对应关系时才置 true。
10. 只输出一个 JSON 对象，不要输出任何解释文字、不要 markdown 代码块。

【输出格式】
""" + OUTPUT_JSON_TEMPLATE


def build_text_user_prompt(markdown_text: str, source_name: str) -> str:
    """文本通道的用户提示词。"""
    return (
        f"以下是由工厂 Excel 单据传成的 Markdown 表格文本（文件名：{source_name}）。"
        "合并单元格已被拆平，空单元格为空白。请按系统指令提取全部 SKU 行的数据，"
        "注意同一份文件可能包含装箱单和发票两个部分，装箱单(Packing List)优先取件数与重量：\n\n"
        + markdown_text
    )


def build_vision_user_prompt(source_name: str) -> str:
    """视觉通道的用户提示词（图片以 image_url 形式随消息传入）。"""
    return (
        f"以上是工厂单据（文件名：{source_name}）的扫描影像，可能有多页。"
        "请像人工审单员一样看懂表格结构，按系统指令提取全部 SKU 行的数据，"
        "只输出 JSON。"
    )


# ---------------- JSON 解析（带容错） ----------------

class JsonParseError(ValueError):
    """模型输出无法解析为合法 JSON。"""


def _strip_code_fence(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def parse_payload(raw_text: str) -> ExtractionPayload:
    """把模型输出解析为 ExtractionPayload。

    容错策略：去代码围栏 → 直接 json.loads → 截取第一个 { 到最后一个 } 再解析。
    失败抛 JsonParseError，由调用方决定是否重试。
    """
    text = _strip_code_fence(raw_text).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    last_err: Exception | None = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, list):  # 模型直接给了数组
                data = {"items": data}
            return ExtractionPayload.model_validate(data)
        except Exception as e:  # noqa: BLE001 - 需要尝试下一个候选
            last_err = e
    raise JsonParseError(f"JSON 解析失败: {last_err}; 原文前200字: {raw_text[:200]!r}")


def build_retry_message(raw_text: str, error: str) -> str:
    """解析失败时的追问消息（最多重试 2 次）。"""
    return (
        "你上一次的输出不是合法 JSON，解析报错："
        f"{error[:300]}\n上次输出前300字：{raw_text[:300]!r}\n"
        "请仅重新输出一个合法 JSON 对象（不要代码块、不要解释），格式如前。"
    )
