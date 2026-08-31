# -*- coding: utf-8 -*-
"""调度 Agent 专用调试日志（dispatcher.log）测试。

覆盖：
1. run_dispatch 全链路落盘：user_message / llm_request（完整 messages）/
   llm_response（工具调用全参数）/ tool_result / confirm_gate 各事件齐全；
2. confirm 执行落 confirm_execute；非法确认落 confirm_rejected；
3. 不上控制台：dispatcher_debug logger propagate=False，root 收不到其记录；
4. 敏感参数脱敏（api_key/token 类键值替换为 ***）；
5. 日志绝不抛异常（不可序列化对象静默容错）。

隔离：工具表注入 dummy read/write 工具（不触 db/图/文件），
DISPATCHER_MOCK=1 剧本驱动；日志写到真实 app/data/logs/dispatcher.log
（测试后清理自己写入的验证——只断言文件内容含本会话标记，不删文件：
该文件是滚动日志，可能已有生产记录）。

用例 1 直调 react 引擎本体（_react_script.run_dispatch），
剧本经 _react_script.set_scripts 注入 mock 通道；llm_request 的 mode
字段恒为 "native-lc"——react 引擎只有 native
tool_calls 一种调用形态（lc_llm mock 模式也记 "native-lc"）。

用法（在 app/ 目录下）：
  DISPATCHER_MOCK=1 python3 validation/dispatcher_debug_log_test.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.dispatcher import debug_log, executor, sessions  # noqa: E402
from app.dispatcher.tools import TOOLS, Tool  # noqa: E402

from _react_script import run_dispatch, set_scripts  # noqa: E402

LOG_FILE = APP_ROOT / "app" / "data" / "logs" / "dispatcher.log"

_passed = 0


def check(cond: bool, label: str) -> None:
    global _passed
    assert cond, f"FAIL: {label}"
    _passed += 1
    print(f"  ok {label}")


def read_log_lines(marker: str) -> list[dict]:
    """读 dispatcher.log，返回含本会话标记的 JSONL 记录。"""
    if not LOG_FILE.exists():
        return []
    out = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        if marker in line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# ---- dummy 工具注入（不触 db/图/文件，测试后恢复原表）----
MARK = f"DBGLOG-{int(time.time() * 1000)}"

_orig_tools = dict(TOOLS)
TOOLS["dbg_read"] = Tool(
    name="dbg_read", description="测试只读工具",
    parameters={"type": "object",
                "properties": {"query": {"type": "string"},
                               "api_key": {"type": "string"}},
                "required": ["query"]},
    risk="read",
    func=lambda args: {"echo": args.get("query"), "marker": MARK},
)
TOOLS["dbg_write"] = Tool(
    name="dbg_write", description="测试写工具",
    parameters={"type": "object",
                "properties": {"target": {"type": "string"},
                               "token": {"type": "string"}},
                "required": ["target"]},
    risk="write",
    preview=lambda args: {"summary": f"预览摘要 {MARK}",
                          "lines": ["预览行1"], "warnings": []},
    execute=lambda args, on_progress=None: {"done": True, "marker": MARK},
)

try:
    print("== 1. run_dispatch 全链路事件落盘 ==")
    sid = f"{MARK}-S1"
    # 轮1：read 工具调用 → 轮2：write 工具调用（确认门拦截）
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "dbg_read",
                         "args": {"query": "你好", "api_key": "sk-secret"}}]},
        {"tool_calls": [{"id": "c2", "name": "dbg_write",
                         "args": {"target": "批次A", "token": "tok-123"}}]},
    ])
    session = sessions.get_session(sid)
    result = run_dispatch("测试消息", session, session_id=sid)
    check(result["status"] == "pending_confirmation", "写工具被确认门拦截")

    events = read_log_lines(MARK)
    kinds = [e["event"] for e in events]
    check("user_message" in kinds, "user_message 事件落盘")
    check(kinds.count("llm_request") == 2, "两轮 llm_request 落盘")
    check(kinds.count("llm_response") == 2, "两轮 llm_response 落盘")
    check("tool_result" in kinds, "tool_result 事件落盘")
    check("confirm_gate" in kinds, "confirm_gate 事件落盘")

    req = next(e for e in events if e["event"] == "llm_request")
    check('"role": "system"' in req["messages"]
          and "测试消息" in req["messages"], "llm_request 含完整 prompt")
    # mode 恒为 native-lc：react 引擎只有
    # native tool_calls 一种调用形态，lc_llm mock 模式也记 "native-lc"
    expected_mode = "native-lc"
    check(req["mode"] == expected_mode and req["round"] == 1,
          "llm_request 带 mode/round")

    resp1 = next(e for e in events
                 if e["event"] == "llm_response" and e["round"] == 1)
    check(resp1["tool_calls"][0]["name"] == "dbg_read",
          "llm_response 记录工具名")
    check(resp1["tool_calls"][0]["args"]["query"] == "你好",
          "llm_response 记录完整工具参数")

    tr = next(e for e in events if e["event"] == "tool_result")
    check(tr["tool"] == "dbg_read" and tr["args"]["query"] == "你好",
          "tool_result 记录工具名与参数")
    check(tr["args"]["api_key"] == "***", "tool_result 敏感参数脱敏")
    # 敏感值扫描覆盖参数类事件；llm_request 的 messages 是模型实际看到的
    # prompt 原文（assistant 回写含原始参数），必须逐字保真，不参与脱敏
    param_events = [e for e in events if e["event"] != "llm_request"]
    check("sk-secret" not in json.dumps(param_events, ensure_ascii=False),
          "敏感值在参数类事件中不出现")

    gate = next(e for e in events if e["event"] == "confirm_gate")
    check(gate["tool"] == "dbg_write"
          and gate["args"]["target"] == "批次A"
          and gate["args"]["token"] == "***",
          "confirm_gate 记录参数且脱敏")
    check(MARK in gate["summary"], "confirm_gate 记录预览摘要")

    print("== 2. confirm 执行 / 拒绝落盘 ==")
    applied = executor.execute_confirmed(session, None, session_id=sid)
    check(applied["status"] == "applied", "确认执行成功")
    events = read_log_lines(MARK)
    kinds = [e["event"] for e in events]
    check("confirm_execute" in kinds, "confirm_execute 事件落盘")
    ce = next(e for e in events if e["event"] == "confirm_execute")
    check(ce["tool"] == "dbg_write" and ce["status"] == "applied",
          "confirm_execute 记录工具与状态")
    check(ce["args"]["token"] == "***", "confirm_execute 参数脱敏")

    rejected = executor.execute_confirmed(session, None, session_id=sid)
    check(rejected["status"] == "error", "无 pending 时拒绝")
    events = read_log_lines(MARK)
    check(any(e["event"] == "confirm_rejected" for e in events),
          "confirm_rejected 事件落盘")

    print("== 3. 不上控制台 / 不进 root ==")
    dbg_logger = logging.getLogger("dispatcher_debug")
    check(dbg_logger.propagate is False, "propagate=False")
    check(all(not isinstance(h, logging.StreamHandler)
              or isinstance(h, logging.handlers.RotatingFileHandler)
              for h in dbg_logger.handlers), "无控制台 handler")
    # root 挂捕获 handler，发一条事件，root 不应收到
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    logging.getLogger().addHandler(capture)
    try:
        debug_log.log_event("probe", marker=MARK)
    finally:
        logging.getLogger().removeHandler(capture)
    check(MARK not in stream.getvalue(), "root handler 收不到调试事件")

    print("== 4. 日志绝不抛异常 ==")
    class Unserializable:
        def __repr__(self):
            return "<unserializable>"

        def __str__(self):
            raise RuntimeError("boom")

    debug_log.log_event("edge", obj=Unserializable())  # 不应抛
    debug_log.log_llm_request(session_id=None, round_no=1, mode="native",
                              messages=[{"role": "user", "content": object()}])
    check(True, "不可序列化输入静默容错")

    print("== 5. 超长截断 ==")
    huge = "x" * (debug_log._PROMPT_CAP + 5000)
    debug_log.log_llm_request(session_id=MARK, round_no=9, mode="native",
                              messages=[{"role": "user", "content": huge}])
    events = [e for e in read_log_lines(MARK)
              if e["event"] == "llm_request" and e.get("round") == 9]
    check(events and "截断" in events[-1]["messages"], "超长 prompt 截断标注")

finally:
    TOOLS.clear()
    TOOLS.update(_orig_tools)
    sessions._SESSIONS.clear()

print(f"\n全部通过（{_passed} 项断言）")
