# -*- coding: utf-8 -*-
"""原件差异检测（写入侧人工改动感知 · 阶段三 · 决策 9）。

副本快照 diff（copy_snapshot）只能感知**副本侧**人工改动；原件（原始装箱单）
被人工改单（增删行/改数量）系统此前同样无感知。本模块补上第二条检测轴：

- **基准（结构）**：graph state 的 downstream_row_map（系统上次解析吸收的
  {工厂: {SKU: [行号]}}，len = 行数需求）——原件不需要行级快照，基准天然存在；
- **基准（数量）**：state 不含数量列，单靠 downstream_requirements 无法检测
  「数量改」——故指纹记录里附带 qty_map（{工厂: {SKU: [按行序数量]}}，
  解析吸收时与指纹一同落盘），作为数量列的吸收基线。这不是行级快照
  （无行身份/无三列），仅为数量变化检测服务；
- **指纹存取口径（选定一处，注释留档）**：存批次侧 batch_config.json 的
  "source_fingerprint" 键——{"path", "mtime_ns", "size", "qty_map"}。
  选 batch_config.json 而非 checkpoint state 的理由：rerun 会重置/重建
  state，而 batch_config 在 rerun 归档后由 rerun_batch 重写、指纹随新 run
  重新落盘；且指纹读写不需要 graph 连接（本模块保持只依赖 app.config，
  避免 orchestrator → graph 的反向依赖）。解析吸收后更新指纹（更新点：
  建批 create_batch / 补充工厂吸收后 / rerun_batch 重写配置后 /
  update_batch_paths 换原件后）；
- **指纹预检零成本短路**：mtime_ns+size 一致 → 原件没动，跳过解析；
  错过短路只是多解析一次再比对出空 diff，无正确性风险（计划 §四.2）；
- **历史批次迁移（决策 7 同哲学）**：无指纹记录的老批次首次检查时以当前
  原件为基线静默落盘并返回「未变化」——此前的原件改动不可追溯，一次性取舍。

三入口（实现于 service 层，本模块只提供纯函数与存取）：
补充工厂 add_factories_to_batch（确认门吸收）/ 单厂重试 retry_factory_extraction
（确认后继续）/ reopen 警告条（不阻断）。

容错哲学（与 copy_snapshot/_write_audit 同）：检测是辅助设施，任何失败
只记日志并退化为「无差异」放行，绝不阻塞主流程。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)

# batch_config.json 中指纹记录的键名（口径见模块 docstring）
SOURCE_FINGERPRINT_KEY = "source_fingerprint"


# ---------------------------------------------------------------------------
# SourceDiff：按厂行级结构差异（增行/删行/数量改），三列不作原件 diff 重点
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceDiff:
    """原件差异结果。

    changes: 仅含「有差异工厂」的明细元组，每项
        {"factory": str, "added": int, "removed": int, "qty_changed": int}
        ——added/removed 按 (工厂, SKU) 行数差（原件行增删），
        qty_changed 按 (工厂, SKU, seq) 逐位比对数量基线。
        按工厂名排序（确定性输出，前端契约直接消费本结构）。
    """

    changes: tuple = ()

    def any(self) -> bool:
        return any(c["added"] or c["removed"] or c["qty_changed"]
                   for c in self.changes)

    def for_factories(self, names) -> "SourceDiff":
        """按工厂名子集过滤（入口二/三的单厂口径用）。"""
        wanted = set(names)
        return SourceDiff(tuple(c for c in self.changes
                                if c["factory"] in wanted))

    def describe(self) -> str:
        """人读摘要：工厂「X」（+2行/-1行/数量改3处）、工厂「Y」（……）。"""
        parts: list[str] = []
        for c in self.changes:
            segs: list[str] = []
            if c["added"]:
                segs.append(f"+{c['added']}行")
            if c["removed"]:
                segs.append(f"-{c['removed']}行")
            if c["qty_changed"]:
                segs.append(f"数量改{c['qty_changed']}处")
            parts.append(f"工厂「{c['factory']}」（{'/'.join(segs)}）")
        return "、".join(parts)


def _qty_equal(a, b) -> bool:
    """数量相等判定：None 与 None 相等；数值按 float 容差 1e-9
    （与 copy_snapshot.field_equal 同口径，防 12.3 vs 12.30 误判）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def diff_source_requirements(
    old_row_map: dict[str, dict[str, list]],
    new_rows: dict[str, dict[str, list]],
    old_qty_map: dict[str, dict[str, list]] | None = None,
    factories=None,
) -> SourceDiff:
    """原件行级 diff 纯函数（决策 9 检测范围：行增删 + 数量等业务列变化）。

    Args:
        old_row_map: state downstream_row_map——{工厂: {SKU: [行号]}}，
            只消费 len（行数需求），行号值不用；
        new_rows: parse_source_rows 的重解析结果——{工厂: {SKU: [按行序数量]}}；
        old_qty_map: 指纹记录里的数量基线（解析吸收时落盘）；
            缺失（历史指纹无 qty_map）时数量维度不比对（qty_changed 恒 0），
            结构维度不受影响；
        factories: 限定比对范围（如已完成工厂集合 / 单厂）；None = 两边并集。

    行标识口径与 copy_snapshot 一致：(工厂, SKU, seq 按行序)。原件中间插行会
    导致 seq 重对齐——数量比对可能把「插行后的位移」报成数量改，方向安全
    （提示人工核对），可接受（计划 §四.3 同口径盲区）。
    """
    old_row_map = old_row_map or {}
    new_rows = new_rows or {}
    old_qty_map = old_qty_map or {}
    if factories is None:
        scope = sorted(set(old_row_map) | set(new_rows))
    else:
        scope = sorted(str(f) for f in factories)

    changes: list[dict] = []
    for factory in scope:
        old_skus = old_row_map.get(factory) or {}
        new_skus = new_rows.get(factory) or {}
        added = removed = qty_changed = 0
        for sku in set(old_skus) | set(new_skus):
            old_n = len(old_skus.get(sku) or [])
            new_qs = new_skus.get(sku) or []
            added += max(0, len(new_qs) - old_n)
            removed += max(0, old_n - len(new_qs))
            old_qs = (old_qty_map.get(factory) or {}).get(sku)
            if old_qs:
                # 逐 seq 比对重叠区；超出的部分已由 added/removed 覆盖
                for ov, nv in zip(old_qs, new_qs):
                    if not _qty_equal(ov, nv):
                        qty_changed += 1
        if added or removed or qty_changed:
            changes.append({"factory": factory, "added": added,
                            "removed": removed, "qty_changed": qty_changed})
    return SourceDiff(changes=tuple(changes))


# ---------------------------------------------------------------------------
# 原件解析（数量维度）：与 parse_downstream.parse_downstream_file 同口径
# （pandas 读首个 sheet、SKU 按字符串、工厂/SKU 空或 "nan" 的行跳过），
# 返回值额外携带按行序数量列表——len 与 row_map[工厂][SKU] 一一对应。
# ---------------------------------------------------------------------------

def parse_source_rows(file_path: str) -> dict[str, dict[str, list]]:
    """解析原件 → {工厂: {SKU: [按行序数量]}}（数量取 SOTOBAKO_D_HACCHU_SU）。

    数量为 None = 原件该单元格空/非数值；数量列整列缺失时全部 None
    （qty_changed 维度退化为 0，结构维度不受影响）。
    """
    settings = get_settings()
    df = pd.read_excel(file_path, sheet_name=0,
                       dtype={settings.col_sku: str})
    df[settings.col_sku] = df[settings.col_sku].astype(str).str.strip()
    df[settings.col_factory] = df[settings.col_factory].astype(str).str.strip()
    has_qty = settings.col_qty in df.columns

    rows: dict[str, dict[str, list]] = {}
    for _, row in df.iterrows():
        factory = row[settings.col_factory]
        sku = row[settings.col_sku]
        if not factory or factory == "nan" or not sku or sku == "nan":
            continue
        qty = None
        if has_qty:
            raw = row[settings.col_qty]
            if not pd.isna(raw):
                try:
                    qty = round(float(raw), 2)
                except (TypeError, ValueError):
                    qty = None
        rows.setdefault(factory, {}).setdefault(sku, []).append(qty)
    return rows


# ---------------------------------------------------------------------------
# 指纹存取（batch_config.json，口径见模块 docstring；全部包死）
# ---------------------------------------------------------------------------

def _batch_config_path(batch_id: str) -> Path:
    return get_settings().batch_output_dir(batch_id) / "batch_config.json"


def _read_config(batch_id: str) -> dict:
    try:
        path = _batch_config_path(batch_id)
        if not path.exists():
            return {}
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fingerprint_of(path: str | Path) -> dict | None:
    """原件文件级指纹 {"path", "mtime_ns", "size"}；文件不可读返回 None。"""
    try:
        p = Path(path).expanduser()
        st = p.stat()
        return {"path": str(p), "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    except (OSError, TypeError, ValueError):
        return None


def read_fingerprint(batch_id: str) -> dict | None:
    """读批次指纹记录（含 qty_map 基线）；无记录/损坏返回 None。"""
    fp = _read_config(str(batch_id)).get(SOURCE_FINGERPRINT_KEY)
    return fp if isinstance(fp, dict) else None


def record_fingerprint(batch_id: str, path: str | Path) -> None:
    """解析吸收后更新指纹（含数量基线 qty_map），原子写回 batch_config.json。

    包死：任何失败只记 error 日志（下次检查会懒建基线自愈），绝不阻塞主流程。
    """
    batch_id = str(batch_id)
    try:
        cur = fingerprint_of(path)
        if cur is None:
            return
        cfg = _read_config(batch_id)
        if not cfg:
            return  # 批次配置不存在（异常场景），不无中生有
        cfg[SOURCE_FINGERPRINT_KEY] = {**cur, "qty_map": parse_source_rows(path)}
        cfg_path = _batch_config_path(batch_id)
        tmp = cfg_path.with_name(cfg_path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, cfg_path)
    except Exception:  # noqa: BLE001 辅助设施失败静默降级，见模块 docstring
        logger.error("[原件指纹] 批次 %s 指纹更新失败（下次检查将重建基线）",
                     batch_id, exc_info=True)


def source_fingerprint_changed(batch_id: str) -> bool:
    """原件指纹是否已变（mtime_ns+size 预检，零成本短路）。

    - 无批次配置 / 无原件路径 / 文件不可读 → False（无法判定，放行；
      后续解析若真失败会报出自己的错）；
    - 无指纹记录或路径已换（历史批次迁移，决策 7 同哲学）→ 以当前原件
      懒建基线并返回 False（此前的原件改动不可追溯，一次性取舍）；
    - 有记录 → mtime_ns+size 任一不一致即 True。
    """
    batch_id = str(batch_id)
    try:
        cfg = _read_config(batch_id)
        path = cfg.get("downstream_file_path")
        if not path:
            return False
        cur = fingerprint_of(path)
        if cur is None:
            return False
        fp = cfg.get(SOURCE_FINGERPRINT_KEY)
        if not isinstance(fp, dict) or fp.get("path") != cur["path"]:
            record_fingerprint(batch_id, path)
            logger.info("[原件指纹] 批次 %s 无指纹记录/路径已换，"
                        "以当前原件为基线（此前改动不可追溯）", batch_id)
            return False
        return not (fp.get("mtime_ns") == cur["mtime_ns"]
                    and fp.get("size") == cur["size"])
    except Exception:  # noqa: BLE001 辅助设施失败静默降级，见模块 docstring
        logger.error("[原件指纹] 批次 %s 指纹检查失败（按未变化放行）",
                     batch_id, exc_info=True)
        return False
