# -*- coding: utf-8 -*-
"""提取结果的确定性交叉校验（纯 Python，不调 LLM）。

校验一：重量口径（weight_basis）与单据 Total 行交叉验证
  动机：weight_basis 的判定是模型的语义判断，实测两次出错（2026-07-27）——
  达安把"合计重"误标 per_carton（被乘 75 倍）；亿钻 pdf 把"每箱重"误标 total
  （漏乘 40 倍）。但单据底部印的 Total 行是客观证据：
  - 当前口径下的合计（per_carton 先乘件数再求和）与 Total 行数字吻合 → 通过；
  - 不匹配时尝试**整体翻转口径**再比一次 → 吻合则自动校正并记录；
  - 仍不匹配 → 全部置 needs_human_review，绝不静默放过。
  该校验同时捕获两种方向的误标，使最终结果不再依赖模型的口径判断。

校验二（隐含）：Total 行数字来自源文本照抄，不涉及任何推断。
"""
from __future__ import annotations

import logging
import re

from .schemas import ExtractedItem

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")
_TOTAL_LINE_RE = re.compile(r"(?i)total|合计")
# Total 标签与数值分行的情况（如达安 "TOTAL:" 标签行的数值在后续行内；
# textutil 转的 HTML 去标签后每个单元格独占一行，Total 行的数值可能分散在
# 后续多行，故前瞻行数取宽）
_LOOKAHEAD_LINES = 8
_TOLERANCE = 0.005


def _total_line_numbers(source_text: str) -> list[float]:
    """收集所有 Total/合计 行（含其后若干非空行）出现的数字。

    按非空行计数前瞻：textutil HTML 去标签后每个单元格独占一行且行间有空行
    （亿钻 doc 的 Total 行数字分散在第 3/6/9/12 个非空行，固定行数前瞻会漏）。
    """
    lines = source_text.split("\n")
    pool: list[float] = []
    for i, ln in enumerate(lines):
        if not _TOTAL_LINE_RE.search(ln):
            continue
        taken = 0
        for j in range(i, len(lines)):
            cell = lines[j].strip()
            if not cell:
                continue
            taken += 1
            for m in _NUM_RE.findall(cell):
                try:
                    pool.append(float(m.replace(",", "")))
                except ValueError:
                    pass
            if taken > _LOOKAHEAD_LINES:
                break
    return pool


def _close(a: float, b: float, tol: float = _TOLERANCE) -> bool:
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= tol


def verify_weight_basis(
    items: list[ExtractedItem], source_text: str
) -> tuple[list[ExtractedItem], list[str]]:
    """重量口径与 Total 行交叉验证。返回 (校正后的 items, 校验备注)。"""
    notes: list[str] = []
    pool = _total_line_numbers(source_text)
    weighted = [
        it for it in items
        if it.total_quantity and (it.total_net_weight is not None or it.total_gross_weight is not None)
    ]
    if not pool or not weighted:
        return items, notes

    def sums(flip: bool) -> tuple[float, float]:
        nw = gw = 0.0
        for it in weighted:
            # flip=False：按当前标注口径折算合计；flip=True：整体翻转口径
            per_carton = (it.weight_basis == "per_carton") != flip
            factor = it.total_quantity if per_carton else 1.0
            nw += (it.total_net_weight or 0) * factor
            gw += (it.total_gross_weight or 0) * factor
        return nw, gw

    def matches(nw: float, gw: float) -> bool:
        return any(_close(nw, p) for p in pool) or any(_close(gw, p) for p in pool)

    # 留痕用的来源标识（工厂/文件路径，便于按批次回放）
    src = next((it.source_file for it in weighted if it.source_file), "（未知文件）")

    cur_nw, cur_gw = sums(flip=False)
    if matches(cur_nw, cur_gw):
        logger.debug(
            "重量口径交叉校验通过（无翻正）| 文件=%s | 合计 NW=%g/GW=%g 与 Total 行吻合",
            src, cur_nw, cur_gw)
        return items, notes

    flip_nw, flip_gw = sums(flip=True)
    if matches(flip_nw, flip_gw):
        # 达安×75、亿钻×40 两类真实事故的防线：翻正必须留痕（WARNING）
        basis_before = weighted[0].weight_basis if weighted else "?"
        basis_after = "per_carton" if basis_before != "per_carton" else "total"
        matched = next(
            (p for p in pool if _close(flip_nw, p) or _close(flip_gw, p)), None)
        logger.warning(
            "重量口径交叉校验翻正 | 文件=%s | 原标注=%s → 翻正后=%s | "
            "原口径合计 NW=%g/GW=%g 与 Total 行不符，翻正后 NW=%g/GW=%g 匹配 Total 行数值 %s",
            src, basis_before, basis_after, cur_nw, cur_gw, flip_nw, flip_gw, matched)
        for it in weighted:
            it.weight_basis = "per_carton" if it.weight_basis != "per_carton" else "total"
        notes.append(
            f"重量口径误标已自动校正（校正前合计 NW={cur_nw:g}/GW={cur_gw:g} 与 Total 行不符，"
            f"校正后 NW={flip_nw:g}/GW={flip_gw:g} 匹配）"
        )
        return items, notes

    logger.warning(
        "重量口径交叉校验不符，已强制人工审核 | 文件=%s | 合计 NW=%g/GW=%g，"
        "翻转口径后 NW=%g/GW=%g 亦不匹配 Total 行数值池=%s",
        src, cur_nw, cur_gw, flip_nw, flip_gw, pool[:10])
    for it in weighted:
        it.needs_human_review = True
        it.review_reason = (it.review_reason or "") + "；重量合计与单据 Total 行交叉校验不符"
    notes.append(
        f"重量合计与单据 Total 行不符（NW={cur_nw:g}/GW={cur_gw:g}，翻转口径后 "
        f"NW={flip_nw:g}/GW={flip_gw:g} 亦不匹配），已全部标记人工审核"
    )
    return items, notes
