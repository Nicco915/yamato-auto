# -*- coding: utf-8 -*-
"""读取 filled ContentsOfTheContainer Excel，返回 list[RawItem]。"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from app.split.schemas import RawItem


def load_filled_excel(path: str | Path) -> list[RawItem]:
    """读取 filled ContentsOfTheContainer，只取有数据的行（跳过表头）。

    字段映射（1-based 列号）：
      KANRI_NO(1)、MINATO_MEI_KJ(19)、CONTAINER_MEI(12)、
      MAKER_MEI_KJ(25)、SHOHIN_CD(29)、净重(33)、毛重(34)、
      SOTOBAKO_D_HACCHU_SU(36)

    表头在第 1 行，数据从第 2 行开始。跳过 KANRI_NO 为空的行。
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    items: list[RawItem] = []
    for row_idx in range(2, ws.max_row + 1):
        kanri_no = ws.cell(row=row_idx, column=1).value
        if kanri_no is None:
            continue
        kanri_no = str(kanri_no).strip()
        if not kanri_no:
            continue

        # Port (col 19)
        port_val = ws.cell(row=row_idx, column=19).value
        port = str(port_val).strip() if port_val is not None else ""

        # Container type (col 12)
        ctype_val = ws.cell(row=row_idx, column=12).value
        container_type = str(ctype_val).strip() if ctype_val is not None else ""

        # Maker (col 25)
        maker_val = ws.cell(row=row_idx, column=25).value
        maker = str(maker_val).strip() if maker_val is not None else ""

        # SKU (col 29)
        sku_val = ws.cell(row=row_idx, column=29).value
        sku = str(sku_val).strip() if sku_val is not None else ""

        # 净重 (col 33)
        nw_val = ws.cell(row=row_idx, column=33).value
        if nw_val is not None:
            try:
                net_weight = float(nw_val)
            except (ValueError, TypeError):
                net_weight = None
        else:
            net_weight = None

        # 毛重 (col 34)
        gw_val = ws.cell(row=row_idx, column=34).value
        if gw_val is not None:
            try:
                gross_weight = float(gw_val)
            except (ValueError, TypeError):
                gross_weight = None
        else:
            gross_weight = None

        # 件数 (col 36)
        pcs_val = ws.cell(row=row_idx, column=36).value
        if pcs_val is not None:
            try:
                pcs = int(pcs_val)
            except (ValueError, TypeError):
                pcs = None
        else:
            pcs = None

        items.append(RawItem(
            kanri_no=kanri_no,
            port=port,
            container_type=container_type,
            maker=maker,
            sku=sku,
            net_weight=net_weight,
            gross_weight=gross_weight,
            pcs=pcs,
        ))

    wb.close()
    return items