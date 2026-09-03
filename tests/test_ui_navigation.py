#!/usr/bin/env python3
"""UI 导航工具测试：go_to_page / open_link 工具 + session 状态管理 + 引擎返回契约。

运行方式：
    cd app && python3 tests/test_ui_navigation.py

说明：
- DISPATCHER_MOCK=1：不产生真实 LLM 调用
- 全程 monkeypatch，测试结束恢复原引用
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许从任意 cwd 直接运行本文件
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 必须在 import app.dispatcher 之前设置
os.environ["DISPATCHER_MOCK"] = "1"

import app.dispatcher as dispatcher  # noqa: E402
from app.dispatcher import lc_llm  # noqa: E402
from app.dispatcher import sessions as _sessions  # noqa: E402
from app.dispatcher import tools as _tools  # noqa: E402


def test_go_to_page_tool_exists():
    """go_to_page 工具存在于 TOOLS 注册表。"""
    assert "go_to_page" in _tools.TOOLS
    tool = _tools.TOOLS["go_to_page"]
    assert tool.name == "go_to_page"
    assert tool.risk == "ui"
    assert tool.func is None
    assert tool.preview is None
    assert tool.execute is None
    print("  ✓ test_go_to_page_tool_exists")


def test_open_link_tool_exists():
    """open_link 工具存在于 TOOLS 注册表。"""
    assert "open_link" in _tools.TOOLS
    tool = _tools.TOOLS["open_link"]
    assert tool.name == "open_link"
    assert tool.risk == "ui"
    assert tool.func is None
    assert tool.preview is None
    assert tool.execute is None
    print("  ✓ test_open_link_tool_exists")


def test_go_to_page_parameters():
    """go_to_page 工具参数 schema 正确。"""
    tool = _tools.TOOLS["go_to_page"]
    params = tool.parameters
    assert params["type"] == "object"
    assert "page" in params["properties"]
    assert params["properties"]["page"]["enum"] == ["review", "split", "batch", "chat"]
    assert "thread_id" in params["properties"]
    assert "label" in params["properties"]
    assert "page" in params["required"]
    assert "thread_id" in params["required"]
    print("  ✓ test_go_to_page_parameters")


def test_open_link_parameters():
    """open_link 工具参数 schema 正确。"""
    tool = _tools.TOOLS["open_link"]
    params = tool.parameters
    assert params["type"] == "object"
    assert "href" in params["properties"]
    assert "label" in params["properties"]
    assert "href" in params["required"]
    assert "label" in params["required"]
    print("  ✓ test_open_link_parameters")


def test_visible_tools_includes_ui_navigation():
    """visible_tools(phase=1) 包含新的 UI 导航工具。"""
    tools = _tools.visible_tools(phase=1)
    names = [t.name for t in tools]
    assert "go_to_page" in names
    assert "open_link" in names
    assert "request_file_selection" in names
    assert "create_batch" not in names
    print("  ✓ test_visible_tools_includes_ui_navigation")


def test_session_go_to_page_request():
    """Session go_to_page 挂起状态管理。"""
    session_id = "test-nav-session-1"
    session = _sessions.get_session(session_id)

    assert session.pending_ui_navigation is None
    _sessions.set_go_to_page_request(
        session, page="review", thread_id="ETD0725", label="去审核")

    assert session.pending_ui_navigation is not None
    assert session.pending_ui_navigation["kind"] == "go_to_page"
    assert session.pending_ui_navigation["page"] == "review"
    assert session.pending_ui_navigation["thread_id"] == "ETD0725"
    assert session.pending_ui_navigation["label"] == "去审核"
    assert "created_at" in session.pending_ui_navigation

    _sessions.clear_ui_navigation_request(session)
    assert session.pending_ui_navigation is None

    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_session_go_to_page_request")


def test_session_open_link_request():
    """Session open_link 挂起状态管理。"""
    session_id = "test-nav-session-2"
    session = _sessions.get_session(session_id)

    assert session.pending_ui_navigation is None
    _sessions.set_open_link_request(
        session, href="https://example.com", label="示例链接")

    assert session.pending_ui_navigation is not None
    assert session.pending_ui_navigation["kind"] == "open_link"
    assert session.pending_ui_navigation["href"] == "https://example.com"
    assert session.pending_ui_navigation["label"] == "示例链接"

    _sessions.clear_ui_navigation_request(session)
    assert session.pending_ui_navigation is None

    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_session_open_link_request")


def test_handle_message_go_to_page():
    """mock LLM 调用 go_to_page 后返回 pending_ui_navigation。"""
    session_id = "test-nav-session-3"
    lc_llm.set_script([
        {"tool_calls": [{"name": "go_to_page",
                         "args": {"page": "split", "thread_id": "ETD0725"}}]},
    ])
    try:
        result = dispatcher.handle_message(
            "打开分票页", session_id, on_progress=lambda e: None)
        assert result["status"] == "pending_ui_navigation"
        nav = result["navigation"]
        assert nav["kind"] == "go_to_page"
        assert nav["page"] == "split"
        assert nav["thread_id"] == "ETD0725"
        assert nav["href"] is None
        assert nav["label"] == "打开分票页"
        assert "打开分票页" in result["message"]

        session = _sessions.get_session(session_id)
        assert session.pending_ui_navigation is not None
        assert session.pending_ui_navigation["page"] == "split"
    finally:
        lc_llm.clear_script()
        _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_handle_message_go_to_page")


def test_handle_message_open_link():
    """mock LLM 调用 open_link 后返回 pending_ui_navigation。"""
    session_id = "test-nav-session-4"
    lc_llm.set_script([
        {"tool_calls": [{"name": "open_link",
                         "args": {"href": "https://example.com/doc",
                                  "label": "查看文档"}}]},
    ])
    try:
        result = dispatcher.handle_message(
            "给我看文档", session_id, on_progress=lambda e: None)
        assert result["status"] == "pending_ui_navigation"
        nav = result["navigation"]
        assert nav["kind"] == "open_link"
        assert nav["href"] == "https://example.com/doc"
        assert nav["label"] == "查看文档"
        assert nav["page"] is None
        assert nav["thread_id"] is None
        assert "查看文档" in result["message"]

        session = _sessions.get_session(session_id)
        assert session.pending_ui_navigation is not None
        assert session.pending_ui_navigation["href"] == "https://example.com/doc"
    finally:
        lc_llm.clear_script()
        _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_handle_message_open_link")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_go_to_page_tool_exists,
        test_open_link_tool_exists,
        test_go_to_page_parameters,
        test_open_link_parameters,
        test_visible_tools_includes_ui_navigation,
        test_session_go_to_page_request,
        test_session_open_link_request,
        test_handle_message_go_to_page,
        test_handle_message_open_link,
    ]

    print(f"\n=== UI 导航工具测试（{len(tests)} 个用例）===\n")

    failed = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"  ✗ {t.__name__}: {e}")

    print()
    if failed:
        print(f"失败 {len(failed)}/{len(tests)}")
        for name, e in failed:
            print(f"  - {name}: {e}")
        sys.exit(1)
    else:
        print(f"全部通过 {len(tests)}/{len(tests)}")


if __name__ == "__main__":
    main()
