# -*- coding: utf-8 -*-
"""通道二：文本降维大模型通道（Excel / CSV）。

流程（第二阶段设计文档第 2 节）：
1. openpyxl 拆分合并单元格（把合并值填到每个单元格），不执行任何业务逻辑抓取；
2. sheet 级目标识别：只把箱单（Packing List）sheet 转成 Markdown，
   发票/合同/报关 sheet 不送 LLM（见 select_pl_sheets）；
3. 交给文本大模型做语义提取，强制 JSON 输出，解析失败最多重试 2 次。

旧版 .xls 用 pandas(xlrd) 读取；读不出的记入 unsupported。
"""
from __future__ import annotations

import io
import logging
import re
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
from .schemas import ExtractedItem, apply_weight_basis
from .verify import verify_weight_basis

# 单个文件转成 Markdown 后的长度上限（控制 token 成本；
# 2026-07-27 由 24000 提高到 100000：正达 INV+PL 两 sheet 合计超限被截断，
# 导致 PL sheet 后半 29 个 SKU 丢失）
MAX_MARKDOWN_CHARS = 100000
# 单表最多保留的行列（防止巨型空表撑爆上下文）
MAX_ROWS = 400
MAX_COLS = 30

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}
# 旧版 .xls 走 pandas/xlrd，不做合并单元格拆分
LEGACY_XLS_SUFFIXES = {".xls"}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sheet 级目标识别（2026-07-27 人工核对引入）
# 箱单（Packing List）是提取的唯一目标 sheet；发票/合同/报关单 sheet 不送 LLM，
# 既省 token 又消除 PCS/单价等干扰数据。
# ---------------------------------------------------------------------------
# 明确的箱单 sheet 名（命中即只取这些 sheet）
PL_SHEET_PATTERNS = [r"(?i)\bPL\b", r"(?i)packing", "箱单", "装箱单"]
# 明确的非箱单 sheet 名（无箱单 sheet 时用于排除，兜底保留其余）
NON_PL_SHEET_PATTERNS = [
    r"(?i)\bINV\b", r"(?i)invoice", "发票",
    r"(?i)contract", "合同", "报关",
]


def select_pl_sheets(names: list[str]) -> list[str]:
    """从 sheet 名列表中选出箱单 sheet。

    规则：有明确箱单 sheet → 只取它们；
    没有 → 排除明确非箱单的，保留其余（如 Sheet1 这类无名 sheet）；
    全被排除 → 原样保留（宁多勿漏）。
    """
    pl = [n for n in names if any(re.search(p, n) for p in PL_SHEET_PATTERNS)]
    if pl:
        return pl
    rest = [n for n in names if not any(re.search(p, n) for p in NON_PL_SHEET_PATTERNS)]
    return rest or list(names)


class UnsupportedFileError(RuntimeError):
    """文件无法被本通道读取。"""


@dataclass
class ChannelResult:
    """单文件提取结果 + 统计信息。"""

    items: list[ExtractedItem] = field(default_factory=list)
    json_attempts: int = 0  # LLM 调用次数（含解析失败重试）
    json_parse_failures: int = 0  # JSON 解析失败次数
    error: str = ""
    notes: list[str] = field(default_factory=list)  # 确定性校验备注（verify.py）


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
    """xlsx/xlsm：openpyxl 拆合并单元格 → 箱单 sheet 转 Markdown。"""
    wb = load_workbook(file_path, data_only=True, read_only=False)
    selected = set(select_pl_sheets([ws.title for ws in wb.worksheets]))
    parts: list[str] = []
    for ws in wb.worksheets:
        if ws.title not in selected:
            continue
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
    selected = set(select_pl_sheets(list(sheets.keys())))
    parts: list[str] = []
    for name, df in sheets.items():
        if name not in selected:
            continue
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

_NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
_BARCODE_RE = re.compile(r"^\d{8,14}$")


def _drop_zero_rows(markdown_text: str) -> str:
    """纯 Python 预过滤全 0 占位行（不送 LLM）。

    判定：某 markdown 行含 8–14 位纯数字条码单元格，且该行其余所有数值
    单元格（QUANTITY/PACKAGES/净重/毛重/体积等）全为 0 → 该行为未发货占位行，
    删除不影响任何真实数据。无条码的行（表头、SUB TOTAL、说明文字）一律保留。
    （2026-07-27 正达案例：PL sheet 约 50 行 0 值占位行使 LLM 输出超 max_tokens
    被截断 / 生成超 120s 超时）
    """
    kept: list[str] = []
    for line in markdown_text.split("\n"):
        if not line.startswith("|"):
            kept.append(line)
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        numerics = [c for c in cells if _NUM_RE.match(c)]
        has_barcode = any(_BARCODE_RE.match(c) for c in numerics)
        others = [c for c in numerics if not _BARCODE_RE.match(c)]
        if has_barcode and others and all(float(c.replace(",", "")) == 0 for c in others):
            continue  # 全 0 占位行，丢弃
        kept.append(line)
    return "\n".join(kept)


def sort_by_source_order(items: list, source_text: str) -> list:
    """按 sku_code 在源文本中首次出现的位置排序（稳定排序）。

    刚性保证输出顺序与单据行序一致（人工核对习惯）——LLM 的 JSON 数组
    顺序只是"恰好"按读入顺序，没有契约保证。找不到条码的项（sku_code
    为空或文本中不存在）保持原相对顺序排到末尾。
    （pdf_text_channel 亦复用本函数；vision 通道无文本层，无法定位，不排。）
    """
    def key(it) -> tuple:
        code = (getattr(it, "sku_code", None) or "").strip()
        pos = source_text.find(code) if code else -1
        return (pos < 0, pos)

    return sorted(items, key=key)


def extract_excel(file_path: str) -> ChannelResult:
    """提取单个 Excel/CSV 文件，返回结构化结果。"""
    result = ChannelResult()
    markdown_text = _drop_zero_rows(excel_to_markdown(file_path))  # 可能抛 UnsupportedFileError
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
            verified, notes = verify_weight_basis(payload.items, markdown_text)
            result.notes.extend(notes)
            result.items = apply_weight_basis(sort_by_source_order(verified, markdown_text))
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
