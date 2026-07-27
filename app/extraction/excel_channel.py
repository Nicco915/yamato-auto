# -*- coding: utf-8 -*-
"""通道二：文本降维大模型通道（Excel / CSV）。

流程（第二阶段设计文档第 2 节）：
1. openpyxl 拆分合并单元格（把合并值填到每个单元格），不执行任何业务逻辑抓取；
2. pandas 把有效表格区域转成 Markdown 表格纯文本；
3. 交给文本大模型做语义提取，强制 JSON 输出，解析失败最多重试 2 次。

旧版 .xls 用 pandas(xlrd) 读取；读不出的记入 unsupported。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from . import llm_client
from .prompts import (
    SYSTEM_PROMPT,
    JsonParseError,
    build_retry_message,
    build_text_user_prompt,
    parse_payload,
)
from .schemas import ExtractedItem

# 单个文件转成 Markdown 后的长度上限（控制 token 成本）
MAX_MARKDOWN_CHARS = 24000
# 单表最多保留的行列（防止巨型空表撑爆上下文）
MAX_ROWS = 400
MAX_COLS = 30

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}
# 旧版 .xls 走 pandas/xlrd，不做合并单元格拆分
LEGACY_XLS_SUFFIXES = {".xls"}


class UnsupportedFileError(RuntimeError):
    """文件无法被本通道读取。"""


@dataclass
class ChannelResult:
    """单文件提取结果 + 统计信息。"""

    items: list[ExtractedItem] = field(default_factory=list)
    json_attempts: int = 0  # LLM 调用次数（含解析失败重试）
    json_parse_failures: int = 0  # JSON 解析失败次数
    error: str = ""


# ---------------------------------------------------------------------------
# 第一步：纯格式降维（不调 LLM，可独立测试）
# ---------------------------------------------------------------------------

def _unmerge_sheet_to_grid(ws) -> list[list]:
    """把一个 worksheet 拆平为二维网格：合并单元格的值填充到所有覆盖单元格。"""
    merged_lookup: dict[tuple[int, int], object] = {}
    for rng in ws.merged_cells.ranges:
        top_value = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                merged_lookup[(r, c)] = top_value

    max_row = min(ws.max_row or 0, MAX_ROWS)
    max_col = min(ws.max_column or 0, MAX_COLS)
    grid: list[list] = []
    for r in range(1, max_row + 1):
        row = []
        for c in range(1, max_col + 1):
            if (r, c) in merged_lookup:
                row.append(merged_lookup[(r, c)])
            else:
                row.append(ws.cell(row=r, column=c).value)
        grid.append(row)
    return grid


def _grid_to_dataframe(grid: list[list]) -> pd.DataFrame:
    """网格 → DataFrame，裁掉全空的首尾行列。"""
    df = pd.DataFrame(grid)
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    # 去掉纯空白字符串行/列
    if not df.empty:
        non_blank = df.apply(lambda col: col.map(lambda v: str(v).strip() if v is not None else ""))
        df = df.loc[(non_blank != "").any(axis=1), (non_blank != "").any(axis=0)]
    return df.reset_index(drop=True)


def _df_to_markdown(df: pd.DataFrame) -> str:
    """DataFrame → Markdown 表格纯文本（无表头概念，全部按数据行处理）。"""
    if df.empty:
        return ""
    text_df = df.map(lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).replace("\n", " ").replace("|", "｜").strip())
    buf = io.StringIO()
    ncols = len(text_df.columns)
    buf.write("| " + " | ".join(f"c{i}" for i in range(ncols)) + " |\n")
    buf.write("| " + " | ".join("---" for _ in range(ncols)) + " |\n")
    for _, row in text_df.iterrows():
        buf.write("| " + " | ".join(row.tolist()) + " |\n")
    return buf.getvalue()


def xlsx_to_markdown(file_path: str) -> str:
    """xlsx/xlsm：openpyxl 拆合并单元格 → 每个 sheet 转 Markdown。"""
    wb = load_workbook(file_path, data_only=True, read_only=False)
    parts: list[str] = []
    for ws in wb.worksheets:
        grid = _unmerge_sheet_to_grid(ws)
        df = _grid_to_dataframe(grid)
        md = _df_to_markdown(df)
        if md:
            parts.append(f"### 工作表: {ws.title}\n{md}")
    return "\n\n".join(parts)


def legacy_xls_to_markdown(file_path: str) -> str:
    """旧版 .xls：pandas/xlrd 逐 sheet 读入 → Markdown（无法拆合并单元格，靠前向填充缓解）。"""
    try:
        sheets = pd.read_excel(file_path, sheet_name=None, header=None, engine="xlrd")
    except Exception as e:  # noqa: BLE001
        raise UnsupportedFileError(f"xlrd 无法读取 {file_path}: {e}") from e
    parts: list[str] = []
    for name, df in sheets.items():
        df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
        # 合并单元格在 xlrd 中表现为只有左上角有值，做有限的前向填充（仅行内向右填）
        df = df.ffill(axis=1)
        md = _df_to_markdown(df.reset_index(drop=True))
        if md:
            parts.append(f"### 工作表: {name}\n{md}")
    return "\n\n".join(parts)


def csv_to_markdown(file_path: str) -> str:
    """CSV：尝试常见中文编码读入 → Markdown。"""
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "shift_jis"):
        try:
            df = pd.read_csv(file_path, header=None, encoding=enc)
            return _df_to_markdown(df.head(MAX_ROWS))
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise UnsupportedFileError(f"CSV 无法解码 {file_path}: {last_err}")


def excel_to_markdown(file_path: str) -> str:
    """对外公开：把 Excel/CSV 降维为 Markdown 纯文本（不调用 LLM）。"""
    suffix = Path(file_path).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        md = xlsx_to_markdown(file_path)
    elif suffix in LEGACY_XLS_SUFFIXES:
        md = legacy_xls_to_markdown(file_path)
    elif suffix == ".csv":
        md = csv_to_markdown(file_path)
    else:
        raise UnsupportedFileError(f"不是表格文件: {file_path}")
    if not md.strip():
        raise UnsupportedFileError(f"文件无有效表格内容: {file_path}")
    if len(md) > MAX_MARKDOWN_CHARS:
        md = md[:MAX_MARKDOWN_CHARS] + "\n\n[... 内容过长已截断 ...]"
    return md


# ---------------------------------------------------------------------------
# 第二步：文本大模型语义提取
# ---------------------------------------------------------------------------

def extract_excel(file_path: str) -> ChannelResult:
    """提取单个 Excel/CSV 文件，返回结构化结果。"""
    result = ChannelResult()
    markdown_text = excel_to_markdown(file_path)  # 可能抛 UnsupportedFileError
    source_name = Path(file_path).name

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_text_user_prompt(markdown_text, source_name)},
    ]

    # JSON 解析失败最多重试 2 次（即最多 3 次调用）
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
