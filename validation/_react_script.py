# -*- coding: utf-8 -*-
"""dispatcher 测试的 react 引擎剧本注入助手。

legacy 引擎删除（2026-08-31）后由 _dual_engine.py 收敛而来：
唯一 mock 通道是 app.dispatcher.lc_llm._SCRIPT（_generate 逐个 pop 消费，
经 set_script/clear_script 操作），剧本格式 {"final_text" | "tool_calls":
[{"name", "args"}]}。
"""
from __future__ import annotations

from typing import Callable

from app.dispatcher import lc_llm
from app.dispatcher.sessions import DispatcherSession


def set_scripts(items: list[dict]) -> None:
    """注入 mock 剧本（react 引擎逐条 pop 消费）。"""
    lc_llm.set_script(items)


def clear_scripts() -> None:
    """清空 mock 剧本（用例收尾用，防用例间串味）。"""
    lc_llm.clear_script()


def active_script() -> list:
    """剧本队列活引用：剧本耗尽断言只认这条通道。"""
    return lc_llm._SCRIPT


def run_dispatch(message: str, session: DispatcherSession, *, phase: int = 2,
                 session_id: str | None = None,
                 on_progress: Callable[[dict], None] | None = None) -> dict:
    """直调 react 引擎本体（绕过 handle_message 的软挂起短路/快路径）。

    供需要直调引擎本体的测试用（如 guide/debug_log 测试）。
    """
    from app.dispatcher import react_engine
    return react_engine.run_dispatch_react(
        message, session, phase=phase, session_id=session_id,
        on_progress=on_progress)


__all__ = ["set_scripts", "clear_scripts", "active_script", "run_dispatch"]
