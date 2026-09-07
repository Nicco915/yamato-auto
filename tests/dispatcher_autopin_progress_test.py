# -*- coding: utf-8 -*-
"""对话启动批次的两项体验修复回归（2026-09 生产反馈）。

1. start_scanned_batch 确认执行时前端只显示「正在执行…」——
   _exec_start_scanned_batch 的 on_progress 形参原为摆设，未透传
   service；修复后节点级进度（正在提取工厂X）经 exec_progress 流式推送。
2. 对话里启动批次后 session 未绑定批次（工作台入口会自动 pin）——
   confirm applied 后若会话未 pin，自动写 chat_sessions.pinned_thread_id。

覆盖：
- tools 层：on_progress 包装为 exec_progress（tool/thread_id 字段，
  thread_id 缺省时解析为 folder_name）；
- service 层：start_batch_from_scan 把 on_progress 透传 create_batch；
- auto-pin：applied 且未 pin → 绑定；已 pin 不动；error 不绑；
  非建批工具（mark_batch_done）不绑；confirm 全链路 applied → pin。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_autopin_progress_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

# 血泪红线：先 import 全部 app 模块，再 isolate_to_tmp
from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.db.models import ChatSession as _ChatSessionOrm  # noqa: E402
from app.db.session import get_session as _get_db_session  # noqa: E402
from app.dispatcher import sessions  # noqa: E402
from app.dispatcher import tools as dispatcher_tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

_WATCH = Path(tempfile.mkdtemp(prefix="yamato_autopin_watch_"))

TMP = isolate_to_tmp("yamato_dispatcher_autopin_",
                     extra_env={"WATCH_DIR": str(_WATCH)})

from openpyxl import Workbook  # noqa: E402


def _fresh_session_id() -> str:
    return f"test-autopin-{uuid.uuid4().hex[:12]}"


def _db_pin(session_id: str):
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, session_id)
        return row.pinned_thread_id if row else None


def _plant_pending(session_id: str, tool: str, args: dict) -> None:
    session = sessions.get_session(session_id)
    session.pending_action = {
        "kind": "dispatcher_tool",
        "tool": tool,
        "args": args,
        "summary": "测试用待确认操作",
        "preview_lines": [],
        "warnings": [],
        "created_at": time.time(),
    }
    sessions.persist_pending(session)


# ---------------------------------------------------------------------------
# 1. on_progress 透传（exec_progress 流式进度）
# ---------------------------------------------------------------------------

def test_exec_wraps_on_progress(monkeypatch):
    """_exec_start_scanned_batch 把 on_progress 包装成 exec_progress 透传。"""
    captured = {}

    def fake_start(folder_name, thread_id=None, downstream_file_path=None,
                   upstream_root=None, on_progress=None):
        captured["on_progress"] = on_progress
        if on_progress:
            on_progress({"node": "extraction", "message": "正在提取工厂A"})
        return {"status": "pending_human_review", "thread_id": "test-93"}

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)
    events = []
    result = dispatcher_tools._exec_start_scanned_batch(
        {"folder_name": "XD437-ETD0613"},
        on_progress=events.append)

    assert result.get("status") == "pending_human_review"
    assert captured["on_progress"] is not None, "on_progress 必须透传 service"
    assert events and events[0]["type"] == "exec_progress"
    assert events[0]["tool"] == "start_scanned_batch"
    # thread_id 缺省时解析为 folder_name（进度事件才能关联批次）
    assert events[0]["thread_id"] == "XD437-ETD0613"
    assert events[0]["message"] == "正在提取工厂A"


def test_exec_without_on_progress_zero_cost(monkeypatch):
    """on_progress=None 时传 None 给 service（零开销，不包空壳）。"""
    captured = {}

    def fake_start(folder_name, thread_id=None, downstream_file_path=None,
                   upstream_root=None, on_progress=None):
        captured["on_progress"] = on_progress
        return {"status": "pending_human_review"}

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)
    dispatcher_tools._exec_start_scanned_batch({"folder_name": "XD437"})
    assert captured["on_progress"] is None


def test_service_threads_on_progress(monkeypatch):
    """service.start_batch_from_scan 把 on_progress 透传 create_batch。"""
    folder = _WATCH / "XD900-PROGRESS"
    folder.mkdir(parents=True, exist_ok=True)
    Workbook().save(folder / "ContentsOfTheContainer_1.xlsx")

    captured = {}

    def fake_create_batch(thread_id, downstream_file_path=None,
                          upstream_root=None, on_progress=None, **kw):
        captured["on_progress"] = on_progress
        return {"status": "pending_human_review", "thread_id": thread_id}

    monkeypatch.setattr(service, "create_batch", fake_create_batch)
    sentinel = lambda e: None  # noqa: E731
    # 混跑隔离：各测试文件都有自己的监控目录，用时显式指向本文件的
    from app.config import get_settings
    original_watch = get_settings().watch_dir
    get_settings().watch_dir = str(_WATCH)
    try:
        result = service.start_batch_from_scan("XD900-PROGRESS",
                                               on_progress=sentinel)
    finally:
        get_settings().watch_dir = original_watch
    assert result.get("status") == "pending_human_review"
    assert captured["on_progress"] is sentinel


# ---------------------------------------------------------------------------
# 2. 自动 pin 会话到批次
# ---------------------------------------------------------------------------

def test_auto_pin_when_unpinned():
    sid = _fresh_session_id()
    sessions.get_session(sid)  # 建行
    pinned = dispatcher._auto_pin_session(
        sid, "start_scanned_batch", {"folder_name": "XD437-ETD0613"},
        {"status": "pending_human_review"})
    assert pinned == "XD437-ETD0613"
    assert _db_pin(sid) == "XD437-ETD0613"


def test_auto_pin_keeps_existing():
    """已 pin 其他批次的会话不动（避免悄悄改绑）。"""
    sid = _fresh_session_id()
    sessions.get_session(sid)
    with _get_db_session() as db:
        db.get(_ChatSessionOrm, sid).pinned_thread_id = "OLD-BATCH"
        db.commit()
    pinned = dispatcher._auto_pin_session(
        sid, "create_batch", {"thread_id": "NEW-BATCH"}, {"status": "ok"})
    assert pinned is None
    assert _db_pin(sid) == "OLD-BATCH"


def test_auto_pin_skips_error_and_other_tools():
    sid = _fresh_session_id()
    sessions.get_session(sid)
    # 执行失败不绑
    assert dispatcher._auto_pin_session(
        sid, "create_batch", {"thread_id": "X"}, {"error": "boom"}) is None
    # 非建批工具不绑
    assert dispatcher._auto_pin_session(
        sid, "mark_batch_done", {"folder_name": "X"}, {"marked": 1}) is None
    assert _db_pin(sid) is None


def test_confirm_applied_autopins(monkeypatch):
    """confirm 全链路：start_scanned_batch applied 后 session 自动 pin。"""
    sid = _fresh_session_id()
    _plant_pending(sid, "start_scanned_batch", {"folder_name": "XD437-ETD0613"})

    tool = dispatcher_tools.TOOLS["start_scanned_batch"]
    monkeypatch.setattr(
        tool, "execute",
        lambda args, on_progress=None: {
            "status": "pending_human_review",
            "thread_id": "test-93",
        })

    result = dispatcher.confirm(sid, None)
    assert result["status"] == "applied"
    # args 无 thread_id 时取 result 里的（服务真实返回带 thread_id）
    assert result.get("pinned_thread_id") == "test-93"
    assert _db_pin(sid) == "test-93"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
