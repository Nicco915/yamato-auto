"""Node 6: 写入与循环（Writer Node）——双写机制（《第三阶段.md》改造点 C）。

1. 写业务表：把人工确认后的精确数据写回下游 Excel 副本
   （先复制到 output/ 目录，绝不覆盖原件）；
   写入规则（2026-07-28 用户定）：
   - 原文件没有 中文品名/净重/毛重 三列：首次写入时在 SHOHIN_MEI_E 后插入
     （与既有填好文件布局一致：第 32/33/34 列），已存在则跳过；
   - 净重 = 单件净重 × SOTOBAKO_D_HACCHU_SU，毛重同理，2 位小数；
   - 中文品名 = 主数据 name_cn（新 SKU 经 Node5 人工补录）；
   - 写入单元格格式：字号 9 + 四周细边框（2026-08-04 用户定），
     字体族/加粗保留单元格原有设置；
   - 表格格式严格不变：全程 openpyxl，禁止 pandas 写入；
   - MX 货物（PURCHASE_ORDER 列形如 "MX2-268510-001" 的行）是人工手动
     加进装箱单的，中文品名/净重/毛重三列已由人工填好并核对——写回与
     reopen 清空均跳过 MX 行，绝不覆盖/清除人工值（2026-09-29）；
   - 第七阶段遇值确认门（§1.3/§2.4）：写入前预检目标格已有非空值
     ≠ 系统值（非 MX 行）→ 整厂一次 interrupt 挂起确认；「覆盖」写
     系统值并留痕，「保留」行记入 state.manual_kept_rows，视同 MX
     人工行（后续写回/reopen 清空均跳过，永不覆盖）；
2. 写数据库（Upsert）：
   - 新 SKU：INSERT 人工补录的多语言品名/HS 编码/单件重量；
   - 老 SKU：人工微调过重量时 UPDATE 刷新历史重量。

写盘保护（2026-08-11）：落盘前做可写探测 + 捕获 PermissionError/OSError(EACCES/EBUSY)，
把英文 traceback 转成中文提示。Windows 下 Excel/LibreOffice 独占锁文件时，
裸 PermissionError 会让用户不知所措；探测可以在做完一堆耗时工作之前先发现问题。
"""
import errno
import json
import logging
import shutil
from copy import copy
from pathlib import Path

from langgraph.types import interrupt
from openpyxl import load_workbook
from openpyxl.styles import Border, Side
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Factory, FactorySKU, ReviewAudit
from app.db.session import get_session
from app.db.sync import attach_sku_to_mapping, relink_sku_to_name
from app.logging_config import bind_factory_from_state
from app.state import AgentState

logger = logging.getLogger(__name__)

# Windows 共享冲突（Excel/LibreOffice 独占打开产生的 EBUSY）也归为「文件被占用」一类提示
_FILE_LOCK_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.EPERM, errno.ETXTBSY})

# 待添加的三列（插入到 SHOHIN_MEI_E 之后，与既有填好文件布局一致）
NEW_COL_NAMES = ("中文品名", "净重", "毛重")
INSERT_AFTER_COL = "SHOHIN_MEI_E"

# MX 人工行识别列：PO 值以 "MX" 开头的行（如 "MX2-268510-001"）为人工手动
# 加进装箱单的货物，三列已由人工填好，写回/清空均跳过；该列缺失视为无 MX
PO_COL_NAME = "PURCHASE_ORDER"


def _is_mx_po(value) -> bool:
    """判断 PURCHASE_ORDER 单元格值是否属于 MX 人工行。"""
    if value is None:
        return False
    return str(value).strip().upper().startswith("MX")

# 写入单元格格式（2026-08-04 用户定）：字号 9 + 四周细边框
WRITE_FONT_SIZE = 9
_THIN_SIDE = Side(style="thin")
WRITE_BORDER = Border(left=_THIN_SIDE, right=_THIN_SIDE,
                      top=_THIN_SIDE, bottom=_THIN_SIDE)

# C 级 short_name 自动回填：仅高置信匹配档允许（fuzzy/contains/none 走审核页建议卡人工确认）
_AUTO_SHORT_NAME_METHODS = frozenset(
    {"override", "alias", "alias_ci", "exact", "alias_folder"})


def _apply_write_format(cell) -> None:
    """写入单元格格式：字号 9 + 四周细边框；字体族/加粗/颜色保留原设置。"""
    font = copy(cell.font)   # StyleProxy → Font，可改后再赋值
    font.sz = WRITE_FONT_SIZE
    cell.font = font
    cell.border = WRITE_BORDER


def _ensure_output_copy(
    state: AgentState, return_created: bool = False,
) -> Path | tuple[Path, bool]:
    """确保 output/{batch_id}/containers/ 下存在原件副本；已存在则复用。

    return_created=True 时返回 (副本路径, 本次是否新建)。Node1 据此判断
    「新一轮写入起点」——rerun 归档旧 output 后副本会重建，旧 run 的
    factory_outputs/manual_kept_rows 必须清空，否则遇值确认门会把原件里
    人工预填的三列误判为「系统上一轮结转值」而漏确认。
    """
    settings = get_settings()
    batch_id = state.get("batch_id") or "unknown"
    out_dir = settings.batch_containers_dir(batch_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(state["downstream_file_path"])
    dst = out_dir / f"{src.stem}_filled{src.suffix}"
    created = not dst.exists()
    if created:
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            # 原件被占用时也走中文提示；优先指向原文件路径（用户日常认知的是原件）
            if _is_file_lock_error(e):
                raise RuntimeError(_format_file_busy_msg(src)) from e
            raise
        logger.info("[Node6] 已复制原件到 %s（绝不覆盖原件）", dst)
        # 快照更新点 1（写入侧人工改动感知 决策 5）：新建副本后建基线快照；
        # 延迟 import 避免 app.graph → writer → orchestrator 循环导入
        from app.orchestrator import copy_snapshot
        copy_snapshot.refresh_snapshot(batch_id)
    return (dst, created) if return_created else dst


def _probe_writable(path: Path) -> None:
    """以追加模式短暂打开再关闭，验证可写。绝不以「w」模式探测（会清空文件）。

    Excel/LibreOffice 在 Windows 下打开 xlsx 会独占锁，此时以「a」模式打开仍会
    抛 PermissionError(EBUSY)；文件不存在则跳过探测，由后续真实写入走相同异常路径。

    注意：只有 PermissionError 或 _FILE_LOCK_ERRNOS 中的错误才会被翻译成
    「文件被占用」提示；IsADirectoryError/FileNotFoundError/EIO 等仍原样抛出，
    避免误导用户。
    """
    if not path.exists():
        return
    try:
        with path.open("a"):
            pass
    except OSError as e:
        if _is_file_lock_error(e):
            raise RuntimeError(_format_file_busy_msg(path)) from e
        raise


def _is_file_lock_error(exc: OSError) -> bool:
    """判断异常是否属于「文件被占用 / 权限拒绝」类错误。"""
    errno_no = getattr(exc, "errno", None)
    return errno_no in _FILE_LOCK_ERRNOS or isinstance(exc, PermissionError)


def _format_file_busy_msg(path: Path) -> str:
    """把 PermissionError/OSError(EACCES/EBUSY) 翻译成可读的中文提示。

    Windows 下 EBUSY/EACCES 几乎都意味着 Excel/LibreOffice 独占锁；
    macOS/Linux 下若出现则多是目录权限或罕见共享冲突。
    """
    return (
        f"输出文件正被 Excel 或其他程序占用，请先关闭该文件后重试。"
        f"文件路径：{path}"
    )


def _ensure_three_columns(ws) -> None:
    """表头缺 中文品名/净重/毛重 时在 SHOHIN_MEI_E 后插入；已存在则跳过。

    部分缺失属于异常布局（非原文件也非既有填好文件），零容错直接报错交人工。
    """
    header = [c.value for c in ws[1]]
    missing = [n for n in NEW_COL_NAMES if n not in header]
    if not missing:
        return
    if len(missing) != len(NEW_COL_NAMES):
        raise ValueError(f"下游表列布局异常：三列部分缺失 {missing}，请人工确认")
    anchor = header.index(INSERT_AFTER_COL) + 1  # 1 基列号
    ws.insert_cols(anchor + 1, len(NEW_COL_NAMES))
    # 表头样式照搬锚点列（字体/边框/填充），保证格式严格不变
    for j, name in enumerate(NEW_COL_NAMES):
        cell = ws.cell(row=1, column=anchor + 1 + j, value=name)
        cell._style = copy(ws.cell(row=1, column=anchor)._style)
    logger.info("[Node6] 原文件无 %s 三列，已在 %s 后插入",
                list(NEW_COL_NAMES), INSERT_AFTER_COL)


def _cell_non_empty(v) -> bool:
    """副本单元格「非空」判定（通道 C 保护跳过用）：None/空白字符串为空。"""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


# ---------------------------------------------------------------------------
# 第七阶段 §1.3/§2.4：写入遇值确认门（overwrite gate）
# ---------------------------------------------------------------------------

# 门禁判定的「人工来源」快照 origin：
# - baseline：建批基线快照记录的装箱单原值（含人工预先填写的三列），
#   只在「本厂从未写入过」时采信，防把系统上一轮结转值误判为人工值；
# - manual_added：决策 4 阶段五纳入管理的人工新增行，无论本厂是否写入过
#   都必须参与门禁（否则会被无确认覆盖）。
# origin="system" 是本系统写入值，走既有通道分流即可，不进门禁

# 确认门 payload 的字段名映射（契约 §2.4：old/new 以中文列名为键）
_GATE_FIELD_LABELS = {"name_cn": "中文品名", "net": "净重", "gross": "毛重"}


def _collect_overwrite_conflicts(
    state: AgentState,
    out_path: Path,
) -> tuple[list[dict], set[tuple[str, str]]]:
    """写入遇值确认门预检（第七阶段 §1.3）：收集本厂「目标格已有非空值
    且与系统值不同」的待写单元格，返回 (整厂冲突清单 payload, 门禁单元格集)。

    门禁命中条件（逐单元格，三列独立判定）：
    - 会实际写入（有系统值：品名非空 / 单重非 None，与 _write_excel 主循环
      同一 pending_writes 口径）；占位行/副本缺行/MX 行/已保留行
      （manual_kept_rows）/保护中单元格不进门禁；
    - 且满足以下任一来源判定：
      a) 写前 diff 冲突（快照值 × 副本值不一致，副本非空——人工改过）；或
      b) 快照 origin == "manual_added"（决策 4 阶段五纳入管理的人工新增
         行，无论本厂是否写入过都参与门禁）；或
      c) 快照 origin == "baseline" 且本厂从未写入过（不在
         factory_outputs——防把系统自己上一轮写的 baseline 结转值误判为
         人工值）且副本当前值非空（人工预先填好，人工改动感知 diff 检不出
         因为基线快照记录的就是该值）；
    - 且副本当前值 ≠ 系统将写值（field_equal 容差对齐）。

    快照缺失/扫描失败 → 返回空（保守放行，与 diff_against_copy 同哲学：
    快照是辅助设施，绝不阻塞写入主流程）。返回的 gate_cells 直接传入
    _write_excel 执行，保证「确认所见 = 实际执行」逐格一致。
    """
    # 延迟 import 避免 app.graph → writer → orchestrator 循环导入
    from app.orchestrator import copy_snapshot

    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    batch_id = state.get("batch_id")
    if not factory or not batch_id:
        return [], set()

    snapshot = copy_snapshot.load_snapshot(batch_id)
    if snapshot is None:
        return [], set()
    origins: dict[str, str] = {}
    for r in snapshot.get("rows") or []:
        k = r.get("key") or {}
        origins[copy_snapshot.row_key_str((
            str(k.get("factory") or ""), str(k.get("sku") or ""),
            int(k.get("seq") or 0)))] = r.get("origin") or "system"

    conflict_map: dict[tuple[str, str], dict] = {}
    protected_cells: set[tuple[str, str]] = set()
    diff = copy_snapshot.diff_against_copy(batch_id)
    if diff is not None:
        conflict_map = {(c["row_key"], c["field"]): c
                        for c in diff.value_conflicts}
        protected_cells = set(diff.protected_cells)

    kept_rows = set((state.get("manual_kept_rows") or {}).get(factory) or [])
    # 本厂已写入过（factory_outputs 有快照）→ 副本里 origin=baseline 的
    # 非空值可能是系统自己上一轮写的，来源判定 b 一律不采信（人工改动
    # 仍可由来源 a 的 diff 冲突捕获）
    written_before = factory in (state.get("factory_outputs") or {})

    settings = get_settings()
    wb = load_workbook(out_path)
    try:
        ws = wb[wb.sheetnames[0]]
        header = [c.value for c in ws[1]]
        if any(n not in header for n in NEW_COL_NAMES):
            # 副本尚无三列（极端：快照存在但副本被人工换成无三列版）→
            # 三列全空，无任何可冲突人工值
            return [], set()
        col_by_field = {
            "name_cn": header.index(settings.col_name_cn) + 1,
            "net": header.index(settings.col_net) + 1,
            "gross": header.index(settings.col_gross) + 1,
        }
        col_qty = header.index(settings.col_qty) + 1
        col_po = (header.index(PO_COL_NAME) + 1
                  if PO_COL_NAME in header else None)
        row_index = copy_snapshot.build_row_index(ws)
        row_map = (state.get("downstream_row_map") or {}).get(factory) or {}

        gate_cells: set[tuple[str, str]] = set()
        by_row: dict[tuple[str, int], dict] = {}
        for item in cur.get("calculated_items") or []:
            calc = item.get("calculation") or {}
            unit_net = calc.get("calculated_unit_net")
            unit_gross = calc.get("calculated_unit_gross")
            name_cn = (item.get("name_cn")
                       or (item.get("db_record") or {}).get("name_cn"))
            if unit_net is None and unit_gross is None and not name_cn:
                continue  # 占位行：主循环同样跳过，无写入即无冲突
            sku = str(item.get("sku"))
            sku_key = copy_snapshot.norm_key_text(sku)
            rows_need = len(row_map.get(sku, []))
            for seq in range(1, rows_need + 1):
                excel_row = row_index.get(
                    (copy_snapshot.norm_key_text(factory), sku_key, seq))
                if excel_row is None:
                    continue
                if col_po is not None and _is_mx_po(
                        ws.cell(row=excel_row, column=col_po).value):
                    continue  # MX 人工行维持直接跳过，不进确认门（§2.4）
                key_str = copy_snapshot.row_key_str(
                    (copy_snapshot.norm_key_text(factory), sku_key, seq))
                if key_str in kept_rows:
                    continue  # 已「保留」的人工行视同 MX，不再打扰
                qty = ws.cell(row=excel_row, column=col_qty).value
                qty = float(qty) if isinstance(qty, (int, float)) else 0.0
                pending_writes: list[tuple[str, int, object]] = []
                if name_cn:
                    pending_writes.append(
                        ("name_cn", col_by_field["name_cn"], name_cn))
                if unit_net is not None:
                    pending_writes.append(
                        ("net", col_by_field["net"], round(unit_net * qty, 2)))
                if unit_gross is not None:
                    pending_writes.append(
                        ("gross", col_by_field["gross"],
                         round(unit_gross * qty, 2)))
                for field, col, new_val in pending_writes:
                    ck = (key_str, field)
                    if ck in protected_cells:
                        continue  # 保护中的人工最终值：通道 C 本就跳过
                    old_raw = ws.cell(row=excel_row, column=col).value
                    conflict = conflict_map.get(ck)
                    origin = origins.get(key_str)
                    gate = (
                        conflict is not None
                        # 人工新增行：无论本厂此前是否写入过都必须确认，
                        # 否则已写入工厂中的 manual_added 行会被无确认覆盖
                        or (origin == "manual_added"
                            and _cell_non_empty(old_raw))
                        # 基线行只在首次写入时视为人工预填；已写入过则可能
                        # 是本系统上一轮的结转值，防误判
                        or (origin == "baseline" and not written_before
                            and _cell_non_empty(old_raw))
                    )
                    if not gate:
                        continue
                    if copy_snapshot.field_equal(old_raw, new_val):
                        continue  # 值相同无冲突（人工值恰好等于系统值）
                    gate_cells.add(ck)
                    agg = by_row.setdefault((sku, excel_row),
                                            {"old": {}, "new": {}})
                    agg["old"][_GATE_FIELD_LABELS[field]] = old_raw
                    agg["new"][_GATE_FIELD_LABELS[field]] = new_val
    finally:
        wb.close()

    conflicts = [
        {"sku": sku, "excel_row": row_no, "old": agg["old"], "new": agg["new"]}
        for (sku, row_no), agg in sorted(by_row.items(), key=lambda kv: kv[0][1])
    ]
    return conflicts, gate_cells


def _write_excel(
    state: AgentState,
    out_path: Path,
    channel: str = "first_write",
    conflict_decision: str | None = None,
    gate_cells: set[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """按行标识精准写回 中文品名/净重/毛重 单元格。

    返回 (写入的行数, 跳过的占位行数)：无有效单重且无中文品名的条目
    （提取失败占位/Error 项）逐条记 warning 并计数，供导出摘要统计
    （提取失败告警闭环 §1.5）。

    写前 diff 通道分流（写入侧人工改动感知 决策 1，阶段二）：
    - channel="first_write"（默认，通道 C：resume_order 图内首次写入，
      无法中途弹窗）——检测到人工非空修改（快照×副本冲突）或保护中的
      人工最终值（protected 且副本非空）→ 跳过保留 + warning + 明细塞
      cur["manual_conflicts"]（service 审计留痕 manual_override_kept、
      完成摘要透出），被保留单元格在落盘后的快照重建打 protected；
    - channel="rework"（通道 A：reopen/补充工厂确认覆写）——不加保护
      直接覆盖，被盖掉的人工旧值逐单元格记 cur["manual_overwritten_cells"]
      （service 审计留痕 manual_value_overwritten），protected 相应解除；
    - 快照非空 × 副本空（人工改空等重新录入）→ 两通道一律正常覆盖；
    - 数量列被人工改（快照 qty ≠ 副本 qty）→ warning + 明细塞
      cur["qty_manual_changes"]，写入仍按副本当前数量计算（告警不阻断）。
    diff/快照失败返回 None 时保守放行（无冲突语义），绝不阻塞写入主流程。

    第七阶段 §1.3/§2.4 遇值确认门（writer() 节点预检挂起后传入）：
    - gate_cells：_collect_overwrite_conflicts 收集的门禁单元格
      （(row_key_str, field) 集合），None = 未走门禁（直接调用/rework
      通道），行为与既有通道分流完全一致；
    - conflict_decision="keep"：门禁单元格按通道 C 保留语义跳过 +
      留痕 + 快照打 protected，行键由 writer() 落账 manual_kept_rows；
    - conflict_decision="overwrite"：门禁单元格按系统值写入，被盖
      人工旧值逐格留痕 cur["manual_overwritten_cells"]；
    - 非门禁单元格不受 decision 影响，仍按既有通道规则处理。
    已「保留」的行（state.manual_kept_rows）视同 MX 人工行：所有通道
    （含 rework）一律整行跳过，永不覆盖（§1.3）。

    人工新增行纳入（决策 4，阶段五）：写前 diff 的 added_rows 经
    copy_snapshot.classify_added_rows 三态分类——
    - A 态（厂在原件需求集）：plan_manual_merge 算出并入计划，本厂
      row_map 当场并入生效（写入定位用），并记入
      cur["_manual_added_merge"] 由调用方持久化进 state（writer() 节点
      返回值合并 / apply_reopen_payload update_state）；提取结果无此 SKU
      的新增行保留人工行不动 + warning + cur["manual_rows_kept_no_extract"]
      留痕（service 审计 source="manual_row_kept_no_extract"）；
    - B 态（全新厂）：并入计划含 b_factories，由调用方并入 pending 走
      既有文件夹匹配→提取→审核链路；
    - C 态（缺管理番号）：不纳入，warning 提示补全（缺工厂/SKU 的行进
      不了快照 diff，由 copy_snapshot.scan_incomplete_rows 活扫透出）；
    落盘后快照重建把 A/B 行键打 origin="manual_added"（mark_manual）。
    """
    settings = get_settings()
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    row_map = (state.get("downstream_row_map") or {}).get(factory) or {}

    wb = load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    _ensure_three_columns(ws)

    # 插入列后按表头名重新定位（列位可能已右移）
    header = [c.value for c in ws[1]]
    col_net = header.index(settings.col_net) + 1
    col_gross = header.index(settings.col_gross) + 1
    col_cn = header.index(settings.col_name_cn) + 1
    col_qty = header.index(settings.col_qty) + 1
    # MX 人工行识别列：无 PURCHASE_ORDER 列视为无 MX，全部照常写
    col_po = header.index(PO_COL_NAME) + 1 if PO_COL_NAME in header else None

    # 行标识重定位（写入侧人工改动感知 决策 3）：row_map 从此只提供
    # 「该 SKU 应有几行」（len = 行数需求），行号值不再消费；实际行号按
    # (工厂, SKU, seq) 在副本中现扫定位，人工插删行移位后仍写对位置。
    # 延迟 import 避免 app.graph → writer → orchestrator 循环导入
    from app.orchestrator import copy_snapshot
    row_index = copy_snapshot.build_row_index(ws)

    # 写前 diff（决策 1/6）：value_conflicts 逐字段冲突 + protected 单元格
    # 全集 + qty 改动。快照不存在/损坏/扫描失败 → diff None，保守放行。
    batch_id = state.get("batch_id")
    conflict_map: dict[tuple[str, str], dict] = {}
    protected_cells: set[tuple[str, str]] = set()
    qty_change_map: dict[str, dict] = {}
    manual_merge: dict | None = None     # 决策 4 并入计划（A/B 态）
    added_classified: dict | None = None  # 决策 4 三态分类（留痕用）
    if batch_id:
        diff = copy_snapshot.diff_against_copy(batch_id)
        if diff is not None:
            conflict_map = {(c["row_key"], c["field"]): c
                            for c in diff.value_conflicts}
            protected_cells = set(diff.protected_cells)
            qty_change_map = {q["row_key"]: q for q in diff.qty_changes}
            # 决策 2（阶段四）：写前 diff 后检出整厂剔除/对称恢复
            # （审计留痕，幂等；检出失败包死不影响写入）
            copy_snapshot._detect_factory_removals(
                batch_id, diff,
                (state.get("downstream_requirements") or {}).keys())
            # 决策 4（阶段五）：人工新增行三态分类与并入计划
            if diff.added_rows:
                known = (set(state.get("downstream_requirements") or {})
                         | set(state.get("downstream_row_map") or {}))
                added_classified = copy_snapshot.classify_added_rows(
                    diff.added_rows, known)
                for c in added_classified["c"]:
                    logger.warning(
                        "[Node6] 副本第 %s 行（%s/%s）缺管理番号"
                        "（KANRI_NO），未纳入批次管理，请补全",
                        c.get("row_no"), c.get("factory"), c.get("sku"))
                if added_classified["a"] or added_classified["b"]:
                    manual_merge = copy_snapshot.plan_manual_merge(
                        added_classified["a"], added_classified["b"],
                        state.get("downstream_requirements") or {},
                        state.get("downstream_row_map") or {})
                    # 本厂 A 态并入当场生效：写入定位用并入后的 row_map
                    # （占位列表 len = 行数需求，row_key 索引定位实际行）
                    upd_rm = manual_merge["row_map_updates"].get(factory)
                    if upd_rm is not None:
                        row_map = upd_rm
                    for bf in manual_merge["b_factories"]:
                        logger.info(
                            "[Node6] 检出人工新增全新工厂「%s」的副本行，"
                            "并入本批次待处理队列", bf)

    # 通道留痕字段复位（随 cur 快照进 factory_outputs，供 service 审计/摘要）
    cur["manual_conflicts"] = []
    cur["manual_override_kept"] = 0
    cur["manual_overwritten_cells"] = []
    cur["manual_values_overwritten"] = 0
    cur["qty_manual_changes"] = []
    # 决策 4（阶段五）：A 态无提取结果支撑的人工新增行留痕
    # （service 审计 source="manual_row_kept_no_extract"）
    cur["manual_rows_kept_no_extract"] = []
    if manual_merge is not None:
        # 并入计划暂存 cur，由调用方取出持久化（writer() 节点返回值合并 /
        # apply_reopen_payload update_state）；writer() 快照 cur 前会摘除
        cur["_manual_added_merge"] = manual_merge
    mark_protected: set[tuple[str, str]] = set()
    unprotect: set[tuple[str, str]] = set()

    # 第七阶段 §1.3「保留」行集（视同 MX 人工行，所有通道整行跳过）
    kept_row_keys = set(
        (state.get("manual_kept_rows") or {}).get(factory) or [])

    written = 0
    skipped_mx = 0
    skipped_kept = 0
    skipped_placeholder = 0
    for item in cur.get("calculated_items") or []:
        calc = item.get("calculation") or {}
        unit_net = calc.get("calculated_unit_net")
        unit_gross = calc.get("calculated_unit_gross")
        name_cn = item.get("name_cn") or (item.get("db_record") or {}).get("name_cn")
        if unit_net is None and unit_gross is None and not name_cn:
            # Error 项/占位数据无有效单重，留给人工线下处理——不再静默跳过
            skipped_placeholder += 1
            logger.warning(
                "[Node6] 跳过占位行：工厂「%s」SKU「%s」无有效单重且无中文品名"
                "（原因：%s）", factory, item.get("sku"),
                item.get("source_file") or item.get("review_reason") or "无有效数据")
            continue
        sku = str(item.get("sku"))
        sku_key = copy_snapshot.norm_key_text(sku)
        rows_need = len(row_map.get(sku, []))
        for seq in range(1, rows_need + 1):
            excel_row = row_index.get(
                (copy_snapshot.norm_key_text(factory), sku_key, seq))
            if excel_row is None:
                # 副本中找不到该 row_key（人工删行进墓碑）：绝不复活，记 warning
                logger.warning(
                    "[Node6] 工厂「%s」SKU「%s」第 %d 行在副本中未找到"
                    "（可能被人工删除），跳过该行写入", factory, sku, seq)
                continue
            if col_po is not None and _is_mx_po(
                    ws.cell(row=excel_row, column=col_po).value):
                # MX 行三列是人工已填并核对好的值，绝不覆盖
                skipped_mx += 1
                continue
            key_str = copy_snapshot.row_key_str(
                (copy_snapshot.norm_key_text(factory), sku_key, seq))
            if key_str in kept_row_keys:
                # 第七阶段 §1.3：确认门「保留」的人工行视同 MX 人工行，
                # 所有通道（含 rework/reopen）一律整行跳过，永不覆盖
                skipped_kept += 1
                continue

            # 数量列告警（决策 1 附则）：人工改数量 → warning + 摘要透出，
            # 写入仍按副本当前数量吸收（人工数量优先，绝不静默）
            qty_change = qty_change_map.get(key_str)
            if qty_change is not None:
                logger.warning(
                    "[Node6] 行 %d（%s/%s）数量被人工改过（%s→%s），"
                    "已按新数量写入", excel_row, factory, sku,
                    qty_change.get("snapshot_qty"), qty_change.get("copy_qty"))
                cur["qty_manual_changes"].append({
                    "factory": factory, "sku": sku, "row": excel_row,
                    "old": qty_change.get("snapshot_qty"),
                    "new": qty_change.get("copy_qty"),
                })

            qty = ws.cell(row=excel_row, column=col_qty).value
            qty = float(qty) if isinstance(qty, (int, float)) else 0.0
            # 逐列独立判定（同一行可部分列覆盖、部分列跳过/保护）
            pending_writes: list[tuple[str, int, object]] = []
            if name_cn:
                pending_writes.append(("name_cn", col_cn, name_cn))
            if unit_net is not None:
                pending_writes.append(("net", col_net, round(unit_net * qty, 2)))
            if unit_gross is not None:
                pending_writes.append(("gross", col_gross, round(unit_gross * qty, 2)))
            row_written = False
            for field, col, new_val in pending_writes:
                ck = (key_str, field)
                conflict = conflict_map.get(ck)
                old_raw = ws.cell(row=excel_row, column=col).value
                is_protected = ck in protected_cells and _cell_non_empty(old_raw)
                # 第七阶段 §2.4 确认门单元格：执行决策与挂起时展示清单逐格一致
                is_gate = gate_cells is not None and ck in gate_cells
                gate_keep = is_gate and conflict_decision == "keep"
                if channel != "rework" and (conflict is not None
                                            or is_protected or gate_keep):
                    # 通道 C / 确认门「保留」：默认跳过保留人工值 + 快照打 protected
                    cur["manual_override_kept"] += 1
                    cur["manual_conflicts"].append({
                        "factory": factory, "sku": sku, "row": excel_row,
                        "field": field,
                        "old": conflict["snapshot"] if conflict else None,
                        "new": conflict["copy"] if conflict else old_raw,
                    })
                    mark_protected.add(ck)
                    logger.warning(
                        "[Node6] 行 %d（%s/%s）%s 列检测到人工修改"
                        "（快照 %r → 副本 %r），保留人工值未覆盖",
                        excel_row, factory, sku, field,
                        conflict["snapshot"] if conflict else None,
                        conflict["copy"] if conflict else old_raw)
                    continue
                cell = ws.cell(row=excel_row, column=col, value=new_val)
                _apply_write_format(cell)
                row_written = True
                if is_gate and conflict_decision == "overwrite":
                    # 第七阶段确认门「覆盖」：写系统值，被盖人工旧值留痕
                    cur["manual_overwritten_cells"].append({
                        "factory": factory, "sku": sku, "row": excel_row,
                        "field": field, "old": old_raw, "new": new_val,
                    })
                    cur["manual_values_overwritten"] += 1
                    unprotect.add(ck)
                    logger.info(
                        "[Node6] 行 %d（%s/%s）%s 列人工值 %r 已被系统值 %r"
                        " 覆盖（遇值确认门「覆盖」决策）",
                        excel_row, factory, sku, field, old_raw, new_val)
                elif conflict is not None:
                    # 通道 A：覆盖人工非空值，被盖旧值留痕 manual_value_overwritten
                    cur["manual_overwritten_cells"].append({
                        "factory": factory, "sku": sku, "row": excel_row,
                        "field": field, "old": conflict["copy"], "new": new_val,
                    })
                    cur["manual_values_overwritten"] += 1
                    unprotect.add(ck)
                    logger.info(
                        "[Node6] 行 %d（%s/%s）%s 列人工值 %r 已被系统值 %r 覆盖"
                        "（通道 A 确认覆写）", excel_row, factory, sku, field,
                        conflict["copy"], new_val)
                elif is_protected:
                    # 通道 A 覆盖保护中的人工最终值：解除保护；值不同才留痕
                    unprotect.add(ck)
                    if not copy_snapshot.field_equal(old_raw, new_val):
                        cur["manual_overwritten_cells"].append({
                            "factory": factory, "sku": sku, "row": excel_row,
                            "field": field, "old": old_raw, "new": new_val,
                        })
                        cur["manual_values_overwritten"] += 1
            if row_written:
                written += 1
    # 决策 4（阶段五）A 态无提取结果分支：本厂人工新增行的 SKU 不在
    # calculated_items → 保留人工行不动 + warning + 留痕（提取结果含此
    # SKU 的行已在主循环按决策 1 规则写入）
    if added_classified and added_classified["a"]:
        calc_skus = {str(i.get("sku")) for i in cur.get("calculated_items") or []}
        fac_norm = copy_snapshot.norm_key_text(factory)
        for a in added_classified["a"]:
            if copy_snapshot.norm_key_text(a.get("factory")) != fac_norm:
                continue
            if str(a.get("sku")) in calc_skus:
                continue
            cur["manual_rows_kept_no_extract"].append({
                "factory": factory, "sku": str(a.get("sku")),
                "row": a.get("row_no")})
            logger.warning(
                "[Node6] 人工新增行：工厂「%s」SKU「%s」（副本第 %s 行）"
                "无提取结果支撑，保留人工行未写入",
                factory, a.get("sku"), a.get("row_no"))
    if skipped_mx:
        logger.info("[Node6] 跳过 MX 行写回 %d 行（人工已填值）", skipped_mx)
    if skipped_kept:
        logger.info("[Node6] 跳过「保留」人工行写回 %d 行"
                    "（第七阶段确认门决策，视同 MX 保护）", skipped_kept)
    if skipped_placeholder:
        logger.warning("[Node6] 工厂「%s」共跳过 %d 条占位行（未写入 Excel）",
                       factory, skipped_placeholder)
    if cur["manual_override_kept"]:
        logger.warning("[Node6] 工厂「%s」%d 处人工修改的单元格已保留未覆盖"
                       "（详见批次详情审计，可在详情页「覆盖写入」）",
                       factory, cur["manual_override_kept"])
    # 落盘前可写探测：让 Excel 独占锁导致的失败尽早冒泡，避免做完耗时写操作才报错
    _probe_writable(out_path)
    try:
        wb.save(out_path)
    except (PermissionError, OSError) as e:
        if _is_file_lock_error(e):
            raise RuntimeError(_format_file_busy_msg(out_path)) from e
        raise
    # 快照更新点 2（决策 5）：写回落盘后全量重扫重建快照（含 protected
    # 结转——被保留的人工值打标，被覆盖的解除）；覆盖 writer() 与
    # apply_reopen_payload 两条调用链；决策 4（阶段五）：本次纳入管理的
    # 人工新增行（A/B 态）打 origin="manual_added"
    if batch_id:
        copy_snapshot.refresh_snapshot(
            batch_id,
            mark_protected=mark_protected or None,
            unprotect=unprotect or None,
            mark_manual=(manual_merge["manual_keys"]
                         if manual_merge else None))
    return written, skipped_placeholder


def clear_sku_rows(
    state: AgentState,
    out_path: Path,
    skus: list[str],
    channel: str = "first_write",
) -> int:
    """清空指定 SKU 在下游表中的 中文品名/净重/毛重 三列单元格，返回清除的行数。

    用于 reopen 模式下人工删除条目：该 SKU 此前已写入输出 Excel，删除后
    需把已写值清空（三列本就是本系统追加的，清空即回到写前状态）。
    MX 行（PURCHASE_ORDER 以 "MX" 开头）三列为人工填写，不在清空之列。
    主库历史单重记录不动（其他批次仍可能引用，删除主库记录不在此语义内）。

    通道 A 留痕（决策 1）：channel="rework"（reopen 链路）时，被清掉的
    人工非空修改（快照×副本冲突单元格）逐单元格记
    cur["manual_overwritten_cells"]（old=人工值，new=None），由 service
    层入审计 changes_json（source="manual_value_overwritten"）。

    第七阶段 §1.3：确认门「保留」的人工行（state.manual_kept_rows）
    视同 MX 人工行，reopen 清空同样整行跳过，永不清除。
    """
    if not skus:
        return 0
    settings = get_settings()
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    row_map = (state.get("downstream_row_map") or {}).get(factory) or {}
    # 第七阶段「保留」行集（视同 MX 人工行）
    kept_row_keys = set(
        (state.get("manual_kept_rows") or {}).get(factory) or [])

    wb = load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    header = [c.value for c in ws[1]]
    cols = [header.index(settings.col_name_cn) + 1,
            header.index(settings.col_net) + 1,
            header.index(settings.col_gross) + 1]
    # MX 人工行识别列：三列是人工填的值（非本系统写入），reopen 删除不得清掉
    col_po = header.index(PO_COL_NAME) + 1 if PO_COL_NAME in header else None

    # 行标识重定位（决策 3，与 _write_excel 同口径）：row_map 行号值不消费，
    # 实际行号按 (工厂, SKU, seq) 现扫定位
    from app.orchestrator import copy_snapshot
    row_index = copy_snapshot.build_row_index(ws)

    # 通道 A 留痕：写前 diff 取快照×副本冲突（人工非空值），清空时旧值入审计
    batch_id = state.get("batch_id")
    conflict_map: dict[tuple[str, str], dict] = {}
    if channel == "rework" and batch_id:
        diff = copy_snapshot.diff_against_copy(batch_id)
        if diff is not None:
            conflict_map = {(c["row_key"], c["field"]): c
                            for c in diff.value_conflicts}
    field_by_col = {cols[0]: "name_cn", cols[1]: "net", cols[2]: "gross"}

    cleared = 0
    skipped_mx = 0
    skipped_kept = 0
    for sku in skus:
        sku_key = copy_snapshot.norm_key_text(sku)
        rows_need = len(row_map.get(str(sku), []))
        for seq in range(1, rows_need + 1):
            excel_row = row_index.get(
                (copy_snapshot.norm_key_text(factory), sku_key, seq))
            if excel_row is None:
                logger.warning(
                    "[Node6] reopen 删除清空：工厂「%s」SKU「%s」第 %d 行"
                    "在副本中未找到（可能被人工删除），跳过", factory, sku, seq)
                continue
            if col_po is not None and _is_mx_po(
                    ws.cell(row=excel_row, column=col_po).value):
                skipped_mx += 1
                continue
            key_str = copy_snapshot.row_key_str(
                (copy_snapshot.norm_key_text(factory), sku_key, seq))
            if key_str in kept_row_keys:
                # 第七阶段 §1.3：「保留」人工行视同 MX 行，reopen 清空跳过
                skipped_kept += 1
                continue
            for col in cols:
                conflict = conflict_map.get((key_str, field_by_col[col]))
                if conflict is not None:
                    # 通道 A：人工非空值随 reopen 删除直接清空，旧值留痕
                    cur.setdefault("manual_overwritten_cells", []).append({
                        "factory": factory, "sku": str(sku), "row": excel_row,
                        "field": field_by_col[col],
                        "old": conflict["copy"], "new": None,
                    })
                    cur["manual_values_overwritten"] = (
                        cur.get("manual_values_overwritten") or 0) + 1
                ws.cell(row=excel_row, column=col).value = None
            cleared += 1
    if skipped_mx:
        logger.info("[Node6] reopen 删除条目：跳过 MX 行清空 %d 行（人工已填值）",
                    skipped_mx)
    if skipped_kept:
        logger.info("[Node6] reopen 删除条目：跳过「保留」人工行清空 %d 行"
                    "（第七阶段确认门决策，视同 MX 保护）", skipped_kept)
    if cleared:
        _probe_writable(out_path)
        wb.save(out_path)
        logger.info("[Node6] reopen 删除条目：工厂「%s」清空 %d 行（SKU：%s）",
                    factory, cleared, "、".join(str(s) for s in skus))
        # 快照更新点 3（决策 5）：清空落盘后重建快照
        batch_id = state.get("batch_id")
        if batch_id:
            copy_snapshot.refresh_snapshot(batch_id)
    return cleared


def _write_short_name_audit(
    state: AgentState, factory_name: str, short_name: str, match_method: str,
) -> None:
    """C 级 short_name 自动回填留痕到 review_audits。

    审计是辅助设施：写入失败只记警告，绝不阻塞已完成的落库
    （与 service._write_audit 同哲学，try/except 包死）。
    """
    try:
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=state.get("batch_id") or "unknown",
                factory_name=factory_name,
                approved=True,
                edited_count=1,
                changes_json=json.dumps([{
                    "field": "short_name",
                    "old": None,
                    "new": short_name,
                    "source": f"auto_c_level_{match_method}",
                }], ensure_ascii=False),
                new_skus_json="[]",
                result_status="auto_short_name",
            ))
            session.commit()
    except Exception as e:  # noqa: BLE001 辅助设施失败静默降级，见 docstring
        logger.warning("[Node6] short_name 自动回填审计落库失败（主流程不受影响）："
                       "%s: %s", type(e).__name__, e)


def _auto_link_mappings(factory_name: str, new_items: list[dict]) -> None:
    """新 SKU 落库后自动挂产品映射（辅助设施，与 _write_short_name_audit 同哲学）。

    只在真·新 SKU（INSERT 分支）落库成功后触发；独立 session、独立事务，
    整体失败只记 warning，绝不影响已完成的 Excel 写入/主数据落库。
    幂等由 attach_sku_to_mapping 内部保证（SKU 已在列表则不动 + 子表唯一约束）。
    """
    try:
        with get_session() as session:
            for item in new_items:
                attach_sku_to_mapping(
                    session,
                    sku_code=str(item.get("sku") or ""),
                    name_cn=item.get("name_cn"),
                    hs_code=item.get("hs_code"),
                    inspection_required=bool(item.get("inspection_required", False)),
                    name_en=item.get("name_en"),
                    factory_name=factory_name,
                )
            session.commit()
    except Exception as e:  # noqa: BLE001 辅助设施失败静默降级，见 docstring
        logger.warning("[Node6] 新 SKU 自动挂接产品映射失败（主流程不受影响）："
                       "%s: %s", type(e).__name__, e)


def _relink_mappings(factory_name: str, relink_items: list[dict]) -> None:
    """老 SKU 人工改品名后重挂映射归属（辅助设施，与 _auto_link_mappings 同哲学）。

    只在 UPDATE 分支检测到 name_cn 变化后触发；独立 session、独立事务，
    逐个调 relink_sku_to_name（先把 SKU 从所有映射行摘除、行保留，
    再按新品名挂接），整体失败只记 warning，绝不影响已完成的
    Excel 写入/主数据落库。
    """
    try:
        with get_session() as session:
            for item in relink_items:
                relink_sku_to_name(
                    session,
                    sku_code=str(item.get("sku") or ""),
                    name_cn=item.get("name_cn"),
                    hs_code=item.get("hs_code"),
                    inspection_required=bool(item.get("inspection_required", False)),
                    name_en=item.get("name_en"),
                    factory_name=factory_name,
                )
            session.commit()
    except Exception as e:  # noqa: BLE001 辅助设施失败静默降级，见 docstring
        logger.warning("[Node6] 品名变更重挂产品映射失败（主流程不受影响）："
                       "%s: %s", type(e).__name__, e)


def _upsert_db(state: AgentState) -> tuple[int, int]:
    """主数据落库，返回 (插入数, 更新数)。"""
    cur = state.get("current_factory_data") or {}
    factory_name = cur.get("factory_name") or "未知工厂"
    inserted = updated = 0
    auto_short_name: str | None = None
    match_method = cur.get("match_method")
    new_sku_items: list[dict] = []  # 真·新 SKU（INSERT 分支），commit 后自动挂映射用
    relink_items: list[dict] = []   # 老 SKU 品名变更（UPDATE 分支），commit 后重挂映射用

    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == factory_name)
        )
        if factory is None:
            factory = Factory(factory_name=factory_name)
            session.add(factory)
            session.flush()

        # C 级自动回填：高置信匹配档 + short_name 为空 + 有 folder_path 时，
        # 取文件夹名沉淀为 short_name（新工厂自动学习）；
        # fuzzy/contains/none 不回填，走审核页建议卡由人工确认
        if (not factory.short_name
                and cur.get("folder_path")
                and match_method in _AUTO_SHORT_NAME_METHODS):
            factory.short_name = Path(cur["folder_path"]).name
            auto_short_name = factory.short_name
            logger.info("[Node6] C 级自动回填：工厂「%s」short_name=「%s」"
                        "（match_method=%s）", factory_name,
                        auto_short_name, match_method)

        for item in cur.get("calculated_items") or []:
            sku = str(item.get("sku") or "")
            if not sku:
                continue
            calc = item.get("calculation") or {}
            unit_net = calc.get("calculated_unit_net")
            unit_gross = calc.get("calculated_unit_gross")
            if unit_net is None and unit_gross is None:
                # writer 防线：净重/毛重全 None 的 SKU（提取失败占位数据）
                # 不 INSERT/UPDATE 主库，堵主库污染
                logger.warning("[Node6] 占位数据不入主库：工厂「%s」SKU「%s」"
                               "净重/毛重全空，跳过落库", factory_name, sku)
                continue

            record = session.scalar(
                select(FactorySKU).where(
                    FactorySKU.factory_id == factory.factory_id,
                    FactorySKU.sku_code == sku,
                )
            )
            if record is None:
                # 新 SKU：INSERT（含人工补录的合规字段）
                record = FactorySKU(
                    factory_id=factory.factory_id,
                    sku_code=sku,
                    name_cn=item.get("name_cn"),
                    name_en=item.get("name_en"),
                    name_jp=item.get("name_jp"),
                    hs_code=item.get("hs_code"),
                    inspection_required=bool(item.get("inspection_required", False)),
                    unit_net_weight=unit_net,
                    unit_gross_weight=unit_gross,
                )
                session.add(record)
                inserted += 1
                new_sku_items.append(item)  # 挂接字段以 item 为准（人工补录值）
            elif item.get("is_human_edited") or item.get("update_history_weight"):
                # 老 SKU：人工微调过 → UPDATE 刷新重量与合规字段；
                # 或提交时勾选了「更新历史单重」（单重波动人工确认沉淀为新基准，
                # 即使数值未编辑也刷新重量；合规字段提交值=主库现值，写回无副作用）
                old_name_cn = (record.name_cn or "").strip()  # 品名变更检测基线
                if unit_net is not None:
                    record.unit_net_weight = unit_net
                if unit_gross is not None:
                    record.unit_gross_weight = unit_gross
                for f in ("name_cn", "name_en", "name_jp", "hs_code"):
                    if item.get(f) is not None:
                        setattr(record, f, item[f])
                if item.get("inspection_required") is not None:
                    record.inspection_required = bool(item["inspection_required"])
                updated += 1
                # 品名变更 → 收进 relink_items，主 commit 后重挂映射归属；
                # name_cn 只在提交值非 None 时刷新（保持现状），故人工没改品名
                # 或只改重量时自然不会进 relink_items；继承字段以落库后现值为准
                new_name_cn = (record.name_cn or "").strip()
                if new_name_cn != old_name_cn:
                    relink_items.append({
                        "sku": sku,
                        "name_cn": record.name_cn,
                        "hs_code": record.hs_code,
                        "inspection_required": record.inspection_required,
                        "name_en": record.name_en,
                    })
        session.commit()
    if new_sku_items:
        # 自动挂接放在主落库 commit 之后：映射是辅助设施，失败不牵连主流程
        _auto_link_mappings(factory_name, new_sku_items)
    if relink_items:
        # 品名变更重挂与自动挂接同款哲学：辅助设施，失败不牵连主流程
        _relink_mappings(factory_name, relink_items)
    if auto_short_name:
        _write_short_name_audit(state, factory_name, auto_short_name,
                                match_method or "")
    return inserted, updated


def writer(state: AgentState) -> dict:
    # L2 日志关联：从 state 重绑当前工厂名（多工厂 resume 链上 submit 拷贝的
    # context 残留上一工厂）；节点独立 context 保证不外泄，无需清理
    bind_factory_from_state(state)
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")

    if state.get("validation_status") != "Approved":
        # Rejected/Skipped 等都走这里：不写 Excel、不落库、不进 factory_outputs
        logger.warning("[Node6] 工厂「%s」未获批准（%s），跳过写入",
                       factory, state.get('validation_status'))
        return {}

    out_path = _ensure_output_copy(state)
    # 通道分流（决策 1）：补充工厂确认门（通道 B）操作员确认覆写后，
    # add_factories_to_batch 把确认工厂写入 state.overwrite_manual_factories，
    # 本厂按通道 A（rework）语义覆盖人工修改；其余走通道 C 默认跳过保留。
    # 消费后自清（防止残留确认污染后续写入）。
    overwrite_facs = set(state.get("overwrite_manual_factories") or [])
    channel = "rework" if factory in overwrite_facs else "first_write"

    # 第七阶段 §1.3/§2.4 遇值确认门：写入前预检——目标格已有非空值且
    # 与系统值不同（MX 行除外）→ 整厂一次 interrupt 挂起确认。
    # 通道 A（rework）操作员已在补充工厂确认门确认过覆写，不再重复打扰。
    # 注意 interrupt 语义：挂起后恢复执行时本节点从头重跑，预检在副本
    # 未被写入的前提下结果一致，interrupt() 直接返回 resume 值；
    # 零容错默认——决策缺失/无法识别一律按「保留」处理。
    conflict_decision: str | None = None
    gate_cells: set[tuple[str, str]] | None = None
    if channel != "rework":
        conflicts, gate_cells = _collect_overwrite_conflicts(state, out_path)
        if conflicts:
            logger.warning(
                "[Node6] 工厂「%s」检测到 %d 行人工已填值与系统值冲突，"
                "挂起等待整厂确认（覆盖/保留）", factory, len(conflicts))
            answer = interrupt({
                "type": "overwrite_confirm",
                "factory": factory,
                "conflicts": conflicts,
            })
            raw = answer.get("decision") if isinstance(answer, dict) else None
            conflict_decision = "overwrite" if raw == "overwrite" else "keep"
            logger.info("[Node6] 工厂「%s」遇值确认门决策：%s",
                        factory, conflict_decision)

    written, skipped_placeholder = _write_excel(
        state, out_path, channel=channel,
        conflict_decision=conflict_decision, gate_cells=gate_cells)
    inserted, updated = _upsert_db(state)

    logger.info("[Node6] 工厂「%s」：写入 %d 行 Excel（跳过占位 %d 条）；"
                "落库 INSERT %d / UPDATE %d",
                factory, written, skipped_placeholder, inserted, updated)

    # 占位行跳过计数随快照入 factory_outputs/state，供 Node7 摘要统计
    cur["skipped_placeholder_rows"] = skipped_placeholder
    # 决策 4（阶段五）：取出人工新增行并入计划（不进 factory_outputs 快照）
    manual_merge = cur.pop("_manual_added_merge", None)
    factory_outputs = state.get("factory_outputs") or {}
    # 存完整 current_factory_data 快照（reopen 时全文恢复，与首次审核字段一致）；
    # 旧格式列表（仅 calculated_items）由 reopen 层兼容读取
    factory_outputs[factory] = dict(cur)
    result = {"final_output_path": str(out_path),
              "factory_outputs": factory_outputs}
    if conflict_decision == "keep" and gate_cells:
        # 第七阶段 §2.4「保留」决策落账：冲突行键并入
        # state.manual_kept_rows（checkpoint 持久化），后续写回/reopen
        # 清空均按视同 MX 人工行跳过（_write_excel / clear_sku_rows 消费）
        kept_keys = sorted({k for k, _f in gate_cells})
        merged_kept = {f: list(v) for f, v in
                       (state.get("manual_kept_rows") or {}).items()}
        merged_kept[factory] = sorted(
            set(merged_kept.get(factory) or []) | set(kept_keys))
        result["manual_kept_rows"] = merged_kept
        logger.info("[Node6] 工厂「%s」%d 行人工值已标记「保留」"
                    "（视同 MX 人工行保护）", factory, len(kept_keys))
    if manual_merge is not None:
        # A 态需求追加 / B 态新厂入批经节点返回值合并进 state（B 厂进
        # pending 后 _route_after_writer 回 Node2，走既有文件夹匹配→提取
        # →审核→缺资料跳过/factory_watch 全链路，零新增机制）。
        # 延迟导入：app.api.service 模块级 import app.graph → writer，
        # 只能函数内反向引用
        from app.api.service import _merge_factories_into_state
        result.update(_merge_factories_into_state(
            state,
            manual_merge["req_updates"],
            manual_merge["row_map_updates"],
            pending_add=manual_merge["b_factories"]))
    if factory in overwrite_facs:
        # 确认标记消费后自清（其余未消费工厂保留）
        result["overwrite_manual_factories"] = sorted(overwrite_facs - {factory})
    return result
