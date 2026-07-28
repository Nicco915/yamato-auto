"""Node 6: 写入与循环（Writer Node）——双写机制（《第三阶段.md》改造点 C）。

1. 写业务表：把人工确认后的精确数据写回下游 Excel 副本
   （先复制到 output/ 目录，绝不覆盖原件）；
   写入规则（2026-07-28 用户定）：
   - 原文件没有 中文品名/净重/毛重 三列：首次写入时在 SHOHIN_MEI_E 后插入
     （与既有填好文件布局一致：第 32/33/34 列），已存在则跳过；
   - 净重 = 单件净重 × SOTOBAKO_D_HACCHU_SU，毛重同理，2 位小数；
   - 中文品名 = 主数据 name_cn（新 SKU 经 Node5 人工补录）；
   - 表格格式严格不变：全程 openpyxl，禁止 pandas 写入。
2. 写数据库（Upsert）：
   - 新 SKU：INSERT 人工补录的多语言品名/HS 编码/单件重量；
   - 老 SKU：人工微调过重量时 UPDATE 刷新历史重量。
"""
import shutil
from copy import copy
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Factory, FactorySKU
from app.db.session import get_session
from app.state import AgentState

# 待添加的三列（插入到 SHOHIN_MEI_E 之后，与既有填好文件布局一致）
NEW_COL_NAMES = ("中文品名", "净重", "毛重")
INSERT_AFTER_COL = "SHOHIN_MEI_E"


def _ensure_output_copy(state: AgentState) -> Path:
    """确保 output/ 下存在原件副本；已存在则复用（多工厂循环写同一个文件）。"""
    settings = get_settings()
    src = Path(state["downstream_file_path"])
    dst = settings.output_dir_abs / f"{src.stem}_filled{src.suffix}"
    if not dst.exists():
        shutil.copy2(src, dst)
        print(f"[Node6] 已复制原件到 {dst}（绝不覆盖原件）")
    return dst


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
    print(f"[Node6] 原文件无 {list(NEW_COL_NAMES)} 三列，已在 {INSERT_AFTER_COL} 后插入")


def _write_excel(state: AgentState, out_path: Path) -> int:
    """按行号精准写回 中文品名/净重/毛重 单元格，返回写入的行数。"""
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

    written = 0
    for item in cur.get("calculated_items") or []:
        calc = item.get("calculation") or {}
        unit_net = calc.get("calculated_unit_net")
        unit_gross = calc.get("calculated_unit_gross")
        name_cn = item.get("name_cn") or (item.get("db_record") or {}).get("name_cn")
        if unit_net is None and unit_gross is None and not name_cn:
            continue  # Error 项无有效单重，留给人工线下处理
        for excel_row in row_map.get(str(item.get("sku")), []):
            qty = ws.cell(row=excel_row, column=col_qty).value
            qty = float(qty) if isinstance(qty, (int, float)) else 0.0
            if name_cn:
                ws.cell(row=excel_row, column=col_cn, value=name_cn)
            if unit_net is not None:
                ws.cell(row=excel_row, column=col_net, value=round(unit_net * qty, 2))
            if unit_gross is not None:
                ws.cell(row=excel_row, column=col_gross, value=round(unit_gross * qty, 2))
            written += 1
    wb.save(out_path)
    return written


def _upsert_db(state: AgentState) -> tuple[int, int]:
    """主数据落库，返回 (插入数, 更新数)。"""
    cur = state.get("current_factory_data") or {}
    factory_name = cur.get("factory_name") or "未知工厂"
    inserted = updated = 0

    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == factory_name)
        )
        if factory is None:
            factory = Factory(factory_name=factory_name)
            session.add(factory)
            session.flush()

        for item in cur.get("calculated_items") or []:
            sku = str(item.get("sku") or "")
            if not sku:
                continue
            calc = item.get("calculation") or {}
            unit_net = calc.get("calculated_unit_net")
            unit_gross = calc.get("calculated_unit_gross")

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
            elif item.get("is_human_edited"):
                # 老 SKU 且人工微调过：UPDATE 刷新重量与合规字段
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
        session.commit()
    return inserted, updated


def writer(state: AgentState) -> dict:
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")

    if state.get("validation_status") != "Approved":
        print(f"[Node6] 工厂「{factory}」审核未通过（{state.get('validation_status')}），跳过写入")
        return {}

    out_path = _ensure_output_copy(state)
    written = _write_excel(state, out_path)
    inserted, updated = _upsert_db(state)

    print(f"[Node6] 工厂「{factory}」：写入 {written} 行 Excel；"
          f"落库 INSERT {inserted} / UPDATE {updated}")

    return {"final_output_path": str(out_path)}
