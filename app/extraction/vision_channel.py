# -*- coding: utf-8 -*-
"""通道一：视觉大模型通道（PDF / JPG / PNG）。

流程（第二阶段设计文档第 2 节）：
1. PyMuPDF(fitz) 把 PDF 按页渲染为 >=200 DPI 的 PNG 图片；
2. 图片转 base64 data URI，以 image_url 形式传给视觉大模型；
3. 多页 PDF 分批传入，逐批提取后合并结果。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import fitz  # PyMuPDF

from . import llm_client
from .schemas import apply_weight_basis
from .excel_channel import ChannelResult, UnsupportedFileError
from .prompts import (
    SYSTEM_PROMPT,
    JsonParseError,
    build_retry_message,
    build_vision_user_prompt,
    parse_payload,
)

RENDER_DPI = 200  # 渲染分辨率（>=200 DPI）
MAX_PAGES_PER_CALL = 4  # 单次调用最多传入的页数（控制 token 与超时风险）
MAX_TOTAL_PAGES = 12  # 单文件最多处理的页数（控制成本）
MAX_IMAGE_EDGE = 2000  # 图片最长边像素上限，超出则等比缩小

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
PDF_SUFFIXES = {".pdf"}

logger = logging.getLogger(__name__)


def _png_bytes_to_data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def render_pdf_pages(file_path: str, dpi: int = RENDER_DPI) -> list[bytes]:
    """把 PDF 每页渲染为 PNG 字节流（>=200 DPI，最长边受限）。"""
    doc = fitz.open(file_path)
    pages: list[bytes] = []
    zoom = dpi / 72.0
    try:
        for page in doc:
            if len(pages) >= MAX_TOTAL_PAGES:
                break
            # 若 200DPI 下最长边超限则等比降低 zoom
            rect = page.rect
            edge = max(rect.width, rect.height) * zoom
            z = zoom if edge <= MAX_IMAGE_EDGE else zoom * MAX_IMAGE_EDGE / edge
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
            pages.append(pix.tobytes("png"))
    finally:
        doc.close()
    if not pages:
        raise UnsupportedFileError(f"PDF 无页面: {file_path}")
    return pages


def load_image_file(file_path: str) -> list[bytes]:
    """读取 jpg/png 为字节流（统一转 PNG 由调用方决定，这里直接按原格式传 data URI）。"""
    data = Path(file_path).read_bytes()
    if not data:
        raise UnsupportedFileError(f"图片文件为空: {file_path}")
    return [data]


def _image_data_uri(file_path: str, data: bytes) -> str:
    suffix = Path(file_path).suffix.lower()
    mime = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _extract_batch(
    image_uris: list[str], source_name: str, source_file: str
) -> ChannelResult:
    """把一批页面图片发给视觉模型并解析 JSON（解析失败最多重试 2 次）。"""
    result = ChannelResult()
    content: list[dict] = [
        {"type": "text", "text": build_vision_user_prompt(source_name)}
    ]
    for uri in image_uris:
        content.append({"type": "image_url", "image_url": {"url": uri}})

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]

    # 排查日志：统计每批请求体体积
    text_len = sum(len(str(p.get("text", ""))) for p in content if p.get("type") == "text")
    image_lens = [len(uri) for uri in image_uris]
    total_image_len = sum(image_lens)
    logger.info(
        "视觉请求体积 | %s | 图片数=%d | 单张base64长度=%s | "
        "图片总长度=%d | 文本长度=%d | 估算总长度=%d",
        source_name,
        len(image_uris),
        "/".join(str(l) for l in image_lens) if image_lens else "0",
        total_image_len,
        text_len,
        text_len + total_image_len,
    )

    for attempt in range(3):
        result.json_attempts += 1
        raw = llm_client.chat_completion(
            messages, vision=True, source_file=source_name
        )
        try:
            payload = parse_payload(raw)
            for item in payload.items:
                item.source_file = source_file
            result.items = apply_weight_basis(payload.items)
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


def extract_vision(file_path: str) -> ChannelResult:
    """提取单个 PDF/图片文件。多页 PDF 分批处理并合并结果。"""
    suffix = Path(file_path).suffix.lower()
    source_name = Path(file_path).name

    if suffix in PDF_SUFFIXES:
        pages = render_pdf_pages(file_path)
        uris = [_png_bytes_to_data_uri(p) for p in pages]
    elif suffix in IMAGE_SUFFIXES:
        uris = [_image_data_uri(file_path, d) for d in load_image_file(file_path)]
    else:
        raise UnsupportedFileError(f"不是影像文件: {file_path}")

    merged = ChannelResult()
    for i in range(0, len(uris), MAX_PAGES_PER_CALL):
        batch = uris[i : i + MAX_PAGES_PER_CALL]
        part = _extract_batch(batch, source_name, file_path)
        merged.items.extend(part.items)
        merged.json_attempts += part.json_attempts
        merged.json_parse_failures += part.json_parse_failures
        if part.error:
            merged.error = (merged.error + " | " + part.error).strip(" |")
    return merged
