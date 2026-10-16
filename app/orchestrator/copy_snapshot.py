# -*- coding: utf-8 -*-
"""output 副本写入快照（写入侧人工改动感知 · 阶段一：快照 + 行标识重定位）。

背景（《2026-10-15 写入侧人工改动感知实施计划》决策 3/5/7）：
- 写入侧以原件为需求基准、消费侧以 output 副本为基准，而副本可被人工
  自由改动（改值/删行/插行），系统此前完全无感知（MX1 复活事故）；
- 本模块在每次系统写副本后全量重扫副本建快照（绝不增量记账），
  落 output/{batch_id}/output_copy_snapshot.json（备份机制 output/ 全量
  覆盖，零额外接线；rerun 归档把旧快照随 run 归档，零特判）；
- 行标识 row_key = (工厂 MAKER_MEI_KJ, SKU SHOHIN_CD, 出现序号 seq)：
  KANRI_NO 实测重复严重不可作定位依据（同一票同 SKU 拆两行是真实业务
  形态），仅作辅助校验列记入快照；同 (工厂, SKU) 组内按行号升序取 seq
  （从 1 起），与 row_map[工厂][SKU] 列表一一对应。

阶段二落地：diff_against_copy 全量五分类 diff（决策 6）+ protected/tombstones
结转（决策 5 重建算法，防「遗忘漏洞」——快照重建绝不盲目采纳现状：
protected 逐列结转、消失行键入墓碑、墓碑只增不减、加回出碑）。

阶段四落地（决策 2）：_detect_factory_removals 整厂剔除/对称恢复检出——
按工厂聚合 diff.deleted_rows，某厂在 requirements 中且其快照行全灭 → 写
factory_manually_removed 审计（剔除名单权威源 = review_audits 每厂最新
一条，零新表零新列）；diff.added_rows 命中最新审计为 removed 的厂 → 写
factory_manually_readded 审计，自动出名单。与 tombstones 双轨印证（该厂
全部 row_key 入碑/出碑由 refresh 结转负责）。

容错哲学（与 _write_audit 同）：快照/diff 是辅助设施，refresh/diff 任何
失败只记 error 日志并退化为「无快照」（保守放行写入+warning），绝不阻塞
写入主流程。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from app.config import get_settings

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 2
SNAPSHOT_FILENAME = "output_copy_snapshot.json"

# row_key 的 JSON 字符串键分隔符：字面 "\0" 两个字符（反斜杠 + 数字零，
# 不是 NUL 控制字符）——工厂名/SKU 不可能包含它；v1 曾误用控制字符踩坑
KEY_SEP = "\\0"

# 辅助校验列（不参与定位）：管理番号 / MX 人工行识别列
COL_KANRI_NO = "KANRI_NO"
COL_PURCHASE_ORDER = "PURCHASE_ORDER"

# 并发兜底：快照读写是单进程内「读-改-写」，per-batch 字典锁
# （照 factory_watch._state_lock 先例；_locks_guard 保护字典本身的创建）
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _batch_lock(batch_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(batch_id)
        if lock is None:
            lock = threading.Lock()
            _locks[batch_id] = lock
        return lock


def snapshot_path(batch_id: str) -> Path:
    """快照文件路径：output/{batch_id}/output_copy_snapshot.json。"""
    return get_settings().batch_output_dir(batch_id) / SNAPSHOT_FILENAME


def _find_copy(batch_id: str) -> Path | None:
    """定位批次副本：containers/ 下 mtime 最新的 *_filled.xlsx（与 XD 透视/
    分体积统计同口径）；无副本返回 None。"""
    containers_dir = get_settings().batch_containers_dir(batch_id)
    if not containers_dir.is_dir():
        return None
    filled = [p for p in containers_dir.glob("*_filled.xlsx") if p.is_file()]
    if not filled:
        return None
    return max(filled, key=lambda p: p.stat().st_mtime)


def find_copy(batch_id: str) -> Path | None:
    """_find_copy 的公开别名（service 层覆盖写入入口等跨模块调用用）。"""
    return _find_copy(batch_id)


def norm_key_text(v) -> str:
    """行标识键成分规整：None/空白 → ""；数值型单元格（Excel 把 13 位条码
    存成数字时）转整数字符串，与 parse_downstream 的 pandas str 口径对齐。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).strip()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def round2(v) -> float | None:
    """数值规整：net/gross/qty 统一 round(float(v), 2) 或 None
    （与 writer 写入 round(..., 2) 对齐，防浮点尾差误判；非数值 → None）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    try:
        return round(float(str(v).strip()), 2)
    except (TypeError, ValueError):
        return None


def _text_or_none(v) -> str | None:
    s = norm_key_text(v)
    return s or None


def _is_mx(v) -> bool:
    """MX 人工行判定（PO 值以 "MX" 开头）。与 writer._is_mx_po 同口径；
    此处不复用是为了让本模块只依赖 app.config（writer 反向依赖本模块）。"""
    if v is None:
        return False
    return str(v).strip().upper().startswith("MX")


def row_key_str(key: tuple[str, str, int]) -> str:
    """row_key 元组 → JSON 字符串键（字面 \\0 分隔，见 KEY_SEP 注释）。"""
    factory, sku, seq = key
    return f"{factory}{KEY_SEP}{sku}{KEY_SEP}{seq}"


def parse_row_key(s: str) -> tuple[str, str, int]:
    """row_key_str 逆运算；格式不符抛 ValueError。"""
    parts = s.split(KEY_SEP)
    if len(parts) != 3:
        raise ValueError(f"非法 row_key 字符串键: {s!r}")
    return parts[0], parts[1], int(parts[2])


def build_row_index(ws) -> dict[tuple[str, str, int], int]:
    """全量扫 ws 建 row_key → 当前行号索引（决策 3 重定位的共享扫描）。

    - row_key = (工厂, SKU, seq)：同 (工厂, SKU) 组内按行号升序取第 N 次
      出现（seq 从 1 起），与 row_map[工厂][SKU] 列表一一对应；
    - 工厂/SKU 任一空值的行（排版填充行）不进索引；
    - 表头缺 MAKER_MEI_KJ/SHOHIN_CD 属异常布局，零容错直接 ValueError。
    """
    settings = get_settings()
    it = ws.iter_rows()
    header_row = next(it, None)
    if header_row is None:
        return {}
    header = [c.value for c in header_row]
    missing = [n for n in (settings.col_factory, settings.col_sku)
               if n not in header]
    if missing:
        raise ValueError(f"下游表缺少关键列 {missing}，无法建立行标识索引")
    ci_f = header.index(settings.col_factory)
    ci_s = header.index(settings.col_sku)

    index: dict[tuple[str, str, int], int] = {}
    seqs: dict[tuple[str, str], int] = {}
    for row_no, row in enumerate(it, start=2):
        factory = norm_key_text(row[ci_f].value if ci_f < len(row) else None)
        sku = norm_key_text(row[ci_s].value if ci_s < len(row) else None)
        if not factory or not sku:
            continue
        group = (factory, sku)
        seqs[group] = seqs.get(group, 0) + 1
        index[(factory, sku, seqs[group])] = row_no
    return index


def build_snapshot(batch_id: str, origin: str = "system") -> dict | None:
    """全量扫副本建快照（version=2 格式一次到位）；无副本返回 None。

    origin 应用于全部行（阶段一无逐行结转）："system" 常规重建 /
    "baseline" 无快照迁移基线（决策 7，由 refresh_snapshot 判定传入）；
    "manual_added" 留待阶段五人工新增行纳入时使用。
    """
    copy_path = _find_copy(batch_id)
    if copy_path is None:
        return None
    settings = get_settings()
    st = copy_path.stat()

    wb = load_workbook(copy_path, read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        it = ws.iter_rows()
        header_row = next(it, None)
        header = [c.value for c in header_row] if header_row else []
        missing = [n for n in (settings.col_factory, settings.col_sku)
                   if n not in header]
        if missing:
            raise ValueError(f"副本缺少关键列 {missing}，无法建快照: {copy_path}")

        def _col(name: str) -> int | None:
            return header.index(name) if name in header else None

        ci_f = header.index(settings.col_factory)
        ci_s = header.index(settings.col_sku)
        # 三列/管理番号/数量/PO 均可缺：新建副本基线时三列尚未插入，
        # 缺失列一律记 None（kanri_no 仅辅助校验，缺列不参与定位）
        ci_kanri = _col(COL_KANRI_NO)
        ci_cn = _col(settings.col_name_cn)
        ci_net = _col(settings.col_net)
        ci_gross = _col(settings.col_gross)
        ci_qty = _col(settings.col_qty)
        ci_po = _col(COL_PURCHASE_ORDER)

        def val(row, ci: int | None):
            return row[ci].value if ci is not None and ci < len(row) else None

        rows: list[dict] = []
        seqs: dict[tuple[str, str], int] = {}
        data_row_count = 0  # 副本数据行总数（含工厂/SKU 空值的填充行）
        for row_no, row in enumerate(it, start=2):
            data_row_count += 1
            factory = norm_key_text(val(row, ci_f))
            sku = norm_key_text(val(row, ci_s))
            if not factory or not sku:
                continue  # 工厂/SKU 皆空/任一空的行不进快照（已确认 §七.6）
            group = (factory, sku)
            seqs[group] = seqs.get(group, 0) + 1
            rows.append({
                "key": {"factory": factory, "sku": sku, "seq": seqs[group]},
                "row_no": row_no,
                "kanri_no": _text_or_none(val(row, ci_kanri)),
                "name_cn": _text_or_none(val(row, ci_cn)),
                "net": round2(val(row, ci_net)),
                "gross": round2(val(row, ci_gross)),
                "qty": round2(val(row, ci_qty)),
                "is_mx": _is_mx(val(row, ci_po)),
                "origin": origin,
                # 阶段一恒为默认；protected 结转逻辑随阶段二 diff 落地
                "protected": False,
                "protected_fields": [],
            })
    finally:
        wb.close()

    return {
        "version": SNAPSHOT_VERSION,
        "batch_id": batch_id,
        "copy_file": copy_path.name,
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "copy_mtime_ns": st.st_mtime_ns,
        "copy_size": st.st_size,
        "data_row_count": data_row_count,
        # 阶段一恒为空名单；墓碑结转（只增不减）随阶段二 diff 落地
        "tombstones": [],
        "rows": rows,
    }


def save_snapshot(batch_id: str, snap: dict) -> None:
    """原子写快照：先写 .tmp 再 os.replace（跨平台），绝不写半截文件。"""
    path = snapshot_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_snapshot(batch_id: str) -> dict | None:
    """读快照；文件不存在 / JSON 损坏 / 版本不符一律返回 None
    （损坏与版本不符记 warning，按无快照处理）。"""
    path = snapshot_path(batch_id)
    if not path.exists():
        return None
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[快照] 批次 %s 快照文件损坏，按无快照处理：%s",
                       batch_id, e)
        return None
    if not isinstance(snap, dict) or snap.get("version") != SNAPSHOT_VERSION:
        logger.warning("[快照] 批次 %s 快照版本不符（%r ≠ %d），按无快照处理",
                       batch_id,
                       snap.get("version") if isinstance(snap, dict)
                       else type(snap).__name__,
                       SNAPSHOT_VERSION)
        return None
    return snap


def _row_key_of(row: dict) -> tuple[str, str, int]:
    """快照行 dict → row_key 元组。"""
    k = row.get("key") or {}
    return (str(k.get("factory") or ""), str(k.get("sku") or ""),
            int(k.get("seq") or 0))


# diff 逐列独立判定的三列（决策 1）：中文品名/净重/毛重
DIFF_FIELDS = ("name_cn", "net", "gross")


def field_equal(a, b) -> bool:
    """字段值相等判定：None 与 None 相等；数值按 float 容差 1e-9 比较
    （openpyxl 读回 2 位小数浮点，避免 12.3 vs 12.30 误判）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return str(a) == str(b)


@dataclass(frozen=True)
class CopyDiff:
    """写前 diff 结果（决策 6 全量分类；frozen，七字段）。

    - value_conflicts: 逐字段人工改非空冲突 [{row_key, factory, sku, seq,
      row_no, field, snapshot, copy}]——快照值与副本值均非空且不等，或快照空
      副本非空（人工抢先填）。命中 protected 列的变更不视为新冲突（保护中的
      人工最终值，由 refresh 结转更新记录值）；「快照非空 × 副本空」= 人工
      改空等重新录入，不是冲突（所有通道一律正常覆盖）。
    - qty_changes: [{row_key, factory, sku, seq, row_no, snapshot_qty,
      copy_qty}]——数量列被人工改（告警不阻断，写入仍按副本当前数量）。
    - deleted_rows: [row_key_str]——快照有、副本无（人工删除；refresh 结转
      时全部入墓碑）。
    - added_rows: [{row_key, row_no, factory, sku, seq, kanri_no,
      in_tombstones}]——副本有、快照无；in_tombstones=True 即人工加回（出碑，
      阶段五纳入流程的输入）。
    - moved_rows: [{row_key, old_row_no, new_row_no}]——信息项（row_key
      重定位本就免疫移位）。
    - mtime_changed: 副本 mtime/size 是否与快照记录不一致（诊断用；
      短路命中时为 False 且其余全空）。
    - protected_cells: ((row_key_str, field), ...)——快照中保护中的单元格
      全集（无论 mtime 是否短路都给出，writer 通道 C 跳过判定用）。
    """

    value_conflicts: tuple = ()
    qty_changes: tuple = ()
    deleted_rows: tuple = ()
    added_rows: tuple = ()
    moved_rows: tuple = ()
    mtime_changed: bool = False
    protected_cells: tuple = ()


def diff_against_copy(batch_id: str) -> CopyDiff | None:
    """快照 vs 副本当前状态的全量 diff（决策 6）。

    返回 None 的退化场景（保守放行写入 + 日志）：无快照 / 无副本 / 任何
    扫描异常——快照是辅助设施，绝不阻塞写入主流程。
    mtime+size 与快照一致 → 空 diff 短路（错过短路只是多扫一次，无正确性
    风险；反向「mtime 没变内容变了」在现代文件系统实际不可发生）。
    """
    batch_id = str(batch_id)
    with _batch_lock(batch_id):
        try:
            snap = load_snapshot(batch_id)
            copy_path = _find_copy(batch_id)
            if snap is None or copy_path is None:
                return None
            protected_cells = tuple(
                (row_key_str(_row_key_of(r)), f)
                for r in snap.get("rows") or []
                for f in (r.get("protected_fields") or [])
            )
            st = copy_path.stat()
            mtime_changed = not (
                st.st_mtime_ns == snap.get("copy_mtime_ns")
                and st.st_size == snap.get("copy_size"))
            if not mtime_changed:
                return CopyDiff(mtime_changed=False,
                                protected_cells=protected_cells)

            cur = build_snapshot(batch_id)
            if cur is None:
                return None
            snap_by_key = {row_key_str(_row_key_of(r)): r
                           for r in snap.get("rows") or []}
            cur_by_key = {row_key_str(_row_key_of(r)): r
                          for r in cur.get("rows") or []}
            tombstones = set(snap.get("tombstones") or [])

            conflicts: list[dict] = []
            qty_changes: list[dict] = []
            deleted: list[str] = []
            added: list[dict] = []
            moved: list[dict] = []

            for k, srow in snap_by_key.items():
                crow = cur_by_key.get(k)
                if crow is None:
                    deleted.append(k)  # 人工删除（refresh 结转入墓碑）
                    continue
                if crow.get("row_no") != srow.get("row_no"):
                    moved.append({"row_key": k,
                                  "old_row_no": srow.get("row_no"),
                                  "new_row_no": crow.get("row_no")})
                protected_fields = set(srow.get("protected_fields") or [])
                for f in DIFF_FIELDS:
                    sv, cv = srow.get(f), crow.get(f)
                    if field_equal(sv, cv):
                        continue
                    if sv is not None and cv is None:
                        continue  # 人工改空 = 等重新录入，所有通道正常覆盖
                    if f in protected_fields:
                        continue  # 保护中的人工最终值：不视为新冲突
                    conflicts.append({
                        "row_key": k,
                        "factory": crow["key"]["factory"],
                        "sku": crow["key"]["sku"],
                        "seq": crow["key"]["seq"],
                        "row_no": crow.get("row_no"),
                        "field": f, "snapshot": sv, "copy": cv,
                    })
                if not field_equal(srow.get("qty"), crow.get("qty")):
                    qty_changes.append({
                        "row_key": k,
                        "factory": crow["key"]["factory"],
                        "sku": crow["key"]["sku"],
                        "seq": crow["key"]["seq"],
                        "row_no": crow.get("row_no"),
                        "snapshot_qty": srow.get("qty"),
                        "copy_qty": crow.get("qty"),
                    })
            for k, crow in cur_by_key.items():
                if k not in snap_by_key:
                    added.append({
                        "row_key": k,
                        "row_no": crow.get("row_no"),
                        "factory": crow["key"]["factory"],
                        "sku": crow["key"]["sku"],
                        "seq": crow["key"]["seq"],
                        "kanri_no": crow.get("kanri_no"),
                        "in_tombstones": k in tombstones,
                    })

            if deleted or added or conflicts:
                logger.info("[快照] 批次 %s 副本 diff：冲突 %d 处 / 删行 %d / "
                            "新增 %d / 移位 %d / 数量改动 %d",
                            batch_id, len(conflicts), len(deleted),
                            len(added), len(moved), len(qty_changes))
            return CopyDiff(
                value_conflicts=tuple(conflicts),
                qty_changes=tuple(qty_changes),
                deleted_rows=tuple(deleted),
                added_rows=tuple(added),
                moved_rows=tuple(moved),
                mtime_changed=True,
                protected_cells=protected_cells,
            )
        except Exception:  # noqa: BLE001 辅助设施失败静默降级，见模块 docstring
            logger.error("[快照] 批次 %s 副本 diff 失败（按无 diff 放行写入）",
                         batch_id, exc_info=True)
            return None


def _carry_over(new: dict, old: dict,
                mark_protected: set[tuple[str, str]] | None,
                unprotect: set[tuple[str, str]] | None,
                mark_manual: set[str] | None) -> None:
    """protected/tombstones/origin 结转（决策 5 重建算法，防遗忘漏洞核心）。

    - origin：旧快照已有键沿用旧 origin；新出现的键保留 build 给的默认
      （"system"），mark_manual 命中的键打 "manual_added"（阶段五）；
    - protected 逐列结转：旧 protected 行键仍在 → 逐被保护列检查：副本当前
      值非空 → 记录值更新为副本当前值（build 已读副本，天然满足）、保护
      标记不变；已被人工清空 → 该列保护解除（等重新录入，下次写入覆盖）；
      全部列解除 → protected 回 false。unprotect（通道 A 覆盖）优先解除；
      mark_protected（通道 B/C 保留）最后并入；
    - tombstones 只增不减：旧行键消失 → 入碑（系统自身不删行，行消失只
      可能是人工删除）；副本中重新出现的墓碑键 → 出碑（人工加回）。
    """
    mark_protected = mark_protected or set()
    unprotect = unprotect or set()
    mark_manual = mark_manual or set()

    old_rows = {row_key_str(_row_key_of(r)): r for r in old.get("rows") or []}
    new_keys: set[str] = set()
    for r in new.get("rows") or []:
        k = row_key_str(_row_key_of(r))
        new_keys.add(k)
        old_r = old_rows.get(k)
        carried: list[str] = []
        if old_r is not None:
            r["origin"] = old_r.get("origin") or r.get("origin") or "system"
            for f in (old_r.get("protected_fields") or []):
                if (k, f) in unprotect:
                    continue  # 通道 A 已覆盖/补写系统值，保护解除
                if r.get(f) is not None:
                    carried.append(f)  # 副本当前值非空 → 保护结转
                # else 人工清空 → 该列保护解除（等重新录入）
        if k in mark_manual:
            r["origin"] = "manual_added"
        for f in carried:
            if f not in r["protected_fields"]:
                r["protected_fields"].append(f)
        for mk, mf in mark_protected:
            if mk == k and mf not in r["protected_fields"]:
                r["protected_fields"].append(mf)
        r["protected"] = bool(r["protected_fields"])

    tombstones = set(old.get("tombstones") or [])
    tombstones |= set(old_rows) - new_keys  # 本次消失的旧行键全部入碑
    tombstones -= new_keys                  # 重新出现 → 出碑（人工加回）
    new["tombstones"] = sorted(tombstones)


def refresh_snapshot(
    batch_id: str,
    mark_protected: set[tuple[str, str]] | None = None,
    unprotect: set[tuple[str, str]] | None = None,
    mark_manual: set[str] | None = None,
) -> dict | None:
    """build + 结转 + save 一体入口（快照四更新点统一调它）。

    参数（均为可选，键 = (row_key_str, field) / row_key_str）：
    - mark_protected：本次写入被保留的人工值单元格（通道 B/C），打保护标；
    - unprotect：本次被系统覆盖/补写的单元格（通道 A），解除保护；
    - mark_manual：本次纳入管理的人工新增行（阶段五），origin 打 manual_added。

    无快照迁移（决策 7）：副本存在但无快照文件 → 以当前副本现状为基线，
    全行 origin="baseline" + warning（此前人工改动不可追溯，一次性取舍）。
    辅助设施包死：任何失败只记 error 日志返回 None，绝不阻塞写入主流程。
    """
    batch_id = str(batch_id)
    with _batch_lock(batch_id):
        try:
            existed = snapshot_path(batch_id).exists()
            old = load_snapshot(batch_id) if existed else None
            snap = build_snapshot(
                batch_id, origin="system" if existed else "baseline")
            if snap is None:
                return None
            if old is not None:
                _carry_over(snap, old, mark_protected, unprotect, mark_manual)
            if not existed:
                logger.warning(
                    "[快照] 批次 %s 无写入快照，以副本现状为基线（baseline），"
                    "此前人工改动不可追溯", batch_id)
            save_snapshot(batch_id, snap)
            return snap
        except Exception:  # noqa: BLE001 辅助设施失败静默降级，见模块 docstring
            logger.error("[快照] 批次 %s 快照刷新失败（写入主流程不受影响）",
                         batch_id, exc_info=True)
            return None


# ---------------------------------------------------------------------------
# 决策 2 / 阶段四：整厂剔除 + 对称恢复检出（审计留痕，名单权威源 = 审计表）
# ---------------------------------------------------------------------------

# 剔除/恢复审计的 result_status（剔除名单 = 每厂最新一条审计为 REMOVED）
MEMBERSHIP_REMOVED = "factory_manually_removed"
MEMBERSHIP_READDED = "factory_manually_readded"


def _latest_audit_statuses(batch_id: str) -> dict[str, str | None]:
    """review_audits 每厂最新一条审计的 result_status（audit_id 升序遍历，
    后者覆盖前者；口径抄 pipeline_state._classify_incomplete_batch）。

    DB 访问用函数内 import：本模块保持只依赖 app.config 的静态依赖
    （source_diff 同哲学，避免 orchestrator → db/graph 的反向依赖）。
    """
    from sqlalchemy import select

    from app.db.models import ReviewAudit
    from app.db.session import get_session

    with get_session() as session:
        rows = session.scalars(
            select(ReviewAudit)
            .where(ReviewAudit.thread_id == batch_id)
            .order_by(ReviewAudit.audit_id)
        ).all()
    latest: dict[str, str | None] = {}
    for r in rows:
        if r.factory_name:
            latest[r.factory_name] = r.result_status
    return latest


def _write_membership_audit(batch_id: str, factory: str,
                            result_status: str, changes: list[dict]) -> None:
    """剔除/恢复审计落库（service._write_audit 同哲学包死：失败只 warning，
    绝不阻塞写入主流程）。"""
    from app.db.models import ReviewAudit
    from app.db.session import get_session

    try:
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=batch_id,
                factory_name=factory,
                approved=False,
                edited_count=0,
                changes_json=json.dumps(changes, ensure_ascii=False),
                new_skus_json="[]",
                result_status=result_status,
            ))
            session.commit()
    except Exception:  # noqa: BLE001 故意包死，见 docstring
        logger.warning("[快照] 批次 %s 工厂「%s」%s 审计落库失败"
                       "（主流程不受影响）",
                       batch_id, factory, result_status, exc_info=True)


def _detect_factory_removals(
    batch_id: str,
    diff: CopyDiff | None,
    requirements,
) -> list[str]:
    """整厂剔除 / 对称恢复检出（决策 2，阶段四；写前 diff 后调用）。

    - 剔除：按工厂聚合 diff.deleted_rows，某厂在 requirements 中且其快照行
      全灭（一行不剩）→ 写 factory_manually_removed 审计
      （changes_json=[{"factory_removed": true, "rows": N}]）。幂等：最新
      审计已是 removed 不重复写。与墓碑双轨印证：该厂全部 row_key 由随后的
      refresh 结转入碑（检出发生在写前，此时墓碑尚未含本次删除键）。
    - 对称恢复：diff.added_rows 命中「该厂最新审计为 removed」→ 写
      factory_manually_readded 审计（changes_json 记行数）→ 最新一条不再
      是 removed，自动离开剔除名单（与墓碑出碑逻辑呼应：行重新出现即出碑；
      新增行纳入主流程在阶段五）。

    辅助设施包死：任何失败只记 error 返回 []，绝不阻塞写入主流程。
    返回本次新检出剔除的工厂名单（排序），供完成摘要透出。
    """
    if diff is None:
        return []
    batch_id = str(batch_id)
    try:
        req = set(requirements or [])
        latest = _latest_audit_statuses(batch_id)
        newly: list[str] = []
        if diff.deleted_rows:
            snap = load_snapshot(batch_id) or {}
            deleted = set(diff.deleted_rows)
            deleted_by_factory: dict[str, int] = {}
            alive_factories: set[str] = set()
            for r in snap.get("rows") or []:
                factory = str((r.get("key") or {}).get("factory") or "")
                if not factory:
                    continue
                if row_key_str(_row_key_of(r)) in deleted:
                    deleted_by_factory[factory] = (
                        deleted_by_factory.get(factory, 0) + 1)
                else:
                    alive_factories.add(factory)
            for factory in sorted(deleted_by_factory):
                if factory in alive_factories or factory not in req:
                    continue  # 快照行未全灭 / 不在需求集，不判定剔除
                if latest.get(factory) == MEMBERSHIP_REMOVED:
                    continue  # 幂等：最新审计已是 removed 不重复写
                rows_n = deleted_by_factory[factory]
                _write_membership_audit(
                    batch_id, factory, MEMBERSHIP_REMOVED,
                    [{"factory_removed": True, "rows": rows_n}])
                latest[factory] = MEMBERSHIP_REMOVED
                newly.append(factory)
                logger.warning(
                    "[快照] 批次 %s 检出整厂人工剔除：工厂「%s」快照 %d 行"
                    "在副本中全灭（本批次不再等待它）",
                    batch_id, factory, rows_n)
        if diff.added_rows:
            readded: dict[str, int] = {}
            for a in diff.added_rows:
                factory = a.get("factory")
                if factory and latest.get(factory) == MEMBERSHIP_REMOVED:
                    readded[factory] = readded.get(factory, 0) + 1
            for factory in sorted(readded):
                _write_membership_audit(
                    batch_id, factory, MEMBERSHIP_READDED,
                    [{"factory_readded": True, "rows": readded[factory]}])
                latest[factory] = MEMBERSHIP_READDED
                logger.info(
                    "[快照] 批次 %s 检出人工加回：工厂「%s」%d 行重新出现"
                    "（自动离开剔除名单）", batch_id, factory, readded[factory])
        return newly
    except Exception:  # noqa: BLE001 辅助设施失败静默降级，见模块 docstring
        logger.error("[快照] 批次 %s 整厂剔除检出失败（不影响写入主流程）",
                     batch_id, exc_info=True)
        return []
