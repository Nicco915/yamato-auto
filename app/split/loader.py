# -*- coding: utf-8 -*-
"""读取 filled ContentsOfTheContainer Excel，返回 list[RawItem]。"""

from __future__ import annotations

import logging
from pathlib import Path

import openpyxl

from app.split.schemas import RawItem

logger = logging.getLogger(__name__)


def load_filled_excel(path: str | Path,
                      skipped_kanri_rows: list[int] | None = None
                      ) -> list[RawItem]:
    """读取 filled ContentsOfTheContainer，只取有数据的行（跳过表头）。

    按表头列名定位（列顺序变化不影响），所需列：
      KANRI_NO、MINATO_MEI_KJ、CONTAINER_MEI、MAKER_MEI_KJ、
      SHOHIN_CD、净重、毛重、SOTOBAKO_D_HACCHU_SU、
      中文品名、D_HACCHU_SU、KAKAKUKEI、TSUKA_MEI（后 4 列供报关生成）

    表头在第 1 行，数据从第 2 行开始。跳过 KANRI_NO 为空的行。
    缺列时抛 ValueError。

    可选列：PURCHASE_ORDER（MX 货物识别，形如 MX2-268510-001）。
    缺该列不报错，is_mx 全部按 False 处理并记 warning。
    可选列：SOTOBAKO_HABA / SOTOBAKO_OKUYUKI / SOTOBAKO_TAKASA
    （外箱长宽高 cm，供分体积统计公式）。缺列不报错，对应字段为 None。

    决策 4（阶段五）：KANRI_NO 为空的行从「静默 continue」改「continue +
    计数 warning」——只数带业务数据的行（工厂/SKU/数量任一非空），纯排版
    空行不计；skipped_kanri_rows 传入列表时追加被跳过的行号（分票图
    load_filled 节点据此把 kanri_missing_rows 写进 SplitState 透出）。
    """
    REQUIRED = [
        "KANRI_NO", "MINATO_MEI_KJ", "CONTAINER_MEI", "MAKER_MEI_KJ",
        "SHOHIN_CD", "净重", "毛重", "SOTOBAKO_D_HACCHU_SU",
        "中文品名", "D_HACCHU_SU", "KAKAKUKEI", "TSUKA_MEI",
        "M3",
    ]
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]
    missing = [n for n in REQUIRED if n not in header]
    if missing:
        wb.close()
        raise ValueError(f"filled Excel 缺少必需列: {missing}")
    col = {name: header.index(name) + 1 for name in REQUIRED}  # 1-based

    # 可选列 PURCHASE_ORDER：存在才读，不存在则 is_mx 全 False（兼容老文件）
    if "PURCHASE_ORDER" in header:
        col["PURCHASE_ORDER"] = header.index("PURCHASE_ORDER") + 1
    else:
        logger.warning(
            "load_filled_excel: %s 缺少可选列 PURCHASE_ORDER，is_mx 全部按 False 处理",
            path,
        )

    # 可选列 外箱长宽高（分体积统计用）：存在才读，缺失则对应字段 None
    _DIM_COLS = ("SOTOBAKO_HABA", "SOTOBAKO_OKUYUKI", "SOTOBAKO_TAKASA")
    missing_dims = [n for n in _DIM_COLS if n not in header]
    for n in _DIM_COLS:
        if n in header:
            col[n] = header.index(n) + 1
    if missing_dims:
        logger.warning(
            "load_filled_excel: %s 缺少可选列 %s，外箱尺寸字段全部为 None",
            path, missing_dims,
        )

    def num(v, as_int=False):
        if v is None:
            return None
        try:
            return int(v) if as_int else float(v)
        except (ValueError, TypeError):
            return None

    items: list[RawItem] = []
    skipped_kanri: list[int] = []   # 缺管理番号被跳过的数据行（计数 warning）
    for row_idx in range(2, ws.max_row + 1):
        kanri_val = ws.cell(row=row_idx, column=col["KANRI_NO"]).value
        if kanri_val is None or not str(kanri_val).strip():
            # 决策 4（阶段五）：只数带业务数据的行（工厂/SKU/数量任一非空），
            # 纯排版空行不算「缺管理番号」
            probe = (ws.cell(row=row_idx, column=col["MAKER_MEI_KJ"]).value,
                     ws.cell(row=row_idx, column=col["SHOHIN_CD"]).value,
                     ws.cell(row=row_idx,
                             column=col["SOTOBAKO_D_HACCHU_SU"]).value)
            if any(v is not None and str(v).strip() for v in probe):
                skipped_kanri.append(row_idx)
            continue
        kanri_no = str(kanri_val).strip()

        def text(name):
            v = ws.cell(row=row_idx, column=col[name]).value
            return str(v).strip() if v is not None else ""

        po_val = (
            ws.cell(row=row_idx, column=col["PURCHASE_ORDER"]).value
            if "PURCHASE_ORDER" in col else None
        )
        is_mx = (
            str(po_val).strip().upper().startswith("MX")
            if po_val is not None else False
        )

        def dim(name):
            """可选外箱尺寸列：列不存在或值为空/非法 → None。"""
            if name not in col:
                return None
            return num(ws.cell(row=row_idx, column=col[name]).value)

        items.append(RawItem(
            kanri_no=kanri_no,
            port=text("MINATO_MEI_KJ"),
            container_type=text("CONTAINER_MEI"),
            maker=text("MAKER_MEI_KJ"),
            sku=text("SHOHIN_CD"),
            net_weight=num(ws.cell(row=row_idx, column=col["净重"]).value),
            gross_weight=num(ws.cell(row=row_idx, column=col["毛重"]).value),
            pcs=num(ws.cell(row=row_idx, column=col["SOTOBAKO_D_HACCHU_SU"]).value, as_int=True),
            name_cn=text("中文品名"),
            qty_pieces=num(ws.cell(row=row_idx, column=col["D_HACCHU_SU"]).value, as_int=True),
            amount=num(ws.cell(row=row_idx, column=col["KAKAKUKEI"]).value),
            currency=text("TSUKA_MEI"),
            m3=num(ws.cell(row=row_idx, column=col["M3"]).value),
            is_mx=is_mx,
            carton_width=dim("SOTOBAKO_HABA"),
            carton_depth=dim("SOTOBAKO_OKUYUKI"),
            carton_height=dim("SOTOBAKO_TAKASA"),
        ))

    wb.close()
    if skipped_kanri:
        logger.warning(
            "load_filled_excel: %s 有 %d 行缺管理番号（KANRI_NO），"
            "已跳过未参与分票（行号 %s）",
            path, len(skipped_kanri), skipped_kanri,
        )
        if skipped_kanri_rows is not None:
            skipped_kanri_rows.extend(skipped_kanri)
    return items