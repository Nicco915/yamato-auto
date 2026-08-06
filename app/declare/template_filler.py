# -*- coding: utf-8 -*-
"""报关单模版填充——复制模版后写入票头、明细、汇总，纯 openpyxl 操作。"""

from __future__ import annotations

import shutil
from pathlib import Path

import openpyxl

from app.declare.aggregator import DetailRow

DETAIL_START_ROW = 18  # 明细从第 18 行起（第 17 行为表头）


def fill_declaration(
    template_path: str,
    out_path: str,
    *,
    ticket_name: str,        # '东京A票'
    invoice_no: str,         # 'YILT656'（调用方拼好）
    onboard: str,            # '2026.7.25'
    port_to_en: str,         # 'TOKYO'
    set_subtotal: tuple[float, float] | None,  # (金额, 净重) 或 None
    rows: list[DetailRow],
) -> None:
    """填充报关单模版并另存为 out_path。

    版式（对照人工样本）：
      J1=票名、A2='INVOICE NO:xxx'、G12=开船日、H13='QINGDAO'、H14=目的港英文
      F16/G16=组套小计（仅 set_subtotal 非 None 时填）
      18 行起逐行明细：A=seq B=品名 C=箱数 D=件数 E=币制 F=金额 G=净重 H=毛重
                       I='商检'（inspection 时） J=计量单位代码；None 的格子留空
      明细后空 1 行，再两行式汇总：
        JPY 行：C=总箱数 D=总件数 E='JPY' G=总净重 H=总毛重
        USD 行：E='USD' F=总金额
    """
    shutil.copy(template_path, out_path)
    wb = openpyxl.load_workbook(out_path)
    ws = wb.active

    ws["J1"] = ticket_name
    ws["A2"] = f"INVOICE NO:{invoice_no}"
    ws["G12"] = onboard
    ws["H13"] = "QINGDAO"
    ws["H14"] = port_to_en

    if set_subtotal is not None:
        ws["F16"] = set_subtotal[0]
        ws["G16"] = set_subtotal[1]

    # TODO: 明细行/汇总行目前使用默认样式；如需与样本一致，可复制第 17 行
    # （表头行）的字体/边框到明细与汇总区域。
    r = DETAIL_START_ROW
    for row in rows:
        ws.cell(row=r, column=1, value=row.seq)
        ws.cell(row=r, column=2, value=row.name_cn)
        if row.cartons is not None:
            ws.cell(row=r, column=3, value=row.cartons)
        if row.pieces is not None:
            ws.cell(row=r, column=4, value=row.pieces)
        ws.cell(row=r, column=5, value=row.currency or "USD")
        if row.amount is not None:
            ws.cell(row=r, column=6, value=row.amount)
        if row.net is not None:
            ws.cell(row=r, column=7, value=row.net)
        if row.gross is not None:
            ws.cell(row=r, column=8, value=row.gross)
        if row.inspection:
            ws.cell(row=r, column=9, value="商检")
        if row.unit_code:
            ws.cell(row=r, column=10, value=row.unit_code)
        r += 1

    # 空 1 行后写两行式汇总；总箱数/净重/毛重 = 各行非 None 值求和
    r += 1
    total_cartons = sum(x.cartons for x in rows if x.cartons is not None)
    total_pieces = sum(x.pieces for x in rows if x.pieces is not None)
    total_amount = sum(x.amount for x in rows if x.amount is not None)
    total_net = sum(x.net for x in rows if x.net is not None)
    total_gross = sum(x.gross for x in rows if x.gross is not None)

    # JPY 行（无金额列）
    ws.cell(row=r, column=3, value=total_cartons)
    ws.cell(row=r, column=4, value=total_pieces)
    ws.cell(row=r, column=5, value="JPY")
    ws.cell(row=r, column=7, value=total_net)
    ws.cell(row=r, column=8, value=total_gross)
    r += 1
    # USD 行（仅金额）
    ws.cell(row=r, column=5, value="USD")
    ws.cell(row=r, column=6, value=total_amount)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    wb.close()
