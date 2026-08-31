# -*- coding: utf-8 -*-
"""调度 Agent skill 测试：split_and_generate / batch_health / master_data_health。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_skills_test.py -v

隔离：血泪红线——app 模块 import 全部完成后才 isolate_to_tmp
（import 链触发 load_dotenv(override=True)，顺序反了会把隔离 env
打回真实路径污染生产库）。skill 测试大量用 monkeypatch mock 执行体，
DB 侧只在 master_data_health 用隔离库。
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

from app.db.models import Factory, FactoryAlias, FactorySKU  # noqa: E402
from app.db.session import get_session  # noqa: E402
# 血泪红线：先完成全部 app 模块 import，再 isolate_to_tmp
from app.dispatcher import tools as dispatcher_tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_dispatcher_skills_")

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# Skill 1：split_and_generate
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_chain(monkeypatch):
    """mock 掉分票链三个执行体 + 状态探测 + 文件清单，返回调用记录。"""
    calls = {"start": 0, "confirm": 0, "generate": 0}

    def fake_start(args, on_progress=None):
        calls["start"] += 1
        return {"status": "pending_review", "message": "方案已生成"}

    def fake_confirm(args, on_progress=None):
        calls["confirm"] += 1
        return {"status": "confirmed", "total_declarations": 3,
                "message": "分票已确认"}

    def fake_generate(args, on_progress=None):
        calls["generate"] += 1
        return {"status": "generated", "count": 3, "warnings": [],
                "message": "3 份报关单已生成"}

    monkeypatch.setattr(dispatcher_tools, "_exec_start_split", fake_start)
    monkeypatch.setattr(dispatcher_tools, "_exec_confirm_split", fake_confirm)
    monkeypatch.setattr(dispatcher_tools, "_exec_generate_declarations",
                        fake_generate)
    monkeypatch.setattr(dispatcher_tools, "_fn_list_declaration_files",
                        lambda args: {"status": "ok", "total": 3,
                                      "message": "3 份"})
    return calls


def _set_status(monkeypatch, status: str):
    monkeypatch.setattr(dispatcher_tools, "_split_status",
                        lambda tid: status)


def test_split_and_generate_full_chain_from_not_started(monkeypatch, mock_chain):
    """not_started：三步全跑，返回成功 + 文件数。"""
    _set_status(monkeypatch, "not_started")
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert r.get("ok"), f"应成功: {r}"
    assert mock_chain == {"start": 1, "confirm": 1, "generate": 1}
    assert r["count"] == 3 and r["files_total"] == 3
    assert "分票方案已生成" in r["message"] and "报关单已生成" in r["message"]


def test_split_and_generate_skips_start_when_pending(monkeypatch, mock_chain):
    """pending_review：跳过启动，只跑确认+生成。"""
    _set_status(monkeypatch, "pending_review")
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert r.get("ok"), f"应成功: {r}"
    assert mock_chain == {"start": 0, "confirm": 1, "generate": 1}


def test_split_and_generate_skips_to_generate_when_confirmed(
        monkeypatch, mock_chain):
    """confirmed：只跑生成。"""
    _set_status(monkeypatch, "confirmed")
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert r.get("ok"), f"应成功: {r}"
    assert mock_chain == {"start": 0, "confirm": 0, "generate": 1}
    assert "此前已确认" in r["message"]


def test_split_and_generate_start_failure_aborts(monkeypatch, mock_chain):
    """启动失败：不执行确认/生成，error 标明步骤。"""
    _set_status(monkeypatch, "not_started")

    def fail_start(args, on_progress=None):
        return {"error": "源文件不存在"}

    monkeypatch.setattr(dispatcher_tools, "_exec_start_split", fail_start)
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert "error" in r and "第 1 步" in r["error"]
    assert mock_chain["confirm"] == 0 and mock_chain["generate"] == 0
    assert not r.get("partial")


def test_split_and_generate_confirm_failure_partial(monkeypatch, mock_chain):
    """确认失败：方案已生成但未确认，partial 回报，不生成报关单。"""
    _set_status(monkeypatch, "not_started")

    def fail_confirm(args, on_progress=None):
        return {"error": "方案存在警告，未强制通过"}

    monkeypatch.setattr(dispatcher_tools, "_exec_confirm_split", fail_confirm)
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert "error" in r and r.get("partial") is True
    assert "分票方案已生成" in r["error"]  # 已完成的第一步必须明示
    assert "确认分票失败" in r["error"]
    assert mock_chain["generate"] == 0


def test_split_and_generate_generate_failure_partial(monkeypatch, mock_chain):
    """生成失败：分票已确认（不沉默回滚），partial 回报。"""
    _set_status(monkeypatch, "not_started")

    def fail_generate(args, on_progress=None):
        return {"error": "发票号码段为空"}

    monkeypatch.setattr(dispatcher_tools, "_exec_generate_declarations",
                        fail_generate)
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert "error" in r and r.get("partial") is True
    assert "分票已确认" in r["error"] and "生成报关单失败" in r["error"]


def test_split_and_generate_requires_invoice_number():
    """缺 invoice_number：直接 error，不启动任何步骤。"""
    r = dispatcher_tools._exec_split_and_generate({"thread_id": "89"})
    assert "error" in r and "invoice_number" in r["error"]


def test_split_and_generate_unknown_status_rejected(monkeypatch, mock_chain):
    """loading/completed 等非常规状态：拒绝一键链路，引导单步工具。"""
    _set_status(monkeypatch, "loading")
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert "error" in r and "不适合一键链路" in r["error"]
    assert mock_chain == {"start": 0, "confirm": 0, "generate": 0}


def test_split_and_generate_start_not_pending_aborts(monkeypatch, mock_chain):
    """启动后未挂起待确认（空方案等异常态）：不自动确认。"""
    _set_status(monkeypatch, "not_started")

    def empty_start(args, on_progress=None):
        return {"status": "completed", "message": "分票已完成"}

    monkeypatch.setattr(dispatcher_tools, "_exec_start_split", empty_start)
    r = dispatcher_tools._exec_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert "error" in r and "未进入待确认状态" in r["error"]
    assert mock_chain["confirm"] == 0


def test_split_and_generate_preview_warnings(monkeypatch):
    """preview：缺发票号 + 已生成过报关单 → 两条警告；状态步骤清单正确。"""
    monkeypatch.setattr(dispatcher_tools, "_split_status",
                        lambda tid: "not_started")
    monkeypatch.setattr(dispatcher_tools, "_fn_list_declaration_files",
                        lambda args: {"status": "ok", "total": 5,
                                      "message": "5 份"})

    class _FakeService:
        @staticmethod
        def get_order_state(tid):
            return {"exists": True, "values": {}}

    monkeypatch.setattr(dispatcher_tools, "service", _FakeService)
    p = dispatcher_tools._preview_split_and_generate({"thread_id": "89"})
    warn_text = " ".join(p["warnings"])
    assert "invoice_number" in warn_text
    assert "已生成过 5 份" in warn_text
    assert any("启动分票" in line for line in p["lines"])

    monkeypatch.setattr(dispatcher_tools, "_split_status",
                        lambda tid: "confirmed")
    p2 = dispatcher_tools._preview_split_and_generate(
        {"thread_id": "89", "invoice_number": "656"})
    assert any("直接生成" in line for line in p2["lines"])
