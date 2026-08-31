# -*- coding: utf-8 -*-
"""SKU 高亮定位纯函数 _find_text_hits 单元测试。

覆盖：
1. 命中页码正确（条码在第 2 页 → page=2，第 1 页不出现在结果里）。
2. 分数坐标全部落在 [0,1] 且 rects 非空。
3. 搜索不存在的文本 → 空列表（不抛异常）。
4. 整行扩展：返回 rect 的 x 范围必须横向包含 search_for 的直接命中范围
   （x0 <= 直接命中 x0 且 x1 >= 直接命中 x1），且 y 带保持原样不纵向扩展。
5. 无文本层的页（图片页）不抛错、返回空列表。

隔离：纯函数测试，只写 tempfile 临时 PDF，不碰 db/.env，
不需要 isolate_to_tmp（不 import 提取线）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import fitz
import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.review.router import _find_text_hits  # noqa: E402

BARCODE = "4549509703198"


@pytest.fixture()
def two_page_pdf() -> str:
    """两页 PDF：第 1 页无条码，第 2 页一行含 13 位条码的单据行。"""
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "COMMERCIAL INVOICE (no barcode here)", fontsize=11)
    p2 = doc.new_page()
    # 一行完整单据行：行首文本 ... 条码 ... 行尾数值（验证整行横向扩展）
    p2.insert_text(
        (72, 200),
        f"POPE  1.5MM*100M  {BARCODE}  480  USD 264.960",
        fontsize=11,
    )
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp = f.name
    doc.save(tmp)
    doc.close()
    yield tmp
    Path(tmp).unlink(missing_ok=True)


def test_hit_page_number_correct(two_page_pdf):
    hits = _find_text_hits(two_page_pdf, BARCODE)
    assert len(hits) == 1
    assert hits[0]["page"] == 2, f"条码在第 2 页，实际命中: {hits}"
    assert len(hits[0]["rects"]) == 1


def test_fraction_coords_in_unit_range(two_page_pdf):
    hits = _find_text_hits(two_page_pdf, BARCODE)
    for h in hits:
        for rect in h["rects"]:
            assert len(rect) == 4
            assert all(0.0 <= v <= 1.0 for v in rect), f"分数坐标越界: {rect}"
            assert rect[2] > rect[0] and rect[3] > rect[1], f"矩形退化: {rect}"


def test_no_match_returns_empty(two_page_pdf):
    assert _find_text_hits(two_page_pdf, "9999999999999") == []


def test_row_expansion_covers_direct_hit(two_page_pdf):
    """整行扩展：x 范围必须包含直接命中范围（行首 POPE 到行尾金额），y 不扩。"""
    doc = fitz.open(two_page_pdf)
    page = doc[1]
    direct = page.search_for(BARCODE)[0]
    pw, ph = page.rect.width, page.rect.height
    doc.close()

    hits = _find_text_hits(two_page_pdf, BARCODE)
    rect = hits[0]["rects"][0]
    x0, y0, x1, y1 = rect
    # 横向包含直接命中
    assert x0 <= direct.x0 / pw + 1e-4, f"x0 {x0} 未覆盖直接命中 {direct.x0 / pw}"
    assert x1 >= direct.x1 / pw - 1e-4, f"x1 {x1} 未覆盖直接命中 {direct.x1 / pw}"
    # 确实扩到了行首文本（POPE 在条码左侧）——扩展后 x0 应显著小于直接命中 x0
    assert x0 < direct.x0 / pw - 0.01, "未发生横向整行扩展"
    # y 带保持命中原样（不纵向扩展）
    assert abs(y0 - direct.y0 / ph) < 1e-3
    assert abs(y1 - direct.y1 / ph) < 1e-3


def test_image_only_pdf_returns_empty():
    """无文本层（模拟扫描件）：空列表，不抛异常。"""
    doc = fitz.open()
    doc.new_page()  # 空白页，无文本
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp = f.name
    doc.save(tmp)
    doc.close()
    try:
        assert _find_text_hits(tmp, BARCODE) == []
    finally:
        Path(tmp).unlink(missing_ok=True)
