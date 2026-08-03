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

import logging
import os
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

logger = logging.getLogger(__name__)


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
    """检测 LibreOffice 是否可用（macOS / Windows / Linux 三平台）。

    优先级：SOFFICE_PATH 环境变量 > PATH 搜索 > 各平台默认安装路径兜底。
    Windows 上 soffice.com 是 soffice.exe 的控制台附着变体，headless 调用
    更稳定（stdout/stderr 可正常回传），故同一目录下优先匹配 soffice.com。
    """
    # 环境变量覆盖（最高优先级，用于自定义安装目录 / 便携版 / 包管理器安装）
    env_path = os.environ.get("SOFFICE_PATH", "").strip()
    if env_path:
        if Path(env_path).exists():
            return env_path
        logger.warning("[doc通道] SOFFICE_PATH 指向的文件不存在：%s，继续自动探测",
                       env_path)
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    # 各平台默认安装路径兜底（未加 PATH 的场景）
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
        # Windows 官方安装包 / Chocolatey（均装到 Program Files）
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files\LibreOffice\program\soffice.exe",     # Windows 64 位
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",  # Windows 32 位
        # Windows Scoop 安装（当前用户目录下）
        str(Path.home() / "scoop" / "apps" / "libreoffice" / "current" / "program" / "soffice.com"),
        str(Path.home() / "scoop" / "apps" / "libreoffice" / "current" / "program" / "soffice.exe"),
        "/usr/bin/soffice",                                      # Linux
        "/usr/lib/libreoffice/program/soffice",                  # Linux 部分发行版
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _convert_doc_to_pdf(doc_path: str, out_dir: str) -> str | None:
    """用 soffice 把 doc/docx 转成 PDF，失败返回 None（契约不变）。

    - 通过 -env:UserInstallation 指向 out_dir 下的独立临时 profile，
      避免与用户正在运行的 LibreOffice 实例 / profile 残留锁冲突
      （Windows 上常见的转换静默失败原因；临时目录随 out_dir 一并清理）；
    - 不读取/依赖 soffice 的 stdout 内容，只依赖 check=True 与输出文件
      存在性，中文 Windows 控制台编码（GBK/cp936）不影响流程；
    - 失败时记日志说明具体原因（soffice 不存在 / 调用了但失败 / 未产出 PDF），
      便于区分排障；返回 None 后由上层回退其他通道。
    """
    soffice = _find_soffice()
    if not soffice:
        logger.info("[doc通道] 未检测到 LibreOffice(soffice)，跳过 PDF 转换：%s",
                    doc_path)
        return None
    # 独立 user profile（as_uri 生成 file:/// URL，Windows 下为正斜杠 file:///C:/...）
    profile_dir = Path(out_dir) / "lo_user_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", out_dir,
                doc_path,
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        # stderr 仅用于排障，尽力解码（Windows 上可能是 GBK）
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.error("[doc通道] soffice 转换失败（退出码 %s）%s：%s",
                     e.returncode, doc_path, stderr[:300])
        return None
    except Exception as e:  # noqa: BLE001 - 超时/权限等，记录原因后回退其他通道
        logger.exception("[doc通道] soffice 调用异常 %s", doc_path)
        return None
    pdf = Path(out_dir) / (Path(doc_path).stem + ".pdf")
    if pdf.exists():
        return str(pdf)
    logger.warning("[doc通道] soffice 执行成功但未产出 PDF：%s", doc_path)
    return None


def convert_excel_to_pdf(excel_path: str, out_dir: str) -> str | None:
    """用 soffice 把 xls/xlsx/xlsm 转成 PDF（审核页原格式显示用），失败返回 None。

    与 _convert_doc_to_pdf 同一套约定：独立临时 user profile、不依赖 stdout、
    check=True + 输出文件存在性判定、subprocess 超时 180s、Windows 兼容
    （shell=False + pathlib）。

    优先尝试 SinglePageSheets 过滤参数（LibreOffice 7.2+）：每个 sheet 渲染成
    一整页（页面尺寸自适应表格内容，不跨页截断），最适合审核对照；老版本
    LibreOffice 不认识该参数导致转换失败/未产出时，回退普通 pdf 转换
    （按打印设置分页）。soffice 不可用或两次尝试均失败返回 None，由上层
    回退 HTML 快照。
    """
    soffice = _find_soffice()
    if not soffice:
        logger.info("[excel转PDF] 未检测到 LibreOffice(soffice)，跳过 PDF 转换：%s",
                    excel_path)
        return None
    profile_dir = Path(out_dir) / "lo_user_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    expect = Path(out_dir) / (Path(excel_path).stem + ".pdf")
    # 依次尝试：单页 sheet（7.2+）→ 普通分页 PDF
    filters = [
        'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}',
        "pdf",
    ]
    for i, pdf_filter in enumerate(filters):
        if i > 0:
            logger.info("[excel转PDF] SinglePageSheets 不可用，回退普通分页转换：%s",
                        excel_path)
            if expect.exists():
                expect.unlink()  # 清掉上一次失败可能留下的残件
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                    "--headless",
                    "--convert-to", pdf_filter,
                    "--outdir", out_dir,
                    excel_path,
                ],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as e:
            # stderr 仅用于排障，尽力解码（Windows 上可能是 GBK）
            stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
            logger.warning("[excel转PDF] soffice 转换失败（退出码 %s，过滤器 %s）%s：%s",
                           e.returncode, pdf_filter, excel_path, stderr[:300])
            continue
        except Exception:  # noqa: BLE001 - 超时/权限等，记录后回退
            logger.exception("[excel转PDF] soffice 调用异常 %s", excel_path)
            return None  # 超时/权限类异常重试无意义，直接交上层回退
        if expect.exists():
            return str(expect)
        logger.warning("[excel转PDF] soffice 执行成功但未产出 PDF（过滤器 %s）：%s",
                       pdf_filter, excel_path)
    return None


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
