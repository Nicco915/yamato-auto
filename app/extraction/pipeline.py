# -*- coding: utf-8 -*-
"""提取管线：对外唯一入口 extract_folder(folder_path)。

按文件类型三路路由：
- .xlsx/.xlsm/.xls/.csv → excel_channel（文本降维 + 文本大模型）
- .pdf                  → 先探测文本层：有文本层走 pdf_text_channel（快速路，
                          成本约为视觉通道 1/10）；无文本层（扫描件）回退视觉通道
- .jpg/.jpeg/.png       → vision_channel（渲染图片 + 视觉大模型）
- .doc/.docx            → 尝试 soffice(LibreOffice) 转 PDF 后按 PDF 规则路由；
                          soffice 不可用时回退 macOS textutil 转 HTML 走文本通道
                          （doc_channel）；两者皆不可用才记入 unsupported_files
- 其他                  → unsupported_files

返回 ExtractionReport：本身是 list[dict]（每个元素为一个 SKU 的提取结果，
均带 source_file），并带有附加属性：
- .unsupported_files: list[str]  不支持的文件
- .file_errors: dict[str, str]   处理失败的文件及原因
- .stats: dict                   JSON 解析成功率、token 用量等统计
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import llm_client
from .doc_channel import extract_doc
from .excel_channel import (
    EXCEL_SUFFIXES,
    ChannelResult,
    UnsupportedFileError,
    extract_excel,
)
from .pdf_text_channel import extract_pdf_text, pdf_to_text
from .schemas import ExtractedItem
from .vision_channel import IMAGE_SUFFIXES, PDF_SUFFIXES, extract_vision

DOC_SUFFIXES = {".doc", ".docx"}
# 明显的系统垃圾文件
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}


class ExtractionReport(list):
    """list[dict] + 附加统计属性（保持 extract_folder 签名稳定）。"""

    def __init__(self) -> None:
        super().__init__()
        self.unsupported_files: list[str] = []
        self.file_errors: dict[str, str] = {}
        self.stats: dict = {}

    def to_dict(self) -> dict:
        """需要完整结构（含 unsupported_files）时使用。"""
        return {
            "items": list(self),
            "unsupported_files": self.unsupported_files,
            "file_errors": self.file_errors,
            "stats": self.stats,
        }


def _find_soffice() -> str | None:
    """检测 LibreOffice 是否可用（macOS / Windows / Linux 三平台）。"""
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    # 各平台默认安装路径兜底（未加 PATH 的场景）
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
        r"C:\Program Files\LibreOffice\program\soffice.exe",     # Windows 64 位
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",  # Windows 32 位
        "/usr/bin/soffice",                                      # Linux
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _convert_doc_to_pdf(doc_path: str, out_dir: str) -> str | None:
    """用 soffice 把 doc/docx 转成 PDF，失败返回 None。"""
    soffice = _find_soffice()
    if not soffice:
        return None
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, doc_path],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except Exception:  # noqa: BLE001
        return None
    pdf = Path(out_dir) / (Path(doc_path).stem + ".pdf")
    return str(pdf) if pdf.exists() else None


def _iter_files(folder_path: str) -> list[Path]:
    """递归收集工厂文件夹下的文件（跳过隐藏文件与系统文件）。"""
    root = Path(folder_path)
    files = [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".") and p.name not in IGNORE_NAMES
    ]
    return files


def _extract_pdf(pdf_path: str, source_file: str | None = None) -> ChannelResult:
    """按文本层探测结果路由 PDF：有文本层走文本快速路，扫描件走视觉通道。

    source_file: 原始来源文件（doc 转换场景下标注回原始 doc 路径）。
    """
    _, has_text = pdf_to_text(pdf_path)
    if has_text:
        res = extract_pdf_text(pdf_path)
    else:
        res = extract_vision(pdf_path)
    if source_file and source_file != pdf_path:
        for item in res.items:
            item.source_file = source_file
    return res


def extract_folder(folder_path: str) -> ExtractionReport:
    """提取一个工厂文件夹下的所有单据。

    返回 ExtractionReport（list[dict]，每个 dict 是一条 ExtractedItem，
    附加属性 .unsupported_files / .file_errors / .stats）。
    """
    report = ExtractionReport()
    json_attempts = 0
    json_parse_failures = 0

    llm_client.usage_tracker.reset()

    for path in _iter_files(folder_path):
        suffix = path.suffix.lower()
        fpath = str(path)
        try:
            if suffix in EXCEL_SUFFIXES:
                res: ChannelResult = extract_excel(fpath)
            elif suffix in PDF_SUFFIXES:
                res = _extract_pdf(fpath)
            elif suffix in IMAGE_SUFFIXES:
                res = extract_vision(fpath)
            elif suffix in DOC_SUFFIXES:
                with tempfile.TemporaryDirectory() as tmp:
                    pdf = _convert_doc_to_pdf(fpath, tmp)
                    if pdf is not None:
                        # doc 转出的 PDF 一般自带文本层，会优先走文本快速路；
                        # 来源文件标注回原始 doc 路径
                        res = _extract_pdf(pdf, source_file=fpath)
                    else:
                        # soffice 不可用：macOS 兜底 textutil 转 HTML 走文本通道
                        # （textutil 也不可用/转换失败时抛 UnsupportedFileError，
                        # 由下方统一记入 unsupported_files）
                        res = extract_doc(fpath)
            else:
                report.unsupported_files.append(fpath)
                continue
        except UnsupportedFileError as e:
            report.unsupported_files.append(fpath)
            report.file_errors[fpath] = str(e)
            continue
        except Exception as e:  # noqa: BLE001 - 单文件失败不中断整批
            report.file_errors[fpath] = f"{type(e).__name__}: {str(e)[:300]}"
            continue

        json_attempts += res.json_attempts
        json_parse_failures += res.json_parse_failures
        if res.error:
            report.file_errors[fpath] = res.error
        for item in res.items:
            report.append(item.model_dump())

    usage = llm_client.usage_tracker.summary()
    report.stats = {
        "files_seen": len(_iter_files(folder_path)),
        "files_extracted": len(
            [p for p in _iter_files(folder_path) if str(p) not in report.file_errors and str(p) not in report.unsupported_files]
        ),
        "json_attempts": json_attempts,
        "json_parse_failures": json_parse_failures,
        "json_parse_success_rate": (
            round((json_attempts - json_parse_failures) / json_attempts, 4)
            if json_attempts
            else None
        ),
        "token_usage": usage,
    }
    return report


__all__ = ["extract_folder", "ExtractionReport", "ExtractedItem"]
