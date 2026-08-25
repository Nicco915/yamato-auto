# -*- coding: utf-8 -*-
"""工作台最近使用路径持久化测试。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/last_paths_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
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

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.ui.last_paths import load_last_paths, save_last_paths  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_last_paths_test_")

client = TestClient(app)


def test_save_and_load_paths():
    """保存后读取能拿到最新路径；空字符串不覆盖旧值。"""
    save_last_paths("/a/upstream", "/a/downstream.xlsx")
    d = load_last_paths()
    assert d["upstream_root"] == "/a/upstream"
    assert d["downstream_file_path"] == "/a/downstream.xlsx"

    # 下游为空时不覆盖上游
    save_last_paths("/b/upstream", "")
    d = load_last_paths()
    assert d["upstream_root"] == "/b/upstream"
    assert d["downstream_file_path"] == "/a/downstream.xlsx"


def test_defaults_endpoint_uses_last_paths():
    """GET /api/v1/config/defaults 优先返回 last_paths 而非 .env 默认值。"""
    save_last_paths("/tmp/upstream", "/tmp/downstream.xlsx")
    r = client.get("/api/v1/config/defaults")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["upstream_root"] == "/tmp/upstream"
    assert data["downstream_file_path"] == "/tmp/downstream.xlsx"


def test_save_endpoint():
    """POST /api/v1/config/last-paths 成功写入文件。"""
    r = client.post("/api/v1/config/last-paths", json={
        "upstream_root": "/api/upstream",
        "downstream_file_path": "/api/file.xlsx",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    d = load_last_paths()
    assert d["upstream_root"] == "/api/upstream"
    assert d["downstream_file_path"] == "/api/file.xlsx"


def test_file_stored_in_data_dir():
    """持久化文件落在 settings 的数据目录下（与 checkpoint_db 同目录）。"""
    save_last_paths("/c/upstream", "/c/downstream.xlsx")
    expected = Path(get_settings().checkpoint_db_abs).parent / "last_paths.json"
    assert expected.is_file()
