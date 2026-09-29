# -*- coding: utf-8 -*-
"""导出 API 端点测试（F1 XD 透视 / F2 截单信息-分体积）。

覆盖：
- POST /api/v1/batches/{batch_id}/export-pivot：
  200 + 文件生成 / 404 批次不存在 / 409 无装箱单
- GET  /api/v1/batches/{batch_id}/pivot/download：200 / 404 未导出过
- POST /api/v1/split/{split_thread_id}/export-stats：
  200 + 文件生成 / 404 无提案 / 409 无装箱单
- GET  /api/v1/split/{split_thread_id}/stats/download：200 / 404 未导出过

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_export_api.py -q

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

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Declaration  # noqa: E402
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_export_api_test_")

client = TestClient(app)

# stats 用 filled Excel 列（必需 13 列 + 可选 PO/长宽高列）
STATS_COLUMNS = [
    "KANRI_NO", "MINATO_MEI_KJ", "CONTAINER_MEI", "MAKER_MEI_KJ",
    "SHOHIN_CD", "净重", "毛重", "SOTOBAKO_D_HACCHU_SU",
    "中文品名", "D_HACCHU_SU", "KAKAKUKEI", "TSUKA_MEI", "M3",
    "PURCHASE_ORDER", "SOTOBAKO_HABA", "SOTOBAKO_OKUYUKI", "SOTOBAKO_TAKASA",
]


def _write_pivot_source(path: Path) -> None:
    """造 pivot 数据源装箱单：1 港口 2 管理号。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["MINATO_MEI_KJ", "KANRI_NO", "CONTAINER_MEI", "M3",
               "SOTOBAKO_D_HACCHU_SU"])
    ws.append(["博多港", "XC001", "20F", 10.0, 100])
    ws.append(["博多港", "XC002", "40F", 20.0, 200])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _write_stats_filled(batch_id: str) -> Path:
    """在隔离 output 下造 containers/{batch}_filled.xlsx（stats 13+4 列）。"""
    containers_dir = get_settings().batch_containers_dir(batch_id)
    containers_dir.mkdir(parents=True, exist_ok=True)
    path = containers_dir / f"{batch_id}_filled.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(STATS_COLUMNS)
    ws.append([
        "K1", "東京港", "40HQ", "厂A", "4900000000001", 5.5, 6.6, 10,
        "测试品", 100, 100.0, "USD", 55.5, "PO-001", 50, 40, 30,
    ])
    wb.save(path)
    return path


def _add_decl(split_id: str, ticket_no: str = "東京港-01",
              port: str = "東京港") -> None:
    """插一条 pending 分票提案（整柜票，管理号 K1）。"""
    with get_session() as s:
        s.add(Declaration(
            split_thread_id=split_id, ticket_no=ticket_no, port=port,
            container_type="40HQ",
            items=[{"kanri_no": "K1", "factory_filter": None,
                    "factory_exclude": None, "is_partial": False,
                    "inspection_filter": None}],
            sj_factories=[], status="pending", version=1,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# POST /api/v1/batches/{batch_id}/export-pivot
# ---------------------------------------------------------------------------

def test_export_pivot_success():
    """200：返回 {ok, file_path, warnings}，文件真实生成在 pivot 目录。"""
    batch = "API-PIVOT-OK"
    _write_pivot_source(
        get_settings().batch_containers_dir(batch) / f"{batch}_filled.xlsx"
    )
    r = client.post(f"/api/v1/batches/{batch}/export-pivot")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["warnings"] == []
    out = Path(data["file_path"])
    assert out.is_file()
    assert out.parent == get_settings().batch_pivot_dir(batch)
    assert out.name.startswith("XD透视_")


def test_export_pivot_batch_not_found():
    """404：批次不存在（containers 无 filled，批次登记也没有）。"""
    r = client.post("/api/v1/batches/API-PIVOT-GHOST/export-pivot")
    assert r.status_code == 404
    assert "批次不存在" in r.json()["detail"]


def test_export_pivot_no_packing_list(monkeypatch):
    """409：批次存在但装箱单不存在（登记原件路径无效）。"""
    from app.export import pivot as pivot_mod
    monkeypatch.setattr(
        pivot_mod, "get_batch",
        lambda bid: {"thread_id": bid,
                     "downstream_file_path": "/nonexistent/xxx.xlsx"},
    )
    r = client.post("/api/v1/batches/API-PIVOT-NOFILE/export-pivot")
    assert r.status_code == 409
    assert "装箱单不存在" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v1/batches/{batch_id}/pivot/download
# ---------------------------------------------------------------------------

def test_download_pivot_not_exported():
    """404：从未导出过的批次没有透视文件。"""
    r = client.get("/api/v1/batches/API-PIVOT-NEVER/pivot/download")
    assert r.status_code == 404
    assert "尚未导出" in r.json()["detail"]


def test_download_pivot_success():
    """200：导出后可下载最新文件，Content-Disposition 带中文文件名。"""
    batch = "API-PIVOT-DL"
    _write_pivot_source(
        get_settings().batch_containers_dir(batch) / f"{batch}_filled.xlsx"
    )
    r = client.post(f"/api/v1/batches/{batch}/export-pivot")
    assert r.status_code == 200, r.text

    d = client.get(f"/api/v1/batches/{batch}/pivot/download")
    assert d.status_code == 200, d.text
    assert len(d.content) > 0
    cd = d.headers.get("content-disposition", "")
    assert "attachment" in cd
    # 中文文件名走 filename*=utf-8'' 百分号编码
    assert "filename" in cd


# ---------------------------------------------------------------------------
# POST /api/v1/split/{split_thread_id}/export-stats
# ---------------------------------------------------------------------------

def test_export_stats_success():
    """200：返回 {ok, file_path, warnings}，文件真实生成在 stats 目录。"""
    batch = "API-STATS-OK"
    sid = f"split-{batch}"
    _write_stats_filled(batch)
    _add_decl(sid)

    r = client.post(f"/api/v1/split/{sid}/export-stats")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["warnings"] == []
    out = Path(data["file_path"])
    assert out.is_file()
    assert out.parent == get_settings().batch_stats_dir(batch)
    assert out.name.startswith("截单信息-分体积_")


def test_export_stats_no_proposal():
    """404：该 split_thread_id 无任何分票提案。"""
    r = client.post("/api/v1/split/split-API-STATS-NONE/export-stats")
    assert r.status_code == 404
    assert "分票提案不存在" in r.json()["detail"]


def test_export_stats_no_packing_list():
    """409：有提案但 containers 下没有 filled 装箱单。"""
    batch = "API-STATS-NOFILE"
    sid = f"split-{batch}"
    _add_decl(sid)
    r = client.post(f"/api/v1/split/{sid}/export-stats")
    assert r.status_code == 409
    assert "装箱单不存在" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v1/split/{split_thread_id}/stats/download
# ---------------------------------------------------------------------------

def test_download_stats_not_exported():
    """404：从未导出过的分票任务没有统计文件。"""
    r = client.get("/api/v1/split/split-API-STATS-NEVER/stats/download")
    assert r.status_code == 404
    assert "尚未导出" in r.json()["detail"]


def test_download_stats_success():
    """200：导出后可下载最新文件，Content-Disposition 带中文文件名。"""
    batch = "API-STATS-DL"
    sid = f"split-{batch}"
    _write_stats_filled(batch)
    _add_decl(sid)
    r = client.post(f"/api/v1/split/{sid}/export-stats")
    assert r.status_code == 200, r.text

    d = client.get(f"/api/v1/split/{sid}/stats/download")
    assert d.status_code == 200, d.text
    assert len(d.content) > 0
    cd = d.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename" in cd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
