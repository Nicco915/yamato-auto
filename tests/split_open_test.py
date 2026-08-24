# -*- coding: utf-8 -*-
"""分票打开目录端点 + 工作台批次列表 split- 前缀过滤的测试。

覆盖：
- POST /api/v1/split/{id}/open：本机闸门（非本机 403）、目录不存在 404、
  正常打开 200（monkeypatch open_with_default_app，绝不真拉起 Finder）、
  OpenFileError → 503、路径穿越前缀被 resolve+白名单拦截
- service._list_thread_ids / list_batches：split- 前缀线程不出现在
  批次列表（分票图与提取图共用 checkpoints 库，混入会显示假批次条目，
  且在工作台删除它会连带删掉分票状态——2026-08-25 实测踩坑）

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/split_open_test.py -v

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

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.split import router as split_router  # noqa: E402
from app.ui.open_file import OpenFileError  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_split_open_test_")

client = TestClient(app, client=("127.0.0.1", 12345))  # 显式本机 client（过本机闸门）
remote_client = TestClient(app, client=("10.0.0.9", 12345))  # 非本机

SPLIT_TID = "split-TEST-OPEN"


def _decl_dir() -> Path:
    """隔离 output 下该分票任务的报关单目录路径（不创建）。"""
    s = get_settings()
    batch_id = SPLIT_TID.removeprefix("split-")
    return s.batch_declarations_dir(batch_id) / SPLIT_TID


# ---------------------------------------------------------------------------
# /open 端点
# ---------------------------------------------------------------------------


def test_open_declarations_happy_path(monkeypatch, tmp_path):
    """目录存在 + 本机 → 200，open_with_default_app 被以该目录调用。"""
    d = _decl_dir()
    d.mkdir(parents=True)
    called = {}
    monkeypatch.setattr(
        split_router, "open_with_default_app",
        lambda p: called.setdefault("path", p))
    r = client.post(f"/api/v1/split/{SPLIT_TID}/open")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert called["path"] == d.resolve()


def test_open_declarations_remote_403():
    """非本机请求一律 403（本机闸门）。"""
    r = remote_client.post(f"/api/v1/split/{SPLIT_TID}/open")
    assert r.status_code == 403, r.text
    assert "本机" in r.json()["detail"]


def test_open_declarations_missing_dir_404():
    """目录不存在（还没生成报关单）→ 404，中文提示。"""
    r = client.post("/api/v1/split/split-NEVER-GENERATED/open")
    assert r.status_code == 404, r.text
    assert "报关单目录不存在" in r.json()["detail"]


def test_open_declarations_open_error_503(monkeypatch):
    """底层打开命令失败 → OpenFileError 翻译成 503。"""
    d = _decl_dir()
    d.mkdir(parents=True, exist_ok=True)

    def _boom(p):
        raise OpenFileError("macOS open 命令执行失败: 模拟")

    monkeypatch.setattr(split_router, "open_with_default_app", _boom)
    r = client.post(f"/api/v1/split/{SPLIT_TID}/open")
    assert r.status_code == 503, r.text
    assert "模拟" in r.json()["detail"]


def test_open_declarations_traversal_blocked():
    """split_thread_id 含 ../ 穿越片段：resolve 后落在白名单外 → 403/404，
    绝不开到输出目录之外。"""
    r = client.post("/api/v1/split/..%2F..%2Ftmp/open")
    assert r.status_code in (403, 404), r.text


# ---------------------------------------------------------------------------
# list_batches 过滤 split- 前缀
# ---------------------------------------------------------------------------


def test_list_thread_ids_filters_split_prefix():
    """checkpoints 表里的 split- 线程不出现在批次枚举中。"""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id INTEGER)")
    conn.executemany(
        "INSERT INTO checkpoints VALUES (?, ?)",
        [("batch-A", 1), ("split-batch-A", 2), ("batch-B", 3),
         ("split-batch-B", 4)],
    )
    ids = service._list_thread_ids(conn)
    conn.close()
    assert ids == ["batch-A", "batch-B"] or set(ids) == {"batch-A", "batch-B"}
    assert not any(t.startswith("split-") for t in ids)
