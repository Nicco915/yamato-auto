# -*- coding: utf-8 -*-
"""dispatcher 测试的双引擎剧本注入助手（迁移期回归保障）。

背景：调度 Agent 双引擎并存期（DISPATCHER_ENGINE=legacy|react），
两边各有一条 mock 剧本通道——
- legacy：app.dispatcher.loop._MOCK_SCRIPT（llm_step 逐个 pop 消费）；
- react：app.dispatcher.lc_llm._SCRIPT（_generate 逐个 pop 消费，
  经 set_script/clear_script 操作）。
两条通道的剧本格式完全一致（{"final_text" | "tool_calls": [{"name",
"args"}]}），同一份剧本注入两边，即可让同一套断言在两种引擎下各跑
一遍——这就是迁移期的回归保障：任何一边行为漂移都会立刻变红。

注意两边各自 pop 消费同一份剧本，注入时必须各自深拷贝（一边消费
不得影响另一边尚未运行的副本）。

legacy 引擎删除后，本模块可整体删除（测试回到单引擎直注）。
"""
from __future__ import annotations

import os
from copy import deepcopy
from typing import Callable

from app.dispatcher import lc_llm, loop
from app.dispatcher.sessions import DispatcherSession


def engine() -> str:
    """当前调度引擎（与 app.dispatcher 入口同一判定口径）。

    "react" → create_react_agent 引擎；其余（含缺省）→ legacy 手写循环。
    测试内按引擎条件断言时一律用本函数，不直接读环境变量。
    """
    return "react" if os.environ.get(
        "DISPATCHER_ENGINE", "legacy").strip().lower() == "react" else "legacy"


def set_scripts(items: list[dict]) -> None:
    """同一份剧本注入两个引擎的 mock 通道（各自 pop 消费，需独立深拷贝）。"""
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(deepcopy(items))
    lc_llm.set_script(deepcopy(items))


def clear_scripts() -> None:
    """清空两个引擎的 mock 剧本（用例收尾用，防用例间串味）。"""
    loop._MOCK_SCRIPT.clear()
    lc_llm.clear_script()


def active_script() -> list:
    """当前引擎的剧本队列（活引用）：剧本耗尽断言只认正在跑的这条通道。"""
    return lc_llm._SCRIPT if engine() == "react" else loop._MOCK_SCRIPT


def run_dispatch(message: str, session: DispatcherSession, *, phase: int = 2,
                 session_id: str | None = None,
                 on_progress: Callable[[dict], None] | None = None) -> dict:
    """按当前引擎直调调度主循环（绕过 handle_message 的 triage/入口短路）。

    供需要直调引擎本体的测试用（如 guide/debug_log 测试）；react 引擎
    无 triage_hint 概念（意图判别交还单循环模型），故本助手不接受该参数。
    """
    if engine() == "react":
        from app.dispatcher import react_engine
        return react_engine.run_dispatch_react(
            message, session, phase=phase, session_id=session_id,
            on_progress=on_progress)
    return loop.run_dispatch(message, session, phase=phase,
                             session_id=session_id, on_progress=on_progress)


__all__ = ["engine", "set_scripts", "clear_scripts", "active_script",
           "run_dispatch"]
