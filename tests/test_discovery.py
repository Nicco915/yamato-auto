# -*- coding: utf-8 -*-
"""端到端批次发现服务测试。

覆盖：
- 扫描监控目录发现新子文件夹；
- 下游装箱单自动匹配；
- MX2 文件检测；
- 已存在 batch 记录被跳过。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_discovery.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.orchestrator import discovery  # noqa: E402
from app.orchestrator.discovery import (  # noqa: E402
    discover_downstream_files,
    discover_mx2_files,
    scan_new_batches,
)

from _test_isolation import isolate_to_tmp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

TMP = isolate_to_tmp("yamato_discovery_test_")

client = TestClient(app)


def _make_xlsx(path: Path):
    """写一个最小 xlsx（空表即可用于文件名探测）。"""
    from openpyxl import Workbook
    wb = Workbook()
    wb.save(path)


def test_discover_downstream_files():
    sub = TMP / "batchA"
    sub.mkdir()
    _make_xlsx(sub / "ContentsOfTheContainer_001.xlsx")
    _make_xlsx(sub / "other.xlsx")

    found = discover_downstream_files(sub)
    assert len(found) == 1
    assert "ContentsOfTheContainer" in found[0].name


def test_discover_mx2_files():
    sub = TMP / "batchB"
    sub.mkdir()
    _make_xlsx(sub / "青島MX2入荷予定リスト_001.xlsx")

    found = discover_mx2_files(sub)
    assert len(found) == 1
    assert "入荷予定リスト" in found[0].name


def test_scan_new_batches_skips_existing():
    watch = TMP / "watch"
    watch.mkdir()
    (watch / "NEWBATCH").mkdir()
    (watch / "OLDBATCH").mkdir()
    _make_xlsx(watch / "NEWBATCH" / "ContentsOfTheContainer.xlsx")
    _make_xlsx(watch / "OLDBATCH" / "ContentsOfTheContainer.xlsx")

    # 预写入 OLDBATCH
    batch_store.upsert_batch("OLDBATCH", watch_dir=str(watch), status="completed")

    # 临时修改 settings.watch_dir
    original = get_settings().watch_dir
    get_settings().watch_dir = str(watch)
    try:
        result = scan_new_batches()
        names = {r["folder_name"] for r in result}
        assert "NEWBATCH" in names
        assert "OLDBATCH" not in names
    finally:
        get_settings().watch_dir = original


def test_api_scan_batches():
    watch = TMP / "watch_api"
    watch.mkdir()
    (watch / "APIBATCH").mkdir()
    _make_xlsx(watch / "APIBATCH" / "ContentsOfTheContainer.xlsx")

    original = get_settings().watch_dir
    get_settings().watch_dir = str(watch)
    try:
        resp = client.post("/api/v1/batches/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        names = [c["folder_name"] for c in data["candidates"]]
        assert "APIBATCH" in names
    finally:
        get_settings().watch_dir = original


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
