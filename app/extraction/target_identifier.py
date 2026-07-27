# -*- coding: utf-8 -*-
"""目标识别器：从工厂文件夹中找出真正的 SKU 级箱单（Packing List）。

设计依据（2026-07-27 人工逐个核对 10 个工厂后沉淀的规律，见 PROGRESS.md 第 5 节）：
1. **不能按文件名/扩展名路由**——「报关」文件多为品类汇总版（无条码），
   益尚的「箱单」PDF 也是汇总版；必须按**内容特征**判定。
2. 内容特征 = 同时满足：有 8–14 位数字条码 + 有净重信号 + 有毛重信号 + 有件数信号。
   （买家采购订单 PO 有条码和件数但没有 SKU 级净重 → 排除；
     发票有条码但没有重量 → 排除；报关汇总版无条码 → 排除。）
3. 去重：条码集合是另一候选的**真子集** → 丢弃（「总」/请款全量版 vs 按日期/分票拆分版）；
   条码集合**相同** → 按文件名信号取代表（请款 > 总 > 清关 > 装箱/箱单/packing > 其他），
   正达的两个送仓文件与请款用文件内容相同即属此例。
4. 条码集合互不相同的多个文件全部保留（贝来/益尚按票分文件，亿钻 doc+pdf 互补）。

全程纯 Python 不调 LLM；图片（jpg/png）无文本层无法扫描，本版不处理
（本批 10 工厂无一家以图片为箱单载体，视觉通道仅作 pipeline 兜底）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .doc_channel import doc_to_html
from .excel_channel import UnsupportedFileError, excel_to_markdown
from .pdf_text_channel import pdf_to_text

# 8–14 位数字条码（JAN/货号）
_BARCODE_RE = re.compile(r"\b\d{8,14}\b")
# 内容信号（均为关键词级别，不解析数值）
_NET_RE = re.compile(r"(?i)net\s*weight|n\.?w\.?|净重")
_GROSS_RE = re.compile(r"(?i)gross\s*weight|g\.?w\.?|毛重")
_QTY_RE = re.compile(r"(?i)ctns|cartons?|packages|件数|箱数")
# 文件名偏好（打分用，仅用于同集合/子集去重时的代表选择，不参与候选判定）
_NAME_SIGNALS = ["请款", "总", "清关", "装箱", "箱单", "packing", "pl"]


@dataclass
class FileProfile:
    """单个文件的内容画像。"""

    path: str
    channel: str  # excel / pdf_text / doc / image / unsupported
    barcodes: set[str] = field(default_factory=set)
    has_net: bool = False
    has_gross: bool = False
    has_qty: bool = False
    error: str = ""

    @property
    def is_candidate(self) -> bool:
        return bool(self.barcodes) and self.has_net and self.has_gross and self.has_qty


def _scan_text(text: str) -> tuple[set[str], bool, bool, bool]:
    return (
        set(_BARCODE_RE.findall(text)),
        bool(_NET_RE.search(text)),
        bool(_GROSS_RE.search(text)),
        bool(_QTY_RE.search(text)),
    )


def scan_file(file_path: str) -> FileProfile:
    """扫描单个文件，产出内容画像（不调 LLM）。"""
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm", ".xls", ".csv"):
            # excel_to_markdown 已做 sheet 级箱单筛选，扫其输出即可
            text = excel_to_markdown(file_path)
            channel = "excel"
        elif suffix == ".pdf":
            text, has_text = pdf_to_text(file_path)
            if not has_text:
                return FileProfile(path=file_path, channel="image_pdf",
                                   error="无文本层（扫描件），目标识别器无法扫描")
            channel = "pdf_text"
        elif suffix in (".doc", ".docx"):
            html = doc_to_html(file_path)
            text = re.sub(r"<[^>]+>", " ", html)
            channel = "doc"
        elif suffix in (".jpg", ".jpeg", ".png"):
            return FileProfile(path=file_path, channel="image",
                               error="图片无文本层，目标识别器无法扫描")
        else:
            return FileProfile(path=file_path, channel="unsupported",
                               error=f"不支持的类型: {suffix}")
    except UnsupportedFileError as e:
        return FileProfile(path=file_path, channel="unsupported", error=str(e))
    except Exception as e:  # noqa: BLE001 - 单文件失败不中断整批
        return FileProfile(path=file_path, channel="unsupported",
                           error=f"{type(e).__name__}: {e}")

    barcodes, has_net, has_gross, has_qty = _scan_text(text)
    return FileProfile(path=file_path, channel=channel, barcodes=barcodes,
                       has_net=has_net, has_gross=has_gross, has_qty=has_qty)


def _name_score(path: str) -> int:
    """文件名信号分（仅在候选间去重时使用）：请款/总/清关/箱单类优先。"""
    name = Path(path).name.lower()
    for i, sig in enumerate(_NAME_SIGNALS):
        if sig in name:
            return len(_NAME_SIGNALS) - i
    return 0


def identify_targets(folder_path: str) -> list[FileProfile]:
    """从工厂文件夹中识别出 SKU 级箱单文件列表（去重后）。

    返回 FileProfile 列表（仅候选文件），按文件名信号分降序。
    """
    root = Path(folder_path)
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".DS_Store")
    profiles = [scan_file(str(p)) for p in files]
    candidates = [p for p in profiles if p.is_candidate]

    kept: list[FileProfile] = []
    for prof in sorted(candidates, key=lambda p: (-_name_score(p.path), p.path)):
        # 真子集：已被某个保留候选覆盖 → 丢弃
        if any(prof.barcodes < k.barcodes for k in kept):
            continue
        # 同集合：已有同分或更高分代表 → 丢弃
        if any(prof.barcodes == k.barcodes for k in kept):
            continue
        # 当前候选是某些已保留项的超集 → 替换掉它们
        kept = [k for k in kept if not k.barcodes < prof.barcodes]
        kept.append(prof)
    return sorted(kept, key=lambda p: p.path)


def scan_folder(folder_path: str) -> list[FileProfile]:
    """扫描文件夹内所有文件（含非候选），供调试/审核界面展示判定依据。"""
    root = Path(folder_path)
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != ".DS_Store")
    return [scan_file(str(p)) for p in files]
