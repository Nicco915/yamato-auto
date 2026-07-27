# -*- coding: utf-8 -*-
"""通道四（macOS 兜底）：.doc/.docx → textutil 转 HTML → 文本大模型。

背景：soffice(LibreOffice) 不可用时 .doc 通道断裂（亿钻装箱单为 .doc，
导致重量数据整批缺失）。macOS 自带 textutil 可把 doc/docx 转成 HTML，
表格结构（<table>/<tr>/<td>）完整保留，可直接按文本通道处理：

- textutil -convert html -stdout <file> 输出含 <head> 与大量 CSS 样板，
  这里裁掉 <head> 并去掉 class 属性，体积约减半（实测 34KB → 17.6KB）；
- 仍超出 MAX_DOC_HTML_CHARS 时按现有策略截断；
- 非 macOS / textutil 不存在 / 转换失败时抛 UnsupportedFileError，
  由 pipeline 记入 unsupported_files（维持原行为）。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import llm_client
from .excel_channel import ChannelResult, UnsupportedFileError
from .prompts import (
    SYSTEM_PROMPT,
    JsonParseError,
    build_retry_message,
    build_text_user_prompt,
    parse_payload,
)

MAX_DOC_HTML_CHARS = 20000  # 与 pdf_text_channel.MAX_PDF_TEXT_CHARS 一致
TEXTUTIL_TIMEOUT = 60  # 秒


def _find_textutil() -> str | None:
    """textutil 为 macOS 自带工具，非 macOS 返回 None。"""
    return shutil.which("textutil")


def doc_to_html(file_path: str) -> str:
    """用 textutil 把 doc/docx 转成精简 HTML（仅 body、去 class 属性）。

    失败（textutil 不存在、转换报错、输出为空）抛 UnsupportedFileError。
    """
    textutil = _find_textutil()
    if not textutil:
        raise UnsupportedFileError(f"textutil 不可用（非 macOS？）: {file_path}")
    try:
        proc = subprocess.run(
            [textutil, "-convert", "html", "-stdout", file_path],
            check=True,
            capture_output=True,
            timeout=TEXTUTIL_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        raise UnsupportedFileError(f"textutil 转换失败 {file_path}: {e}") from e
    html = proc.stdout.decode("utf-8", errors="replace")
    # 裁掉 <head>（含 CSS 样板），只保留 body
    m = re.search(r"<body[^>]*>", html)
    if m:
        html = html[m.start():]
    # 去 class 属性（Cocoa 输出每个 <p>/<td> 都带，占体积且无语义价值）
    html = re.sub(r'\s+class="[^"]*"', "", html)
    if not html.strip():
        raise UnsupportedFileError(f"textutil 输出为空: {file_path}")
    if len(html) > MAX_DOC_HTML_CHARS:
        html = html[:MAX_DOC_HTML_CHARS] + "\n\n[... 内容过长已截断 ...]"
    return html


def extract_doc(file_path: str) -> ChannelResult:
    """提取单个 doc/docx 文件（textutil → HTML → 文本大模型），JSON 解析失败最多重试 2 次。"""
    result = ChannelResult()
    html = doc_to_html(file_path)  # 可能抛 UnsupportedFileError

    source_name = Path(file_path).name
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_text_user_prompt(html, source_name)},
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
            result.items = payload.items
            return result
        except JsonParseError as e:
            result.json_parse_failures += 1
            if attempt >= 2:
                result.error = f"JSON 解析重试 2 次后仍失败: {e}"
                return result
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": build_retry_message(raw, str(e))},
            ]
    return result
