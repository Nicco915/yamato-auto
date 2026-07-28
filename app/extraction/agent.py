# -*- coding: utf-8 -*-
"""提取 Agent（编排层）：目标识别 → 通道路由 → 合并去重 → 工厂级报告 + 结构化反馈。

三层架构（见《提取agent如何识别文件.md》）：
  ① 目标识别器 target_identifier（纯规则，10/10 工厂验证）
  ② 四通道提取工具（excel / pdf_text+vision 回退 / doc / vision，146 SKU 人工核对 100%）
  ③ 本模块：薄编排，不调 LLM 做决策

反馈义务（零容错：宁停勿猜）——以下情况必须在 AgentReport.issues 中直接反馈，
绝不静默继续或"凑合"用非箱单文件：
- BLOCKING 找不到箱单：文件夹内没有任何 SKU 级箱单候选 → 需人工指定文件
- BLOCKING 目标文件提取结果为空：可能误识别，需人工确认
- WARNING 特殊文件类型无工具可调取（如 zip/txt/未知扩展名）
- WARNING 疑似图片箱单：文件夹只有图片（识别器无法扫描图片内容）
- WARNING 同一 SKU 在多个目标文件中数值冲突
- WARNING 通道错误（JSON 解析失败超限、API 异常等）
- INFO     提取项含 needs_human_review（通道级防跌落，正常透传）

尚未覆盖的已知缺口：纯图片箱单（本批 10 工厂未遇到；届时走 vision 通道+
人工确认，识别器无法自动判定图片内容）。
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import llm_client
from .doc_channel import extract_doc
from .excel_channel import (
    EXCEL_SUFFIXES,
    ChannelResult,
    UnsupportedFileError,
    extract_excel,
)
from .pdf_text_channel import pdf_to_text
from .pipeline import _convert_doc_to_pdf, _extract_pdf
from .schemas import ExtractedItem
from .target_identifier import FileProfile, identify_targets, scan_folder
from .vision_channel import IMAGE_SUFFIXES, PDF_SUFFIXES, extract_vision

DOC_SUFFIXES = {".doc", ".docx"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}

# 反馈级别
BLOCKING = "blocking"  # 结果不可信/无法继续，必须人工介入
WARNING = "warning"  # 需要人工过目，但流程已尽量推进
INFO = "info"  # 正常透传信息


@dataclass
class Issue:
    """一条结构化反馈。"""

    level: str  # blocking / warning / info
    type: str  # 机器可读类型，如 NO_PACKING_LIST / UNSUPPORTED_FILE_TYPE ...
    message: str  # 给人看的中文描述
    file: str = ""  # 相关文件（可选）


@dataclass
class AgentReport:
    """工厂级提取结果。"""

    factory_folder: str = ""
    items: list[ExtractedItem] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)  # 实际用于提取的文件
    skipped_files: list[str] = field(default_factory=list)  # 识别为非目标的文件
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def has_blocking(self) -> bool:
        return any(i.level == BLOCKING for i in self.issues)

    def add(self, level: str, type_: str, message: str, file: str = "") -> None:
        self.issues.append(Issue(level=level, type=type_, message=message, file=file))


def _route_extract(fpath: str) -> ChannelResult:
    """按文件类型路由到对应通道（与 pipeline.extract_folder 同一套规则）。"""
    suffix = Path(fpath).suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        return extract_excel(fpath)
    if suffix in PDF_SUFFIXES:
        return _extract_pdf(fpath)
    if suffix in IMAGE_SUFFIXES:
        return extract_vision(fpath)
    if suffix in DOC_SUFFIXES:
        with tempfile.TemporaryDirectory() as tmp:
            pdf = _convert_doc_to_pdf(fpath, tmp)
            if pdf is not None:
                return _extract_pdf(pdf, source_file=fpath)
            return extract_doc(fpath)
    raise UnsupportedFileError(f"无可用通道的文件类型: {fpath}")


def _merge_items(report: AgentReport, new_items: list[ExtractedItem], from_file: str) -> None:
    """按 sku_code 合并多文件提取结果。

    - 同 code 且三要素数值一致 → 去重（保留先到的）；
    - 同 code 但数值冲突 → 两条都保留并置 needs_human_review，发 WARNING 反馈；
    - 无 code 的条目原样保留（下游品名模糊匹配兜底）。
    """
    by_code: dict[str, ExtractedItem] = {
        it.sku_code: it for it in report.items if it.sku_code
    }
    for it in new_items:
        if not it.sku_code:
            report.items.append(it)
            continue
        old = by_code.get(it.sku_code)
        if old is None:
            by_code[it.sku_code] = it
            report.items.append(it)
            continue
        same = (
            old.total_quantity == it.total_quantity
            and old.total_net_weight == it.total_net_weight
            and old.total_gross_weight == it.total_gross_weight
        )
        if not same:
            old.needs_human_review = True
            it.needs_human_review = True
            report.items.append(it)
            report.add(
                WARNING, "SKU_CONFLICT",
                f"SKU {it.sku_code} 在多个文件中数值冲突："
                f"[{old.source_file}] {old.total_quantity}/{old.total_net_weight}/{old.total_gross_weight} vs "
                f"[{from_file}] {it.total_quantity}/{it.total_net_weight}/{it.total_gross_weight}",
                file=from_file,
            )


def extract_factory(folder_path: str) -> AgentReport:
    """提取 Agent 主入口：给定工厂文件夹，产出工厂级提取报告。

    流程：扫描画像 → 识别目标 → 反馈检查 → 逐目标通道提取 → 合并去重 → 报告。
    """
    report = AgentReport(factory_folder=folder_path)
    root = Path(folder_path)
    all_files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name not in IGNORE_NAMES
    )

    # --- 第一步：目标识别（纯规则） ---
    profiles: list[FileProfile] = scan_folder(folder_path)
    targets = identify_targets(folder_path)
    target_paths = {t.path for t in targets}
    report.targets = sorted(target_paths)
    report.skipped_files = [str(p) for p in all_files if str(p) not in target_paths]

    # --- 第二步：反馈检查（先反馈，再决定要不要继续） ---
    scannable = {".xlsx", ".xlsm", ".xls", ".csv", ".pdf", ".doc", ".docx"}
    images = [p for p in all_files if p.suffix.lower() in IMAGE_SUFFIXES]
    unknown = [
        p for p in all_files
        if p.suffix.lower() not in scannable and p.suffix.lower() not in IMAGE_SUFFIXES
    ]
    for p in unknown:
        report.add(
            WARNING, "UNSUPPORTED_FILE_TYPE",
            f"特殊类型文件，现有工具无法调取：{p.name}（{p.suffix}）。"
            "如该文件是箱单，需人工处理或为其扩展新通道。",
            file=str(p),
        )

    if not targets:
        hint = ""
        if images:
            hint = (f"；文件夹内有 {len(images)} 个图片文件，识别器无法扫描图片内容，"
                    "如箱单是图片/扫描件，需人工确认后走视觉通道")
        report.add(
            BLOCKING, "NO_PACKING_LIST",
            f"未找到 SKU 级箱单（13 位条码 + 件数 + 净重 + 毛重四要素）。"
            f"请人工指定箱单文件{hint}。",
        )
        return report  # 找不到箱单：直接反馈，不用非箱单文件凑合

    if images and all(p in report.skipped_files for p in map(str, images)):
        # 有目标后图片默认忽略（已知多为买家 PO/照片），仅 info 记录
        report.add(
            INFO, "IMAGES_IGNORED",
            f"{len(images)} 个图片文件未作为提取目标（识别器无法扫描图片内容，"
            "如其中含箱单请人工指出）。",
        )

    # --- 第三步：逐目标通道提取 ---
    llm_client.usage_tracker.reset()
    json_attempts = json_failures = 0
    for tpath in sorted(target_paths):
        try:
            res = _route_extract(tpath)
        except Exception as e:  # noqa: BLE001 - 单文件失败不中断
            report.add(
                WARNING, "CHANNEL_ERROR",
                f"通道处理失败：{type(e).__name__}: {str(e)[:200]}",
                file=tpath,
            )
            continue
        json_attempts += res.json_attempts
        json_failures += res.json_parse_failures
        for note in getattr(res, "notes", []):
            level = WARNING if "不符" in note and "校正" not in note else INFO
            report.add(level, "VERIFY_NOTE", note, file=tpath)
        if res.error:
            report.add(WARNING, "CHANNEL_ERROR", f"通道报错：{res.error}", file=tpath)
        if not res.items:
            report.add(
                BLOCKING, "TARGET_EMPTY",
                "目标文件提取结果为空——可能是目标误识别或文件内容异常，需人工确认。",
                file=tpath,
            )
            continue
        _merge_items(report, res.items, tpath)

    # --- 第四步：汇总 ---
    review_n = sum(1 for it in report.items if it.needs_human_review)
    if review_n:
        report.add(INFO, "ITEMS_NEED_REVIEW", f"{review_n} 个提取项被通道标记 needs_human_review")
    report.stats = {
        "files_total": len(all_files),
        "targets_used": len(report.targets),
        "items_extracted": len(report.items),
        "json_attempts": json_attempts,
        "json_parse_failures": json_failures,
        "token_usage": llm_client.usage_tracker.summary(),
    }
    return report


def format_report(report: AgentReport) -> str:
    """把 AgentReport 渲染成人读的纯文本摘要（CLI/日志用）。"""
    lines = [f"工厂文件夹: {report.factory_folder}"]
    lines.append(f"提取目标({len(report.targets)}): " + (
        "、".join(Path(t).name for t in report.targets) or "无"))
    lines.append(f"提取 SKU 条目: {len(report.items)}")
    if report.issues:
        lines.append(f"反馈({len(report.issues)}):")
        for i in report.issues:
            tag = {"blocking": "🔴", "warning": "🟡", "info": "ℹ️"}[i.level]
            f = f" [{Path(i.file).name}]" if i.file else ""
            lines.append(f"  {tag} {i.type}: {i.message}{f}")
    else:
        lines.append("反馈: 无")
    return "\n".join(lines)


__all__ = ["extract_factory", "AgentReport", "Issue", "format_report",
           "BLOCKING", "WARNING", "INFO"]
