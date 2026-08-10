# -*- coding: utf-8 -*-
"""提取 Agent 增量会话：单据逐个到达、逐个处理的生产模式。

与 agent.py（整批模式：文件夹到齐后一次性 extract_factory）互补。
核心语义差异（2026-07-28 与用户确认的三条设定）：

1. **按单据逐个处理**：process_file(session, path) 每来一个文件调一次。
   非候选文件（PO/发票/报关汇总…）只登记身份、不调 LLM，零成本。
2. **完整性双轨**：
   - set_expected_skus() 后，每次处理都报告覆盖率（已到 X / 缺 Y，缺哪些）；
   - mark_complete() 人工显式宣告到齐（两者兼容：覆盖率不满也可人工强判，
     覆盖率满了也仍走人工审核界面最终确认）。
3. **负向候选只提示不提取**：只收到报关/汇总类候选时，登记为"暂缓"并提示
   「暂无箱单」（提示一次后静默，直到真箱单到达）；人工可用
   force_extract() 指定任何文件强制提取（"人工告知后找到正确目标"）。

改单语义：同 SKU 新文件数值不同 → 新值覆盖旧值（旧值入 history），
标 needs_human_review 提示人工确认改单。

状态可 JSON 序列化（save/load），落在 app/data/sessions/，重启不丢。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import get_settings
from .agent import _route_extract
from .excel_channel import UnsupportedFileError
from .schemas import ExtractedItem
from .target_identifier import (
    _NEGATIVE_NAME_SIGNALS,
    _name_score,
    scan_file,
)

SESSIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "sessions"


def batch_session_dir(batch_id: str) -> Path:
    """获取某批次专属 session 目录：data/sessions/{safe(batch_id)}/

    调用方按需 mkdir。此函数不创建目录，只返回路径。
    """
    settings = get_settings()
    return SESSIONS_DIR / settings.safe_path_tag(batch_id)


def batch_session_path(batch_id: str, factory_name: str) -> Path:
    """获取某批次某工厂 session.json 路径：data/sessions/{safe(batch_id)}/{factory}.json"""
    return batch_session_dir(batch_id) / f"{factory_name}.json"


# 注意：保留 save/load 与原语义兼容——他们接受 path 参数，调用方按需提供路径

# 会话状态
WAITING_PL = "waiting_pl"  # 暂无箱单
COLLECTING = "collecting"  # 箱单提取中（可能还有票/文件未到）
COMPLETE_AUTO = "complete_auto"  # 覆盖率达到 100%（vs expected_skus）
COMPLETE_MANUAL = "complete_manual"  # 人工宣告到齐


@dataclass
class ProcessResult:
    """process_file 的增量报告。"""

    file: str
    action: str  # extracted / ignored_subset / ignored_duplicate / registered_non_target
    #            / deferred_negative / replaced_target / already_processed / forced
    message: str
    new_skus: list[str] = field(default_factory=list)
    updated_skus: list[str] = field(default_factory=list)  # 改单覆盖
    coverage: dict = field(default_factory=dict)  # extracted/expected/missing/extra
    status: str = WAITING_PL


@dataclass
class FactorySession:
    factory: str
    status: str = WAITING_PL
    items: dict[str, dict] = field(default_factory=dict)  # sku_code → ExtractedItem.model_dump()
    no_code_items: list[dict] = field(default_factory=list)  # 无条码条目
    targets: list[dict] = field(default_factory=list)  # {path, barcodes[], name_score, forced}
    deferred: list[dict] = field(default_factory=list)  # 暂缓的负向候选 {path, barcodes[], reason}
    file_records: dict[str, dict] = field(default_factory=dict)  # path → {role, note}
    history: list[dict] = field(default_factory=list)  # 改单记录
    expected_skus: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    no_pl_notified: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---------------- 完整性 ----------------

    def set_expected_skus(self, skus: list[str]) -> None:
        self.expected_skus = sorted(skus)
        self._refresh_status()

    def mark_complete(self, by: str = "人工宣告") -> None:
        self.status = COMPLETE_MANUAL
        self._log("info", "MANUAL_COMPLETE", f"{by}：工厂单据宣告到齐")

    def coverage(self) -> dict:
        have = set(self.items)
        if not self.expected_skus:
            return {"extracted": len(have), "expected": None, "missing": [], "extra": []}
        exp = set(self.expected_skus)
        return {
            "extracted": len(have & exp),
            "expected": len(exp),
            "missing": sorted(exp - have),
            "extra": sorted(have - exp),
        }

    def _refresh_status(self) -> None:
        if self.status == COMPLETE_MANUAL:
            return
        if not self.targets:
            self.status = WAITING_PL
            return
        cov = self.coverage()
        if self.expected_skus and not cov["missing"]:
            self.status = COMPLETE_AUTO
        else:
            self.status = COLLECTING

    # ---------------- 反馈 ----------------

    def _log(self, level: str, type_: str, message: str, file: str = "") -> None:
        self.issues.append({
            "level": level, "type": type_, "message": message,
            "file": file, "ts": time.time(),
        })

    # ---------------- 序列化 ----------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FactorySession":
        return cls(**data)

    def save(self, dir_: Path | None = None) -> Path:
        d = dir_ or SESSIONS_DIR
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.factory}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, factory: str, dir_: Path | None = None) -> "FactorySession":
        path = (dir_ or SESSIONS_DIR) / f"{factory}.json"
        if path.exists():
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(factory=factory)


# ---------------------------------------------------------------------------
# 内部：合并（改单语义）
# ---------------------------------------------------------------------------

def _merge(session: FactorySession, new_items: list[ExtractedItem], from_file: str) -> tuple[list[str], list[str]]:
    new_skus: list[str] = []
    updated: list[str] = []
    for it in new_items:
        if not it.sku_code:
            session.no_code_items.append(it.model_dump())
            continue
        old = session.items.get(it.sku_code)
        if old is None:
            session.items[it.sku_code] = it.model_dump()
            new_skus.append(it.sku_code)
            continue
        same = (
            old.get("total_quantity") == it.total_quantity
            and old.get("total_net_weight") == it.total_net_weight
            and old.get("total_gross_weight") == it.total_gross_weight
        )
        if same:
            continue  # 同值重复（如同内容的多份拷贝），不动
        # 改单：新值覆盖，旧值入 history，标人工确认
        session.history.append({
            "sku": it.sku_code,
            "old": {k: old.get(k) for k in ("total_quantity", "total_net_weight", "total_gross_weight", "source_file")},
            "new": {"total_quantity": it.total_quantity, "total_net_weight": it.total_net_weight,
                    "total_gross_weight": it.total_gross_weight, "source_file": from_file},
            "ts": time.time(),
        })
        dump = it.model_dump()
        dump["needs_human_review"] = True
        dump["review_reason"] = (it.review_reason or "") + "；改单覆盖：新文件数值与此前不同，请人工确认"
        session.items[it.sku_code] = dump
        updated.append(it.sku_code)
        session._log("warning", "SKU_REVISED",
                     f"SKU {it.sku_code} 改单：{old.get('total_quantity')}/{old.get('total_net_weight')}/{old.get('total_gross_weight')}"
                     f" → {it.total_quantity}/{it.total_net_weight}/{it.total_gross_weight}",
                     file=from_file)
    return new_skus, updated


# ---------------------------------------------------------------------------
# 主入口：逐文件处理
# ---------------------------------------------------------------------------

def _rollback_target(session: FactorySession, file_path: str, replaced: list[dict]) -> None:
    """目标提取失败/为空时回滚 targets 登记：移除刚登记的目标，恢复被取代的旧目标。"""
    session.targets = [t for t in session.targets if t["path"] != file_path]
    session.targets.extend(replaced)
    session._log("warning", "TARGET_ROLLBACK",
                 f"目标提取失败已回滚登记：{Path(file_path).name}，后备文件不再被判覆盖",
                 file=file_path)


def process_file(session: FactorySession, file_path: str) -> ProcessResult:
    """单据到达一个处理一个。返回增量报告（覆盖率/缺失/反馈）。"""
    file_path = str(file_path)
    session.updated_at = time.time()
    name = Path(file_path).name

    if file_path in session.file_records:
        rec = session.file_records[file_path]
        return ProcessResult(file=file_path, action="already_processed",
                             message=f"文件此前已处理（身份：{rec.get('role')}）",
                             coverage=session.coverage(), status=session.status)

    profile = scan_file(file_path)

    # --- 无法扫描/无工具的类型 ---
    if profile.channel in ("image", "image_pdf"):
        session.file_records[file_path] = {"role": "image", "note": "图片无法扫描内容"}
        hint = "暂无箱单" if not session.targets else "图片未作为目标"
        session._log("info" if session.targets else "warning", "IMAGE_FILE",
                     f"{hint}：{name} 为图片，识别器无法扫描；如它是箱单请人工指出后走视觉通道",
                     file=file_path)
        session._refresh_status()
        return ProcessResult(file=file_path, action="registered_non_target",
                             message=f"图片文件已登记（{hint}）",
                             coverage=session.coverage(), status=session.status)
    if profile.channel == "unsupported":
        session.file_records[file_path] = {"role": "unsupported", "note": profile.error}
        session._log("warning", "UNSUPPORTED_FILE_TYPE",
                     f"特殊类型文件，现有工具无法调取：{name}（{profile.error}）。"
                     "如该文件是箱单，需人工处理或扩展新通道。",
                     file=file_path)
        session._refresh_status()
        return ProcessResult(file=file_path, action="registered_non_target",
                             message=f"无工具可调取该类型，已反馈（{profile.error}）",
                             coverage=session.coverage(), status=session.status)

    # --- 非候选（PO/发票/报关汇总/模板…）---
    if not profile.is_candidate:
        reasons = []
        if not profile.barcodes:
            reasons.append("无13位条码")
        if not (profile.has_net and profile.has_gross):
            reasons.append("缺重量列")
        if not profile.has_qty:
            reasons.append("缺件数列")
        note = "、".join(reasons) or "非SKU级箱单"
        session.file_records[file_path] = {"role": "non_target", "note": note}
        session._refresh_status()
        return ProcessResult(file=file_path, action="registered_non_target",
                             message=f"非箱单（{note}），不提取",
                             coverage=session.coverage(), status=session.status)

    # --- 候选：负向路径信号 → 暂缓 + 提示「暂无箱单」---
    if _name_score(file_path) <= -100:
        negs = [s for s in _NEGATIVE_NAME_SIGNALS if s in file_path.lower()]
        session.deferred.append({
            "path": file_path, "barcodes": sorted(profile.barcodes),
            "reason": f"路径含负向信号（{'/'.join(negs)}），疑似报关/汇总版",
        })
        session.file_records[file_path] = {"role": "deferred", "note": "负向候选暂缓"}
        if not session.targets and not session.no_pl_notified:
            session.no_pl_notified = True
            session._log("warning", "NO_PACKING_LIST",
                         f"暂无箱单：目前只收到 {name}（疑似报关/汇总类文件，"
                         f"含 {len(profile.barcodes)} 个条码但按规则不直接采用）。"
                         "SKU 级箱单可能未到；若确认它就是箱单，请人工指定强制提取。",
                         file=file_path)
            msg = "暂无箱单（已暂缓并提示）"
        else:
            msg = "负向候选已暂缓"
        session._refresh_status()
        return ProcessResult(file=file_path, action="deferred_negative",
                             message=msg, coverage=session.coverage(), status=session.status)

    # --- 候选：与现有 targets 做集合比较 ---
    for t in session.targets:
        tset = set(t["barcodes"])
        if profile.barcodes <= tset:
            relation = "duplicate" if profile.barcodes == tset else "subset"
            session.file_records[file_path] = {"role": relation, "note": f"已被 {Path(t['path']).name} 覆盖"}
            session._refresh_status()
            return ProcessResult(file=file_path, action=f"ignored_{relation}",
                                 message=f"条码集合已被现有目标覆盖，忽略（不调 LLM）",
                                 coverage=session.coverage(), status=session.status)

    # 新目标（可能取代若干子集 targets）
    replaced = [t for t in session.targets if set(t["barcodes"]) < profile.barcodes]
    session.targets = [t for t in session.targets if t not in replaced]
    session.targets.append({
        "path": file_path, "barcodes": sorted(profile.barcodes),
        "name_score": _name_score(file_path), "forced": False,
    })
    session.no_pl_notified = False
    action = "replaced_target" if replaced else "extracted"
    if replaced:
        session._log("info", "TARGET_SUPERSEDED",
                     f"更优目标到达，取代：{'、'.join(Path(t['path']).name for t in replaced)}")

    # --- 通道提取 + 合并 ---
    try:
        res = _route_extract(file_path)
    except Exception as e:  # noqa: BLE001
        session._log("warning", "CHANNEL_ERROR", f"通道处理失败：{type(e).__name__}: {str(e)[:200]}",
                     file=file_path)
        _rollback_target(session, file_path, replaced)
        session._refresh_status()
        return ProcessResult(file=file_path, action="channel_error",
                             message=f"通道处理失败：{e}", coverage=session.coverage(), status=session.status)
    for note in getattr(res, "notes", []):
        session._log("warning" if "不符" in note and "校正" not in note else "info",
                     "VERIFY_NOTE", note, file=file_path)
    if res.error:
        session._log("warning", "CHANNEL_ERROR", f"通道报错：{res.error}", file=file_path)
    if not res.items:
        session._log("blocking", "TARGET_EMPTY",
                     "目标文件提取结果为空——可能误识别或文件异常，需人工确认。", file=file_path)
        _rollback_target(session, file_path, replaced)

    new_skus, updated = _merge(session, res.items, file_path)
    session._refresh_status()

    # 覆盖率提示：仍缺 SKU 且有暂缓文件时给出线索
    cov = session.coverage()
    if cov.get("missing") and session.deferred:
        session._log("info", "MISSING_HINT",
                     f"仍缺 {len(cov['missing'])} 个 SKU；有 {len(session.deferred)} 份暂缓文件"
                     "（疑似报关/汇总版）可供人工确认是否强制提取。")

    return ProcessResult(
        file=file_path, action=action,
        message=f"提取 {len(res.items)} 条（新增 {len(new_skus)}，改单 {len(updated)}）",
        new_skus=new_skus, updated_skus=updated,
        coverage=cov, status=session.status,
    )


def force_extract(session: FactorySession, file_path: str) -> ProcessResult:
    """人工告知：强制提取指定文件（跳过一切身份判定）。"""
    file_path = str(file_path)
    session.updated_at = time.time()
    # 从暂缓名单移除（如在其中）
    session.deferred = [d for d in session.deferred if d["path"] != file_path]
    try:
        profile = scan_file(file_path)
        barcodes = sorted(profile.barcodes)
    except Exception:  # noqa: BLE001
        barcodes = []
    session.targets.append({
        "path": file_path, "barcodes": barcodes,
        "name_score": 999, "forced": True,
    })
    session.no_pl_notified = False
    try:
        res = _route_extract(file_path)
    except Exception as e:  # noqa: BLE001
        session._log("blocking", "FORCE_EXTRACT_FAILED",
                     f"人工指定文件提取失败：{type(e).__name__}: {str(e)[:200]}", file=file_path)
        session._refresh_status()
        return ProcessResult(file=file_path, action="channel_error",
                             message=f"提取失败：{e}", coverage=session.coverage(), status=session.status)
    new_skus, updated = _merge(session, res.items, file_path)
    session.file_records[file_path] = {"role": "forced", "note": "人工指定强制提取"}
    session._refresh_status()
    return ProcessResult(file=file_path, action="forced",
                         message=f"人工指定提取 {len(res.items)} 条（新增 {len(new_skus)}，改单 {len(updated)}）",
                         new_skus=new_skus, updated_skus=updated,
                         coverage=session.coverage(), status=session.status)


__all__ = [
    "FactorySession", "ProcessResult", "process_file", "force_extract",
    "WAITING_PL", "COLLECTING", "COMPLETE_AUTO", "COMPLETE_MANUAL",
]
