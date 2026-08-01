# -*- coding: utf-8 -*-
"""通道三（快速路）：文本型 PDF 通道。

设计依据：纯电子版 PDF（非扫描件）自带文本层，直接用 PyMuPDF 抽取文字
交给文本大模型做语义对齐即可，无需渲染图片走视觉通道：
- 成本约为视觉通道的 1/10（省掉每页上千 image tokens）
- 数字零失真（不存在 OCR 误识别风险）
- 速度更快，规避密集报关 PDF 的视觉调用 120s 超时问题

判定规则：全文可提取字符数 / 页数 < MIN_CHARS_PER_PAGE 视为扫描件，
回退视觉通道。
"""
from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF

from . import llm_client
from .excel_channel import (
    ChannelResult,
    UnsupportedFileError,
    sort_by_source_order,
)
from .schemas import apply_weight_basis
from .verify import verify_weight_basis
from .prompts import (
    SYSTEM_PROMPT,
    JsonParseError,
    build_retry_message,
    build_text_user_prompt,
    parse_payload,
)

MIN_CHARS_PER_PAGE = 50  # 每页平均可提取字符数阈值，低于此值判定为扫描件
MAX_TOTAL_PAGES = 12  # 单文件最多处理的页数（与视觉通道一致，控制成本）
MAX_PDF_TEXT_CHARS = 20000  # 文本最长字符数，超出截断

logger = logging.getLogger(__name__)


def pdf_to_text(file_path: str) -> tuple[str, bool]:
    """抽取 PDF 文本层。

    返回 (文本, 是否有可用文本层)。sort=True 按视觉位置重排文本块，
    尽量保留表格的阅读顺序，便于大模型语义对齐。
    """
    doc = fitz.open(file_path)
    try:
        texts: list[str] = []
        for i, page in enumerate(doc):
            if i >= MAX_TOTAL_PAGES:
                break
            texts.append(f"--- 第 {i + 1} 页 ---\n" + page.get_text("text", sort=True))
        full = "\n".join(texts).strip()
        page_count = min(len(doc), MAX_TOTAL_PAGES) or 1
        has_text = len(full) / page_count >= MIN_CHARS_PER_PAGE
        return full, has_text
    finally:
        doc.close()


def extract_pdf_text(file_path: str) -> ChannelResult:
    """提取单个文本型 PDF 文件，返回结构化结果（JSON 解析失败最多重试 2 次）。"""
    result = ChannelResult()
    text, has_text = pdf_to_text(file_path)
    if not has_text:
        raise UnsupportedFileError(f"PDF 无可用文本层（判定为扫描件）: {file_path}")
    full_text = text  # 校验/排序用完整文本（Total 行在文档末尾，不能用截断版）
    if len(text) > MAX_PDF_TEXT_CHARS:
        text = text[:MAX_PDF_TEXT_CHARS] + "\n\n[... 内容过长已截断 ...]"

    source_name = Path(file_path).name
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_text_user_prompt(text, source_name)},
    ]

    for attempt in range(3):
        result.json_attempts += 1
        raw = llm_client.chat_completion(
            messages, vision=False, source_file=source_name
        )
        try:
            payload = parse_payload(raw)
            for item in payload.items:
                item.source_file = file_path
            verified, notes = verify_weight_basis(payload.items, full_text)
            result.notes.extend(notes)
            result.items = apply_weight_basis(sort_by_source_order(verified, full_text))
            return result
        except JsonParseError as e:
            result.json_parse_failures += 1
            if attempt >= 2:
                # 重试耗尽：带堆栈记 ERROR 进 error.log，便于事后追查
                logger.exception(
                    "JSON 解析重试耗尽，最终失败 | %s | 共 %d 次尝试 | %s",
                    source_name, attempt + 1, str(e)[:300],
                )
                result.error = f"JSON 解析重试 2 次后仍失败: {e}"
                return result
            logger.warning(
                "JSON 解析失败，第 %d/2 次重试 | %s | 错误: %s",
                attempt + 1, source_name, str(e)[:200],
            )
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": build_retry_message(raw, str(e))},
            ]
    return result
