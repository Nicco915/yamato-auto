# -*- coding: utf-8 -*-
"""手动录入模式 UI 路由测试（第七阶段 任务 B，§2.3/§2.4）。

覆盖接线：
- GET  /api/v1/batches/precheck-manual        预检透传（200 / 422；路由顺序守卫）
- POST /api/v1/watch/start                    面板异步启动 + manual_skip_factories 透传
- POST /api/v1/batches/start                  StartBatchRequest.manual_skip_factories 透传
- POST /api/v1/batches                        CreateBatchRequest.manual_skip_factories 透传
- POST /api/v1/batches/{tid}/add-factories    AddFactoriesRequest.reopen_factories 透传
- POST /api/v1/batches/{tid}/resume-overwrite 写入冲突决策 resume（§2.4）
- 静态资产钩子（board.html / batch.html 关键接线存在性）

service/board 层函数由任务 A 并行实现，本测试全部 monkeypatch
（raising=False 兼容签名未就位）。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_manual_entry_ui.py -q

隔离（血泪红线 2026-08-11）：先 import 全部 app 模块，再 isolate_to_tmp；
跑 pytest 前另设 YAMATO_DOTENV_PATH 指向临时 .env。
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

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_manual_entry_ui_test_")

client = TestClient(app)

STATIC = APP_ROOT / "app" / "ui" / "static"

PRECHECK_REPORT = {
    "factories": [
        {"name": "厂A", "complete": True, "filled_rows": 3, "total_rows": 3},
        {"name": "厂B", "complete": False, "filled_rows": 1, "total_rows": 4},
    ]
}


# ---------- GET /api/v1/batches/precheck-manual ----------

def test_precheck_manual_200_passthrough(monkeypatch):
    """预检端点透传 service.precheck_manual_completeness 的返回值。"""
    seen = {}

    def fake(folder_name, downstream_file_path=None):
        seen["folder_name"] = folder_name
        seen["downstream_file_path"] = downstream_file_path
        return PRECHECK_REPORT

    monkeypatch.setattr(service, "precheck_manual_completeness", fake,
                        raising=False)
    r = client.get("/api/v1/batches/precheck-manual",
                   params={"folder_name": "ETD0901"})
    assert r.status_code == 200, r.text
    assert r.json() == PRECHECK_REPORT
    assert seen["folder_name"] == "ETD0901"
    assert seen["downstream_file_path"] is None
    print("  ✓ test_precheck_manual_200_passthrough")


def test_precheck_manual_passes_downstream_path(monkeypatch):
    """多装箱单：下游文件选择随预检透传（否则工厂勾选清单不可用）。"""
    seen = {}

    def fake(folder_name, downstream_file_path=None):
        seen["folder_name"] = folder_name
        seen["downstream_file_path"] = downstream_file_path
        return {"factories": []}

    monkeypatch.setattr(service, "precheck_manual_completeness", fake,
                        raising=False)
    r = client.get("/api/v1/batches/precheck-manual",
                   params={"folder_name": "ETD0901",
                           "downstream_file_path": "/w/ETD0901/a.xlsx"})
    assert r.status_code == 200, r.text
    assert seen["downstream_file_path"] == "/w/ETD0901/a.xlsx"
    print("  ✓ test_precheck_manual_passes_downstream_path")


def test_precheck_manual_route_order_guard(monkeypatch):
    """路由顺序守卫：/precheck-manual 不能被 /{thread_id} 捕获（捕获则 404）。"""
    monkeypatch.setattr(service, "precheck_manual_completeness",
                        lambda folder_name, downstream_file_path=None:
                        {"factories": []}, raising=False)
    r = client.get("/api/v1/batches/precheck-manual",
                   params={"folder_name": "f"})
    assert r.status_code == 200 and "factories" in r.json()
    print("  ✓ test_precheck_manual_route_order_guard")


def test_precheck_manual_422(monkeypatch):
    """文件夹不存在/装箱单解析失败 → 422。"""
    def fake(folder_name, downstream_file_path=None):
        raise ValueError(f"子文件夹不存在: {folder_name}")

    monkeypatch.setattr(service, "precheck_manual_completeness", fake,
                        raising=False)
    r = client.get("/api/v1/batches/precheck-manual",
                   params={"folder_name": "nope"})
    assert r.status_code == 422
    assert "子文件夹不存在" in r.json()["detail"]
    print("  ✓ test_precheck_manual_422")


# ---------- POST /api/v1/batches（CreateBatchRequest.manual_skip_factories） ----------

def test_create_batch_passes_manual_skip(monkeypatch):
    """带 manual_skip_factories 发起批次 → 透传 service.create_batch。"""
    captured = {}

    def fake(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"status": "pending_human_review"}

    monkeypatch.setattr(service, "create_batch", fake)
    r = client.post("/api/v1/batches", json={
        "thread_id": "t-manual",
        "manual_skip_factories": ["厂A", "厂B"],
    })
    assert r.status_code == 200, r.text
    assert captured["args"][0] == "t-manual"
    assert captured.get("manual_skip_factories") == ["厂A", "厂B"]
    print("  ✓ test_create_batch_passes_manual_skip")


def test_create_batch_empty_skip_not_passed(monkeypatch):
    """缺省/空清单不透传 manual_skip_factories（兼容任务 A 签名未就位）。"""
    captured = {}

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(service, "create_batch", fake)
    r = client.post("/api/v1/batches", json={"thread_id": "t-normal"})
    assert r.status_code == 200, r.text
    assert "manual_skip_factories" not in captured
    print("  ✓ test_create_batch_empty_skip_not_passed")


# ---------- POST /api/v1/watch/start（面板异步启动 + manual_skip_factories） ----------

def test_watch_start_passes_manual_skip(monkeypatch):
    """面板一键启动：手动跳过清单经 WatchStartRequest 透传 board.start_from_board。"""
    from app.orchestrator import board

    captured = {}

    def fake(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"ok": True, "thread_id": "tid-1"}

    monkeypatch.setattr(board, "start_from_board", fake)
    r = client.post("/api/v1/watch/start", json={
        "folder_name": "ETD0901",
        "thread_id": "tid-1",
        "manual_skip_factories": ["厂A"],
    })
    assert r.status_code == 200, r.text
    assert captured["args"][0] == "ETD0901"
    assert captured["args"][1] == "tid-1"
    assert captured.get("manual_skip_factories") == ["厂A"]
    print("  ✓ test_watch_start_passes_manual_skip")


def test_watch_start_empty_skip_not_passed(monkeypatch):
    """无手动录入厂时保持既有调用形态（不透传空清单）。"""
    from app.orchestrator import board

    captured = {}

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(board, "start_from_board", fake)
    r = client.post("/api/v1/watch/start", json={"folder_name": "f"})
    assert r.status_code == 200, r.text
    assert "manual_skip_factories" not in captured
    print("  ✓ test_watch_start_empty_skip_not_passed")


# ---------- POST /api/v1/batches/start（StartBatchRequest.manual_skip_factories） ----------

def test_batches_start_passes_manual_skip(monkeypatch):
    """API 启动端点同样透传手动跳过清单（与面板/控制台口径一致）。"""
    captured = {}

    def fake(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"status": "pending_human_review"}

    monkeypatch.setattr(service, "start_batch_from_scan", fake)
    r = client.post("/api/v1/batches/start", json={
        "folder_name": "ETD0901",
        "thread_id": "tid-1",
        "manual_skip_factories": ["厂A"],
    })
    assert r.status_code == 200, r.text
    assert captured["args"][0] == "ETD0901"
    assert captured["args"][1] == "tid-1"
    assert captured.get("manual_skip_factories") == ["厂A"]
    print("  ✓ test_batches_start_passes_manual_skip")


# ---------- POST /api/v1/batches/{tid}/add-factories（reopen_factories） ----------

def test_add_factories_reopen_passthrough(monkeypatch):
    """reopen_factories 透传 service.add_factories_to_batch；响应带 reopened。"""
    captured = {}

    def fake(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"added": 0, "reopened": ["厂A"]}

    monkeypatch.setattr(service, "add_factories_to_batch", fake)
    r = client.post("/api/v1/batches/t1/add-factories",
                    json={"reopen_factories": ["厂A"]})
    assert r.status_code == 200, r.text
    assert captured["args"][0] == "t1"
    assert captured.get("reopen_factories") == ["厂A"]
    assert r.json()["reopened"] == ["厂A"]
    print("  ✓ test_add_factories_reopen_passthrough")


def test_add_factories_empty_reopen_not_passed(monkeypatch):
    """空 reopen 清单不透传（兼容任务 A 签名未就位），confirm 标志照旧。"""
    captured = {}

    def fake(*args, **kwargs):
        captured.update(kwargs)
        return {"added": 1}

    monkeypatch.setattr(service, "add_factories_to_batch", fake)
    r = client.post("/api/v1/batches/t1/add-factories",
                    json={"confirm_overwrite": True})
    assert r.status_code == 200, r.text
    assert captured.get("confirm_overwrite") is True
    assert "reopen_factories" not in captured
    print("  ✓ test_add_factories_empty_reopen_not_passed")


# ---------- POST /api/v1/batches/{tid}/resume-overwrite（§2.4） ----------

def test_resume_overwrite_decision_passthrough(monkeypatch):
    """overwrite/keep 两种决策都以 {"decision": ...} 形态进 resume_order。"""
    captured = {}

    def fake(thread_id, resume_data):
        captured["thread_id"] = thread_id
        captured["resume_data"] = resume_data
        return {"status": "success", "thread_id": thread_id}

    monkeypatch.setattr(service, "resume_order", fake)
    for decision in ("overwrite", "keep"):
        r = client.post("/api/v1/batches/t1/resume-overwrite",
                        json={"decision": decision})
        assert r.status_code == 200, r.text
        assert captured["thread_id"] == "t1"
        assert captured["resume_data"] == {"decision": decision}
    print("  ✓ test_resume_overwrite_decision_passthrough")


def test_resume_overwrite_invalid_decision_422():
    """非法 decision 值被 pydantic Literal 拦下 → 422。"""
    r = client.post("/api/v1/batches/t1/resume-overwrite",
                    json={"decision": "bogus"})
    assert r.status_code == 422
    print("  ✓ test_resume_overwrite_invalid_decision_422")


def test_resume_overwrite_not_suspended_400(monkeypatch):
    """未挂起时 resume_order 抛 ValueError → 400。"""
    def fake(thread_id, resume_data):
        raise ValueError("该任务没有处于等待审核状态，或已完成。")

    monkeypatch.setattr(service, "resume_order", fake)
    r = client.post("/api/v1/batches/t1/resume-overwrite",
                    json={"decision": "overwrite"})
    assert r.status_code == 400
    print("  ✓ test_resume_overwrite_not_suspended_400")


# ---------- 静态资产钩子（前端接线存在性回归守卫） ----------

def test_board_html_manual_hooks():
    """board.html：预检调用 + 全部跳过总开关 + watch/start 透传 + 重开勾选窗。"""
    h = (STATIC / "board.html").read_text(encoding="utf-8")
    assert "/api/v1/batches/precheck-manual" in h   # 预检端点
    assert "/api/v1/watch/start" in h               # 统一异步启动端点
    assert "manual_skip_factories" in h             # 跳过清单随启动透传
    assert "/api/v1/batches/scan-start" not in h    # 同步 workaround 已移除
    assert "start-manual-all" in h                  # 全部跳过总开关
    assert "已填完整" in h                           # 预勾选建议标注
    assert "af-reopen-check" in h                   # 重开识别勾选
    assert "reopen_factories" in h                  # add-factories 透传
    print("  ✓ test_board_html_manual_hooks")


def test_batch_html_manual_hooks():
    """batch.html：手动录入标识 + 冲突确认对话框 + 重开勾选窗。"""
    h = (STATIC / "batch.html").read_text(encoding="utf-8")
    assert "手动录入" in h                           # 工厂项标识（§2.5）
    assert "overwrite_confirm" in h                  # 冲突 payload 检出（§2.4）
    assert "resume-overwrite" in h                   # 决策 resume 端点
    assert '"decision": decision' in h or "decision: decision" in h
    assert "af-reopen-check" in h                    # 重开识别勾选
    assert "reopen_factories" in h
    print("  ✓ test_batch_html_manual_hooks")


def main():
    """脚本模式直跑（无 pytest 注入 fixture 时用 MonkeyPatch.context）。"""
    with pytest.MonkeyPatch.context() as mp:
        test_precheck_manual_200_passthrough(mp)
        test_precheck_manual_passes_downstream_path(mp)
        test_precheck_manual_route_order_guard(mp)
        test_precheck_manual_422(mp)
        test_create_batch_passes_manual_skip(mp)
        test_create_batch_empty_skip_not_passed(mp)
        test_watch_start_passes_manual_skip(mp)
        test_watch_start_empty_skip_not_passed(mp)
        test_batches_start_passes_manual_skip(mp)
        test_add_factories_reopen_passthrough(mp)
        test_add_factories_empty_reopen_not_passed(mp)
        test_resume_overwrite_decision_passthrough(mp)
    test_resume_overwrite_invalid_decision_422()
    with pytest.MonkeyPatch.context() as mp:
        test_resume_overwrite_not_suspended_400(mp)
    test_board_html_manual_hooks()
    test_batch_html_manual_hooks()
    print("\ntest_manual_entry_ui: PASS")


if __name__ == "__main__":
    main()
