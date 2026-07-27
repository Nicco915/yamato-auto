"""Node 6: 写入与循环（Writer Node）——双写机制（《第三阶段.md》改造点 C）。

1. 写业务表：把人工确认后的精确数据写回下游 Excel 副本
   （先复制到 output/ 目录，绝不覆盖原件）；
   每个 (工厂, SKU) 可能对应多行，按行分摊：行净重 = 单件净重 × 该行发注数量。
2. 写数据库（Upsert）：
   - 新 SKU：INSERT 人工补录的多语言品名/HS 编码/单件重量；
   - 老 SKU：人工微调过重量时 UPDATE 刷新历史重量。
"""
import shutil
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Factory, FactorySKU
from app.db.session import get_session
from app.state import AgentState


def _ensure_output_copy(state: AgentState) -> Path:
    """确保 output/ 下存在原件副本；已存在则复用（多工厂循环写同一个文件）。"""
    settings = get_settings()
    src = Path(state["downstream_file_path"])
    dst = settings.output_dir_abs / f"{src.stem}_filled{src.suffix}"
    if not dst.exists():
        shutil.copy2(src, dst)
        print(f"[Node6] 已复制原件到 {dst}（绝不覆盖原件）")
    return dst


def _write_excel(state: AgentState, out_path: Path) -> int:
    """按行号精准写回 净重/毛重 单元格，返回写入的单元格数量。"""
    settings = get_settings()
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    row_map = (state.get("downstream_row_map") or {}).get(factory) or {}

    # 用 pandas 取每行的发注数量（行号 -> 数量）
    df = pd.read_excel(
        state["downstream_file_path"], sheet_name=0,
        dtype={settings.col_sku: str}, usecols=[settings.col_qty],
    )

    wb = load_workbook(out_path)
    ws = wb[wb.sheetnames[0]]
    header = [c.value for c in ws[1]]
    col_net = header.index(settings.col_net) + 1
    col_gross = header.index(settings.col_gross) + 1

    written = 0
    for item in cur.get("calculated_items") or []:
        calc = item.get("calculation") or {}
        unit_net = calc.get("calculated_unit_net")
        unit_gross = calc.get("calculated_unit_gross")
        if unit_net is None and unit_gross is None:
            continue  # Error 项无有效单重，留给人工线下处理
        for excel_row in row_map.get(str(item.get("sku")), []):
            qty = df.iloc[excel_row - 2][settings.col_qty]
            qty = float(qty) if pd.notna(qty) else 0.0
            if unit_net is not None:
                ws.cell(row=excel_row, column=col_net, value=round(unit_net * qty, 3))
            if unit_gross is not None:
                ws.cell(row=excel_row, column=col_gross, value=round(unit_gross * qty, 3))
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
