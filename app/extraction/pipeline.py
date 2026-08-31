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
# openpyxl 能改写的格式（xlsx / xlsm）；legacy .xls 先经 soffice 转成 xlsx
# 副本再进同一预处理管线（见 convert_excel_to_pdf）
_OPENPYXL_SUFFIXES = {".xlsx", ".xlsm"}
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


def _reset_topleft_for_soffice(src: Path, work_dir: Path) -> Path | None:
    """把 xlsx/xlsm 复制到 work_dir 下并重置每个 sheet 的 topLeftCell/selection，
    返回预处理后副本路径；无法处理（如 .xls、openpyxl 打开失败、文件无 sheetView）
    时返回 None（调用方应回退原始源文件继续转换）。

    为什么不重置源文件：缓存键 = (源路径, mtime)，修改源文件会污染键判定；
    为什么不就地预处理：源文件是工厂交付的原始单据，必须保留只读语义。
    LibreOffice SinglePageSheets 以 topLeftCell 为页面锚点，未重置时若上次
    保存停留在 D15/AC30 之类位置，左上整片会被裁掉（JAN CODE / 品名列消失），
    兆丰 XD-261830-001-1.26/1.28 都踩过此坑。
    """
    if src.suffix.lower() not in _OPENPYXL_SUFFIXES:
        return None  # .xls：openpyxl 不能写，跳过预处理
    preprocess_dir = work_dir / "_lo_preprocess"
    preprocess_dir.mkdir(parents=True, exist_ok=True)
    # work_dir 通常已经 mkdtemp 在系统临时区，但保险起见用 _lo_preprocess/ 隔离
    tmp_xlsx = preprocess_dir / src.name
    try:
        shutil.copy2(src, tmp_xlsx)
        # 必须用 keep_vba=True：xlsm 含宏，丢宏会在 LibreOffice 里加载失败
        from openpyxl import load_workbook
        wb = load_workbook(tmp_xlsx, keep_vba=(src.suffix.lower() == ".xlsm"))
        changed = False
        for ws in wb.worksheets:
            sv = ws.sheet_view
            if sv.topLeftCell != "A1":
                sv.topLeftCell = "A1"
                changed = True
            # selection 是 list；选区（activeCell/sqref 偏离 A1）会让
            # SinglePageSheets 进一步锚到该区域，直接清成 [] 在 save 时
            # 会被 openpyxl 重建为默认 A1；为保险起见显式置 A1
            if sv.selection:
                for sel in sv.selection:
                    if sel.activeCell != "A1":
                        sel.activeCell = "A1"
                        changed = True
                    if sel.sqref != "A1":
                        sel.sqref = "A1"
                        changed = True
        if changed:
            wb.save(tmp_xlsx)
    except Exception:  # noqa: BLE001 - 任何 openpyxl 异常都回退源文件
        logger.warning("[excel转PDF] topLeftCell 预处理失败，回退原始源文件：%s",
                       src, exc_info=True)
        try:
            if tmp_xlsx.exists():
                tmp_xlsx.unlink()
        except OSError:
            pass
        return None
    return str(tmp_xlsx)


def _convert_xls_to_xlsx(soffice: str, profile_arg: str,
                         src: Path, work_dir: Path) -> Path | None:
    """.xls 先经 soffice 转一份 .xlsx 副本（work_dir/_xls_conv/ 下，不动源文件），
    成功返回副本路径，失败返回 None（调用方回退原始 .xls 走普通分页）。

    为什么要先转 xlsx：SinglePageSheets 能把每个 sheet 收成单页，避免老式 .xls
    按打印设置把一个 sheet 拆成多页（审核左屏翻页割裂，TOP 请款资料已复现）；
    但 SinglePageSheets 以 topLeftCell 为页面锚点，必须先经 openpyxl 重置——
    openpyxl 只能写 xlsx/xlsm，所以 .xls 先做一次格式转换再走统一管线。
    转换出的副本与源文件同名（仅后缀不同），保证下游 PDF 产物命名不变。
    """
    conv_dir = work_dir / "_xls_conv"
    conv_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                soffice,
                profile_arg,
                "--headless",
                "--convert-to", "xlsx",
                "--outdir", str(conv_dir),
                str(src),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        # stderr 仅用于排障，尽力解码（Windows 上可能是 GBK）
        stderr = (e.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.warning("[excel转PDF] .xls→.xlsx 预转换失败（退出码 %s）%s：%s",
                       e.returncode, src, stderr[:300])
        return None
    except Exception:  # noqa: BLE001 - 超时/权限等，记录后回退原始 .xls
        logger.exception("[excel转PDF] .xls→.xlsx 预转换调用异常 %s", src)
        return None
    out = conv_dir / (src.stem + ".xlsx")
    if out.exists():
        return out
    logger.warning("[excel转PDF] .xls→.xlsx 预转换成功但未产出文件：%s", src)
    return None


def convert_excel_to_pdf(excel_path: str, out_dir: str) -> str | None:
    """用 soffice 把 xls/xlsx/xlsm 转成 PDF（审核页原格式显示用），失败返回 None。

    与 _convert_doc_to_pdf 同一套约定：独立临时 user profile、不依赖 stdout、
    check=True + 输出文件存在性判定、subprocess 超时 180s、Windows 兼容
    （shell=False + pathlib）。

    渲染策略：
    - .xls：先用 soffice 转成 .xlsx 副本（out_dir/_xls_conv/ 下，源文件只读不动），
      之后与 xlsx 同管线（topLeftCell 预处理 + SinglePageSheets 单页/sheet）；
      预转换失败才回退原始 .xls 走普通分页（多页但完整，审核页已有翻页器）。
    - .xlsx/.xlsm：先复制到 out_dir/_lo_preprocess/，用 openpyxl 重置每个 sheet 的
      topLeftCell="A1" + 清空 selection（防 LibreOffice SinglePageSheets 以视图
      停留点为锚点把左上整片裁掉——兆丰 Packing 已复现），再用预处理副本走
      SinglePageSheets；预处理失败（openpyxl 异常/无 sheetView）回退原始源
      文件继续。

    SinglePageSheets 不可用时回退普通 pdf 转换（按打印设置分页）。soffice 不可用
    或两次尝试均失败返回 None，由上层回退 HTML 快照。
    """
    soffice = _find_soffice()
    if not soffice:
        logger.info("[excel转PDF] 未检测到 LibreOffice(soffice)，跳过 PDF 转换：%s",
                    excel_path)
        return None
    profile_dir = Path(out_dir) / "lo_user_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_arg = f"-env:UserInstallation={profile_dir.resolve().as_uri()}"
    # 选送 soffice 的实际源文件：.xls 先转 xlsx 副本；xlsx/xlsm 走预处理副本
    src = Path(excel_path)
    work_dir = Path(out_dir)
    effective_src = src
    if src.suffix.lower() == ".xls":
        converted = _convert_xls_to_xlsx(soffice, profile_arg, src, work_dir)
        if converted is not None:
            effective_src = converted
    preprocessed = _reset_topleft_for_soffice(effective_src, work_dir)
    soffice_input = preprocessed if preprocessed else str(effective_src)
    # 产物名取源文件 stem（.xls 预转换副本同名，命名不变）
    expect = Path(out_dir) / (src.stem + ".pdf")
    # effective_src 仍是 .xls 说明预转换失败：SinglePageSheets 在未重置
    # topLeftCell 的 .xls 上会按视图停留点裁切，只能走普通分页
    if effective_src.suffix.lower() == ".xls":
        filters = ["pdf"]
    else:
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
                    soffice_input,
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


def _extract_pdf(pdf_path: str, source_file: str | None = None,
                 pages: list[int] | None = None,
                 force_vision: bool = False) -> ChannelResult:
    """按文本层探测结果路由 PDF：有文本层走文本快速路，扫描件走视觉通道。

    source_file: 原始来源文件（doc 转换场景下标注回原始 doc 路径）。
    pages: 可选，指定要提取的 1-based 页码列表；非空时一律走视觉通道。
    force_vision: 可选，True 时跳过文本层探测直接走视觉通道。
    """
    if force_vision or pages:
        # 显式指定页码或强制视觉 →直接走视觉，跳过 has_text 探测
        res = extract_vision(pdf_path, pages=pages)
    else:
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
