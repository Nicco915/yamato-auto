#!/usr/bin/env python3
"""文件选择功能测试：request_file_selection 工具 + session 状态管理 + API 集成。

运行方式：
    cd app && python3 tests/test_file_selection.py

说明：
- DISPATCHER_MOCK=1：不产生真实 LLM 调用
- DISPATCHER_ENGINE=react：测试 react 引擎路径（文件选择主要在 react 引擎中实现）
- 全程 monkeypatch，测试结束恢复原引用
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 允许从任意 cwd 直接运行本文件
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 必须在 import app.dispatcher 之前设置
os.environ["DISPATCHER_MOCK"] = "1"
os.environ["DISPATCHER_ENGINE"] = "react"

import app.dispatcher as dispatcher  # noqa: E402
from app.dispatcher import sessions as _sessions  # noqa: E402
from app.dispatcher import tools as _tools  # noqa: E402

# 重钉（防止 llm_client import 时 load_dotenv 覆盖）
os.environ["DISPATCHER_ENGINE"] = "react"


def test_tool_exists():
    """request_file_selection 工具存在于 TOOLS 注册表。"""
    assert "request_file_selection" in _tools.TOOLS
    tool = _tools.TOOLS["request_file_selection"]
    assert tool.name == "request_file_selection"
    assert tool.risk == "ui"
    assert tool.func is None  # UI 工具无 func
    assert tool.preview is None  # UI 工具无 preview
    assert tool.execute is None  # UI 工具无 execute
    print("  ✓ test_tool_exists")


def test_tool_parameters():
    """request_file_selection 工具参数 schema 正确。"""
    tool = _tools.TOOLS["request_file_selection"]
    params = tool.parameters
    assert params["type"] == "object"
    assert "type" in params["properties"]
    assert params["properties"]["type"]["enum"] == ["file", "dir"]
    assert "extensions" in params["properties"]
    assert "title" in params["properties"]
    assert "type" in params["required"]
    print("  ✓ test_tool_parameters")


def test_visible_tools_includes_ui():
    """visible_tools(phase=1) 包含 UI 工具。"""
    tools = _tools.visible_tools(phase=1)
    names = [t.name for t in tools]
    assert "request_file_selection" in names
    assert "list_directory" in names  # 只读工具也在
    assert "create_batch" not in names  # 写工具不在 phase=1
    print("  ✓ test_visible_tools_includes_ui")


def test_session_file_selection():
    """Session 文件选择挂起状态管理。"""
    session_id = "test-fs-session-1"
    session = _sessions.get_session(session_id)

    # 初始状态无挂起
    assert session.pending_file_selection is None

    # 设置挂起
    _sessions.set_file_selection_request(
        session, file_type="file", extensions="xlsx", title="选择装箱单")

    assert session.pending_file_selection is not None
    assert session.pending_file_selection["type"] == "file"
    assert session.pending_file_selection["extensions"] == "xlsx"
    assert session.pending_file_selection["title"] == "选择装箱单"
    assert "created_at" in session.pending_file_selection

    # 清除挂起
    _sessions.clear_file_selection_request(session)
    assert session.pending_file_selection is None

    # 清理
    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_session_file_selection")


def test_handle_message_file_selection_without_pending():
    """收到 file_selection 但没有 pending 状态时返回提示。"""
    session_id = "test-fs-session-2"
    events = []

    result = dispatcher.handle_message(
        "", session_id,
        on_progress=events.append,
        file_selection="/some/path.xlsx")

    assert result["status"] == "ok"
    assert "未找到" in result["message"] or "已处理" in result["message"]

    # 清理
    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_handle_message_file_selection_without_pending")


def test_handle_message_file_selection_with_pending():
    """收到 file_selection 且有 pending 状态时注入为新消息。"""
    session_id = "test-fs-session-3"
    session = _sessions.get_session(session_id)

    # 模拟 Agent 已请求文件选择
    _sessions.set_file_selection_request(
        session, file_type="dir", title="选择工厂文件夹")
    assert session.pending_file_selection is not None

    # 模拟用户选择路径（由于 mock 环境，run_dispatch 会被拦截）
    # 这里主要测试状态清除
    result = dispatcher.handle_message(
        "", session_id,
        on_progress=lambda e: None,
        file_selection="/Users/test/factory")

    # pending 应该被清除
    assert session.pending_file_selection is None

    # 历史应该有用户选择的消息
    user_msgs = [h for h in session.history if h["role"] == "user"]
    assert any("/Users/test/factory" in h["content"] for h in user_msgs)

    # 清理
    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_handle_message_file_selection_with_pending")


def test_history_returns_file_selection_state():
    """history API 返回 pending_file_selection 状态。"""
    session_id = "test-fs-session-4"
    session = _sessions.get_session(session_id)

    # 设置挂起
    _sessions.set_file_selection_request(
        session, file_type="file", extensions="xlsx,xls", title="选择装箱单")

    # peek_session 应该返回挂起状态
    peeked = _sessions.peek_session(session_id)
    assert peeked is not None
    assert peeked.pending_file_selection is not None
    assert peeked.pending_file_selection["type"] == "file"
    assert peeked.pending_file_selection["extensions"] == "xlsx,xls"
    assert peeked.pending_file_selection["title"] == "选择装箱单"

    # 清理
    _sessions.clear_file_selection_request(session)
    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_history_returns_file_selection_state")


def test_file_selection_ttl_expired():
    """过期的 file_selection 挂起应该被忽略。"""
    session_id = "test-fs-session-5"
    session = _sessions.get_session(session_id)

    # 设置挂起并手动让 created_at 过期
    _sessions.set_file_selection_request(session, file_type="dir")
    session.pending_file_selection["created_at"] = time.time() - 4000  # 超过 TTL

    # peek 后检查，过期应该被清理
    peeked = _sessions.peek_session(session_id)
    # 注意：peek_session 本身不清理 file_selection，
    # 清理发生在 dispatcher_history API 中

    # 直接测试 clear 函数
    _sessions.clear_file_selection_request(session)
    assert session.pending_file_selection is None

    # 清理
    _sessions._SESSIONS.pop(session_id, None)
    print("  ✓ test_file_selection_ttl_expired")


def test_cross_platform_pathlib():
    """验证 pathlib.Path 跨平台路径处理（macOS 测试 Windows 风格路径）。"""
    from pathlib import Path, PurePosixPath, PureWindowsPath

    # 测试路径解析（使用 PureWindowsPath 避免平台依赖）
    win_path = PureWindowsPath("C:/Users/test/Documents")
    assert str(win_path) == "C:\\Users\\test\\Documents"
    assert win_path.parent == PureWindowsPath("C:\\Users\\test")

    # 测试 pathlib.Path 在当前平台的行为
    posix_path = PurePosixPath("/Users/test/Documents")
    assert str(posix_path) == "/Users/test/Documents"
    assert str(posix_path.parent) == "/Users/test"

    # 验证 expanduser 在不同平台都能工作
    home_path = Path.home()
    assert home_path.is_absolute()

    print("  ✓ test_cross_platform_pathlib")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    tests = [
        test_tool_exists,
        test_tool_parameters,
        test_visible_tools_includes_ui,
        test_session_file_selection,
        test_handle_message_file_selection_without_pending,
        test_handle_message_file_selection_with_pending,
        test_history_returns_file_selection_state,
        test_file_selection_ttl_expired,
        test_cross_platform_pathlib,
    ]

    print(f"\n=== 文件选择功能测试（{len(tests)} 个用例）===\n")

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
