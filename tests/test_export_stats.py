# -*- coding: utf-8 -*-
"""F2 报关单统计导出（截单信息-分体积）单元测试。

覆盖：
1. 整柜票体积 = M3 原值；
2. 一柜拆两票：第一票公式三位小数、第二票 = M3 − 第一票（同柜两票和 = M3）；
3. MX 票体积留空、件/PSC 照填；
4. 缺长宽高行跳过 + 中文警告；
5. 余额为负 → 警告仍输出数值；
6. sheet 按港口分组、票按票号排序；
另附：无提案 ValueError、无装箱单 FileNotFoundError、净重组空输出空单元格。

运行方式（在 app/ 目录下）：
    PYTHONPATH=. python3 -m pytest tests/test_export_stats.py -q

隔离（血泪红线 2026-08-11）：先 import 全部 app 模块，再 isolate_to_tmp；
output/DB 全部落在临时目录，绝不碰真实 app/output 与 master.db。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- env 前置（需在 import app 之前）----
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

import openpyxl  # noqa: E402
import pytest  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Declaration  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.export.stats import generate_split_stats  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_export_stats_test_")

# filled Excel 列（必需 13 列 + 可选 PO/长宽高列）
COLUMNS = [
    "KANRI_NO", "MINATO_MEI_KJ", "CONTAINER_MEI", "MAKER_MEI_KJ",
    "SHOHIN_CD", "净重", "毛重", "SOTOBAKO_D_HACCHU_SU",
    "中文品名", "D_HACCHU_SU", "KAKAKUKEI", "TSUKA_MEI", "M3",
    "PURCHASE_ORDER", "SOTOBAKO_HABA", "SOTOBAKO_OKUYUKI", "SOTOBAKO_TAKASA",
]

# 标准外箱尺寸：10 件 × 50×40×30 cm = 0.6 m³
W, D, H = 50, 40, 30


def _row(kanri: str, port: str, maker: str, *, pcs=10, pieces=100,
         net=5.5, gross=6.6, m3=1.0, po="PO-001", w=W, d=D, h=H,
         ctype="40HQ", sku="4900000000001") -> dict:
    return {
        "KANRI_NO": kanri, "MINATO_MEI_KJ": port, "CONTAINER_MEI": ctype,
        "MAKER_MEI_KJ": maker, "SHOHIN_CD": sku, "净重": net, "毛重": gross,
        "SOTOBAKO_D_HACCHU_SU": pcs, "中文品名": "测试品",
        "D_HACCHU_SU": pieces, "KAKAKUKEI": 100.0, "TSUKA_MEI": "USD",
        "M3": m3, "PURCHASE_ORDER": po,
        "SOTOBAKO_HABA": w, "SOTOBAKO_OKUYUKI": d, "SOTOBAKO_TAKASA": h,
    }


def _make_filled(batch_id: str, rows: list[dict]) -> Path:
    """在隔离 output 下造 containers/{batch}_filled.xlsx。"""
    containers_dir = get_settings().batch_containers_dir(batch_id)
    containers_dir.mkdir(parents=True, exist_ok=True)
    path = containers_dir / f"{batch_id}_filled.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(COLUMNS)
    for r in rows:
        ws.append([r.get(c) for c in COLUMNS])
    wb.save(path)
    return path


def _add_decl(split_id: str, ticket_no: str, port: str, items: list[dict],
              *, status: str = "pending", version: int = 1,
              ctype: str = "40HQ") -> None:
    with get_session() as s:
        s.add(Declaration(
            split_thread_id=split_id, ticket_no=ticket_no, port=port,
            container_type=ctype, items=items, sj_factories=[],
            status=status, version=version,
        ))
        s.commit()


def _full_item(kanri: str) -> dict:
    return {"kanri_no": kanri, "factory_filter": None,
            "factory_exclude": None, "is_partial": False,
            "inspection_filter": None}


def _partial_item(kanri: str, factory: str) -> dict:
    return {"kanri_no": kanri, "factory_filter": factory,
            "factory_exclude": None, "is_partial": True,
            "inspection_filter": None}


def _blocks(ws) -> list[tuple[str, list[list]]]:
    """把 sheet 解析为 [(票号, [明细行 7 列...]), ...]（空行分隔 block）。"""
    blocks = []
    r = 1
    while r <= ws.max_row:
        title = ws.cell(row=r, column=1).value
        if title is None:
            r += 1
            continue
        assert ws.cell(row=r + 1, column=1).value == "管理号", \
            f"票 {title} 缺表头行"
        details = []
        rr = r + 2
        while rr <= ws.max_row and ws.cell(row=rr, column=1).value is not None:
            details.append([ws.cell(row=rr, column=c).value
                            for c in range(1, 8)])
            rr += 1
        blocks.append((title, details))
        r = rr + 1
    return blocks


def _read_blocks(result: dict) -> dict[str, list[tuple[str, list[list]]]]:
    wb = openpyxl.load_workbook(result["file_path"])
    out = {}
    for name in wb.sheetnames:
        out[name] = _blocks(wb[name])
    wb.close()
    return out


def _sheetnames(result: dict) -> list[str]:
    wb = openpyxl.load_workbook(result["file_path"], read_only=True)
    names = list(wb.sheetnames)
    wb.close()
    return names


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_full_container_volume_is_m3():
    """1. 整柜票：体积 = M3 列原值；件/PSC/净重/毛重为票内求和。"""
    batch = "STATS-FULL"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K1", "東京港", "厂A", pcs=10, pieces=100, net=5.5, gross=6.6, m3=55.5),
        _row("K1", "東京港", "厂B", pcs=20, pieces=200, net=4.5, gross=5.4,
             m3=55.5, sku="4900000000002"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_full_item("K1")], status="confirmed")

    result = generate_split_stats(sid)
    assert result["warnings"] == []
    blocks = _read_blocks(result)["東京港"]
    assert [t for t, _ in blocks] == ["東京港-01"]
    # 管理号 | 柜型 | 件 | 净重 | 毛重 | 体积 | PSC
    assert blocks[0][1] == [["K1", "40HQ", 30, 10.0, 12.0, 55.5, 300]]


def test_split_container_formula_and_balance():
    """2. 一柜拆两票：小票号走公式（三位小数），大票号 = M3 − 小票号；
    同柜两票体积和 = M3。"""
    batch = "STATS-SPLIT"
    sid = f"split-{batch}"
    _make_filled(batch, [
        # 厂A 部分：10 件 × 50×40×30 cm = 0.6 m³
        _row("K1", "東京港", "厂A", pcs=10, m3=1.0),
        _row("K1", "東京港", "厂B", pcs=5, m3=1.0, sku="4900000000002"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_partial_item("K1", "厂A")])
    _add_decl(sid, "東京港-02", "東京港", [_partial_item("K1", "厂B")])

    result = generate_split_stats(sid)
    assert result["warnings"] == []
    blocks = _read_blocks(result)["東京港"]
    assert [t for t, _ in blocks] == ["東京港-01", "東京港-02"]
    v1 = blocks[0][1][0][5]
    v2 = blocks[1][1][0][5]
    assert v1 == 0.6            # 公式：10×50×40×30÷10⁶，三位小数
    assert v2 == pytest.approx(0.4)   # 余额：1.0 − 0.6
    assert v1 + v2 == pytest.approx(1.0)  # 体积和 = M3


def test_mx_ticket_volume_blank_but_counts_filled():
    """3. MX 票体积留空，件/PSC 照填；净重组全空 → 空单元格而非 0。"""
    batch = "STATS-MX"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K9", "東京港", "MX厂", pcs=8, pieces=88, net=None, gross=None,
             m3=30.0, po="MX2-268510-001"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_partial_item("K9", "MX厂")])

    result = generate_split_stats(sid)
    blocks = _read_blocks(result)["東京港"]
    kanri, ctype, pcs, net, gross, volume, psc = blocks[0][1][0]
    assert kanri == "K9"
    assert pcs == 8 and psc == 88      # 件/PSC 照填
    assert volume is None              # MX 票体积留空
    assert net is None and gross is None  # 净重/毛重组全空 → 空单元格


def test_missing_dims_skipped_with_warning():
    """4. 缺长宽高行跳过不计体积 + 中文警告（含管理号和票号）。"""
    batch = "STATS-NODIM"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K1", "東京港", "厂A", pcs=10, m3=1.0),
        # 厂A 第二行缺宽 → 跳过；体积仍只有第一行的 0.6
        _row("K1", "東京港", "厂A", pcs=5, m3=1.0, w=None,
             sku="4900000000002"),
        _row("K1", "東京港", "厂B", pcs=5, m3=1.0, sku="4900000000003"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_partial_item("K1", "厂A")])
    _add_decl(sid, "東京港-02", "東京港", [_partial_item("K1", "厂B")])

    result = generate_split_stats(sid)
    blocks = _read_blocks(result)["東京港"]
    assert blocks[0][1][0][5] == 0.6           # 缺尺寸行未计入
    assert blocks[0][1][0][2] == 15            # 件数不受影响（10+5）
    assert any("K1" in w and "東京港-01" in w and "长宽高" in w
               for w in result["warnings"])
    # 高度为 0 同样视为缺失（再补一条 0 尺寸行的警告验证）
    assert blocks[1][1][0][5] == pytest.approx(0.4)


def test_zero_dim_also_skipped():
    """4b. 长宽高为 0 的行同样跳过 + 警告。"""
    batch = "STATS-ZERODIM"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K1", "東京港", "厂A", pcs=10, m3=1.0, h=0),
        _row("K1", "東京港", "厂B", pcs=5, m3=1.0, sku="4900000000002"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_partial_item("K1", "厂A")])
    _add_decl(sid, "東京港-02", "東京港", [_partial_item("K1", "厂B")])

    result = generate_split_stats(sid)
    blocks = _read_blocks(result)["東京港"]
    assert blocks[0][1][0][5] == 0.0   # 唯一行被跳过，公式体积 0
    assert any("长宽高" in w for w in result["warnings"])
    assert blocks[1][1][0][5] == pytest.approx(1.0)  # 余额 = M3 − 0


def test_negative_balance_warns_but_outputs():
    """5. 余额 ≤ 0 → 中文警告，仍输出数值。"""
    batch = "STATS-NEG"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K1", "東京港", "厂A", pcs=10, m3=0.5),   # 公式 0.6 > M3 0.5
        _row("K1", "東京港", "厂B", pcs=5, m3=0.5, sku="4900000000002"),
    ])
    _add_decl(sid, "東京港-01", "東京港", [_partial_item("K1", "厂A")])
    _add_decl(sid, "東京港-02", "東京港", [_partial_item("K1", "厂B")])

    result = generate_split_stats(sid)
    blocks = _read_blocks(result)["東京港"]
    assert blocks[1][1][0][5] == pytest.approx(-0.1)  # 仍输出负余额
    assert any("K1" in w and "東京港-02" in w and "≤ 0" in w
               for w in result["warnings"])


def test_sheets_grouped_by_port_tickets_sorted():
    """6. 每港口一个 sheet（按票号首现顺序）、票 block 按 ticket_no 排序
    （DB 乱序插入也应排好）。"""
    batch = "STATS-PORTS"
    sid = f"split-{batch}"
    _make_filled(batch, [
        _row("K1", "東京港", "厂A", m3=10.0),
        _row("K2", "東京港", "厂A", m3=20.0, sku="4900000000002"),
        _row("K3", "横浜港", "厂A", m3=30.0, sku="4900000000003"),
    ])
    _add_decl(sid, "横浜港-01", "横浜港", [_full_item("K3")])
    _add_decl(sid, "東京港-02", "東京港", [_full_item("K2")])
    _add_decl(sid, "東京港-01", "東京港", [_full_item("K1")])

    result = generate_split_stats(sid)
    assert _sheetnames(result) == ["東京港", "横浜港"]
    sheets = _read_blocks(result)
    assert [t for t, _ in sheets["東京港"]] == ["東京港-01", "東京港-02"]
    assert [t for t, _ in sheets["横浜港"]] == ["横浜港-01"]
    # 输出文件名与覆盖语义
    assert Path(result["file_path"]).name == f"截单信息-分体积_{batch}.xlsx"
    result2 = generate_split_stats(sid)  # 重复导出覆盖同名，不报错
    assert result2["file_path"] == result["file_path"]


def test_no_proposal_raises():
    """无分票记录 → ValueError('分票提案不存在')。"""
    with pytest.raises(ValueError, match="分票提案不存在"):
        generate_split_stats("split-STATS-NONE")


def test_no_filled_excel_raises():
    """有提案无装箱单 → FileNotFoundError('装箱单不存在')。"""
    batch = "STATS-NOFILE"
    sid = f"split-{batch}"
    _add_decl(sid, "東京港-01", "東京港", [_full_item("K1")])
    with pytest.raises(FileNotFoundError, match="装箱单不存在"):
        generate_split_stats(sid)
