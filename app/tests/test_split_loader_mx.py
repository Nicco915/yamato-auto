# -*- coding: utf-8 -*-
"""loader 的 MX 货物行级识别（RawItem.is_mx）单测。

用 openpyxl 在 tmp_path 现造 filled Excel，不依赖真实生产文件。
只 import app.split.loader（其依赖仅 schemas + openpyxl），
不触 settings 单例、不起子进程，无需 YAMATO_DOTENV_PATH 隔离。
"""

from __future__ import annotations

import logging

import openpyxl
import pytest

from app.split.loader import load_filled_excel
from app.split.schemas import RawItem

# loader 必需列（与 app/split/loader.py REQUIRED 保持一致）
_REQUIRED_HEADERS = [
    "KANRI_NO", "MINATO_MEI_KJ", "CONTAINER_MEI", "MAKER_MEI_KJ",
    "SHOHIN_CD", "净重", "毛重", "SOTOBAKO_D_HACCHU_SU",
    "中文品名", "D_HACCHU_SU", "KAKAKUKEI", "TSUKA_MEI",
    "M3",
]


def _write_filled_excel(path, rows, with_po_column: bool = True):
    """造一个最小 filled Excel：表头 + 若干数据行。

    rows: list of (kanri_no, po_value)；po_value=None 表示单元格留空。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    headers = list(_REQUIRED_HEADERS)
    if with_po_column:
        headers.append("PURCHASE_ORDER")
    ws.append(headers)
    for kanri_no, po_value in rows:
        row = [""] * len(headers)
        row[headers.index("KANRI_NO")] = kanri_no
        row[headers.index("MINATO_MEI_KJ")] = "東京港"
        row[headers.index("CONTAINER_MEI")] = "40HQ"
        row[headers.index("MAKER_MEI_KJ")] = "某工厂"
        row[headers.index("SHOHIN_CD")] = "SKU-1"
        if with_po_column and po_value is not None:
            row[headers.index("PURCHASE_ORDER")] = po_value
        ws.append(row)
    wb.save(path)
    wb.close()


class TestIsMxFlag:
    """PURCHASE_ORDER 前缀识别 → RawItem.is_mx。"""

    def _load_single(self, tmp_path, po_value, with_po_column=True):
        f = tmp_path / "filled.xlsx"
        _write_filled_excel(f, [("K001", po_value)], with_po_column)
        items = load_filled_excel(f)
        assert len(items) == 1
        return items[0]

    def test_mx_po_is_true(self, tmp_path):
        item = self._load_single(tmp_path, "MX2-268510-001")
        assert item.is_mx is True

    def test_plain_po_is_false(self, tmp_path):
        item = self._load_single(tmp_path, "272752")
        assert item.is_mx is False

    def test_none_po_is_false(self, tmp_path):
        """PO 单元格为空 → is_mx=False，不报错。"""
        item = self._load_single(tmp_path, None)
        assert item.is_mx is False

    def test_lowercase_mx_is_true(self, tmp_path):
        item = self._load_single(tmp_path, "mx1-001")
        assert item.is_mx is True

    def test_missing_po_column_all_false(self, tmp_path, caplog):
        """老文件没有 PURCHASE_ORDER 列：不报错，is_mx 全 False，记 warning。"""
        f = tmp_path / "filled.xlsx"
        _write_filled_excel(f, [("K001", None), ("K002", None)],
                            with_po_column=False)
        with caplog.at_level(logging.WARNING, logger="app.split.loader"):
            items = load_filled_excel(f)
        assert len(items) == 2
        assert all(i.is_mx is False for i in items)
        assert any("PURCHASE_ORDER" in r.message for r in caplog.records)

    def test_is_mx_survives_state_roundtrip(self, tmp_path):
        """is_mx 经 model_dump → RawItem(**d) 序列化往返不丢失。"""
        f = tmp_path / "filled.xlsx"
        _write_filled_excel(f, [("K001", "MX2-268510-001"), ("K002", "272752")])
        items = load_filled_excel(f)
        restored = [RawItem(**d) for d in (i.model_dump() for i in items)]
        assert [r.is_mx for r in restored] == [True, False]

    def test_is_mx_default_false_for_old_payload(self):
        """旧 state 载荷无 is_mx 键 → 反序列化默认 False（向后兼容）。"""
        item = RawItem(
            kanri_no="K001", port="東京港", container_type="40HQ",
            maker="某工厂", sku="SKU-1",
            net_weight=None, gross_weight=None, pcs=None,
        )
        assert item.is_mx is False
