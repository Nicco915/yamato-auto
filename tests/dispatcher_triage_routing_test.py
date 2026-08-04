#!/usr/bin/env python3
"""调度 Agent Triage 路由重构的确定性回归测试（mock 剧本驱动）。

覆盖新路由顺序的关键路径：
soft_pending 消费 → qa → clarify（模型主动） → 黄灯软挂起（0.6–0.8 且写
工具，先于缺参判定） → 缺参（代码生成反问） → 绿灯（>0.8 带 hint 进
loop） → 旧循环兜底；槽位无条件 merge_slots 合并落槽。

运行方式（与 smoke_test 一致，无 pytest 依赖）：
    cd app && python3 tests/dispatcher_triage_routing_test.py

说明：
- 全程 DISPATCHER_MOCK=1：triage 走 _TRIAGE_MOCK_SCRIPT 剧本、loop 走
  _MOCK_SCRIPT 剧本，import 与运行均不触发真实 LLM 调用（无需 API key）；
- run_dispatch 以 monkeypatch 方式替换 app.dispatcher 模块内的引用
  （__init__.py 是 from ... import run_dispatch，必须 patch
  app.dispatcher.run_dispatch 本身），伪造实现记录调用参数后返回
  {"status": "ok", "message": "fake-loop"}，测试结束恢复原引用；
- 每个测试用独立 session_id，测试间清理 triage/loop 剧本与 session。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许从任意 cwd 直接运行本文件（app 包在仓库根下）
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 必须在 import app.dispatcher 之前设置：保证 import 链上任何读取
# DISPATCHER_MOCK 的模块都落在 mock 通道，不产生真实 LLM 副作用
os.environ["DISPATCHER_MOCK"] = "1"
# 本文件测 triage 分诊层（legacy 引擎专属机制），无论外部环境变量如何都钉死
# legacy——防止全量套件以 DISPATCHER_ENGINE=react 外导时被误跑
os.environ["DISPATCHER_ENGINE"] = "legacy"

import app.dispatcher as dispatcher  # noqa: E402
from app.dispatcher import loop as _loop  # noqa: E402
from app.dispatcher import sessions as _sessions  # noqa: E402
from app.dispatcher import triage as _triage  # noqa: E402

# 二次钉死：llm_client 模块 import 时 load_dotenv(override=True) 会把
# 生产 .env 的 DISPATCHER_ENGINE=react 灌进 os.environ 覆盖上面的钉——
# app import 完成后必须重钉（_engine() 在调用时才读，此处钉住即生效）
os.environ["DISPATCHER_ENGINE"] = "legacy"


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------

class _FakeLoop:
    """run_dispatch 拦截器：记录调用，伪造返回，测试结束恢复原引用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._orig = dispatcher.run_dispatch
        dispatcher.run_dispatch = self._fake

    def _fake(self, message, session, *, phase=2, session_id=None,
              on_progress=None, triage_hint=None):
        self.calls.append({
            "message": message,
            "session_id": session_id,
            "triage_hint": triage_hint,
        })
        return {"status": "ok", "message": "fake-loop"}

    def restore(self) -> None:
        dispatcher.run_dispatch = self._orig


def _reset(session_id: str) -> None:
    """每个测试前的环境归位：mock 开关、双剧本清空、session 清除。"""
    os.environ["DISPATCHER_MOCK"] = "1"
    _triage._TRIAGE_MOCK_SCRIPT.clear()
    _loop._MOCK_SCRIPT.clear()
    _sessions._SESSIONS.pop(session_id, None)


def _peek(session_id: str):
    session = _sessions.peek_session(session_id)
    assert session is not None, f"session {session_id} 应已存在"
    return session


# ---------------------------------------------------------------------------
# 场景 1（主修复路径）：黄灯布防 → 「是的」按已存意图带 hint 进 loop
# 另并入兜底断言：布防后发非确认非否定消息 → 清挂起、走正常 triage
# ---------------------------------------------------------------------------

def test_yellow_armed_then_yes_enters_loop():
    sid = "test-路由-1"
    _reset(sid)
    fake = _FakeLoop()
    try:
        yellow = {
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {"thread_id": "test"},
            "reply_message": "您是要创建批次 test 吗？",
            "confidence": 0.75,
        }
        _triage._TRIAGE_MOCK_SCRIPT.append(dict(yellow))
        res = dispatcher.handle_message("开始新批次test", session_id=sid)
        assert res["intent"] == "soft_confirm", res
        assert "您是要创建批次 test 吗？" in res["message"], res
        session = _peek(sid)
        assert session.soft_pending is not None, "黄灯应布防 soft_pending"
        assert session.soft_pending["slots"].get("thread_id") == "test"
        assert fake.calls == [], "黄灯反问阶段不应进 loop"

        # 兜底断言：布防后发非确认非否定的消息 → soft_pending 被清除，
        # 消息走正常新一轮 triage（剧本塞一条 clarify 结果验证走到了）
        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "clarify", "target_tool": "create_batch",
            "extracted_args": {},
            "reply_message": "请补充说明您的需求。",
            "confidence": 0.5,
        })
        res2 = dispatcher.handle_message("等等", session_id=sid)
        assert res2["intent"] == "clarify", res2
        assert res2["message"] == "请补充说明您的需求。", res2
        assert _peek(sid).soft_pending is None, "其他消息应清掉软挂起"
        assert fake.calls == [], "clarify 路径不应进 loop"

        # 重新布防后答「是的」：不再过 triage，直接带 hint 进 loop
        _triage._TRIAGE_MOCK_SCRIPT.append(dict(yellow))
        dispatcher.handle_message("开始新批次test", session_id=sid)
        assert _peek(sid).soft_pending is not None
        res3 = dispatcher.handle_message("是的", session_id=sid)
        assert len(fake.calls) == 1, f"确认后应进一次 loop: {fake.calls}"
        assert fake.calls[0]["triage_hint"] == {
            "target_tool": "create_batch", "args": {"thread_id": "test"},
        }, fake.calls[0]
        assert res3["message"] == "fake-loop", res3
    finally:
        fake.restore()
        _reset(sid)
    print("test_yellow_armed_then_yes_enters_loop: ok")


# ---------------------------------------------------------------------------
# 场景 2：黄灯空槽布防 → 「是的」缺参复检 → 代码反问缺参，槽位保留
# ---------------------------------------------------------------------------

def test_yellow_empty_slots_yes_asks_missing():
    sid = "test-路由-2"
    _reset(sid)
    fake = _FakeLoop()
    try:
        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {},
            "reply_message": "您是要创建批次吗？",
            "confidence": 0.75,
        })
        res = dispatcher.handle_message("开始新批次", session_id=sid)
        assert res["intent"] == "soft_confirm", res
        session = _peek(sid)
        assert session.soft_pending is not None, "黄灯空槽也应布防"
        assert session.soft_pending["slots"] == {}
        assert fake.calls == []

        res2 = dispatcher.handle_message("是的", session_id=sid)
        assert res2["intent"] == "clarify", res2
        assert "请提供批次号" in res2["message"], res2
        assert fake.calls == [], "缺参复检不通过不应进 loop"
        session = _peek(sid)
        assert session.current_target_tool == "create_batch", "槽位应保留"
        assert session.soft_pending is None, "确认消费后软挂起应清除"
    finally:
        fake.restore()
        _reset(sid)
    print("test_yellow_empty_slots_yes_asks_missing: ok")


# ---------------------------------------------------------------------------
# 场景 3：绿灯缺参 → 一律代码生成缺参反问（不播模型 yes/no 文案）
# ---------------------------------------------------------------------------

def test_green_missing_uses_code_reply():
    sid = "test-路由-3"
    _reset(sid)
    fake = _FakeLoop()
    try:
        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {},
            "reply_message": "您是要创建批次吗？确认后我为您生成预览。",
            "confidence": 0.9,
        })
        res = dispatcher.handle_message("开始新批次", session_id=sid)
        assert res["intent"] == "clarify", res
        assert res["message"] == "请提供批次号后再继续。", res
        assert "您是" not in res["message"], res
        assert fake.calls == [], "缺参不应进 loop"
    finally:
        fake.restore()
        _reset(sid)
    print("test_green_missing_uses_code_reply: ok")


# ---------------------------------------------------------------------------
# 场景 4：多轮补齐合槽——缺参反问后下轮补上 thread_id，合槽带 hint 进 loop
# ---------------------------------------------------------------------------

def test_multi_turn_slot_merge():
    sid = "test-路由-4"
    _reset(sid)
    fake = _FakeLoop()
    try:
        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {},
            "reply_message": "",
            "confidence": 0.9,
        })
        res = dispatcher.handle_message("开始新批次", session_id=sid)
        assert res["intent"] == "clarify", res
        assert "请提供批次号" in res["message"], res
        assert _peek(sid).current_target_tool == "create_batch"
        assert fake.calls == []

        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {"thread_id": "test"},
            "reply_message": "",
            "confidence": 0.9,
        })
        res2 = dispatcher.handle_message("批次号是test", session_id=sid)
        assert len(fake.calls) == 1, f"补齐后应带 hint 进 loop: {fake.calls}"
        hint = fake.calls[0]["triage_hint"]
        assert hint is not None and hint["target_tool"] == "create_batch", hint
        assert hint["args"].get("thread_id") == "test", hint
        assert res2["message"] == "fake-loop", res2
    finally:
        fake.restore()
        _reset(sid)
    print("test_multi_turn_slot_merge: ok")


# ---------------------------------------------------------------------------
# 场景 5：中止语义——clarify 且 target_tool 为空 → 槽位全清
# ---------------------------------------------------------------------------

def test_abort_clears_slots():
    sid = "test-路由-5"
    _reset(sid)
    fake = _FakeLoop()
    try:
        # 先让 session 有槽位（同场景 3：绿灯缺参落槽等补齐）
        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "action", "target_tool": "create_batch",
            "extracted_args": {},
            "reply_message": "您是要创建批次吗？确认后我为您生成预览。",
            "confidence": 0.9,
        })
        dispatcher.handle_message("开始新批次", session_id=sid)
        assert _peek(sid).current_target_tool == "create_batch"

        _triage._TRIAGE_MOCK_SCRIPT.append({
            "intent": "clarify", "target_tool": None,
            "extracted_args": {},
            "reply_message": "好的，已取消。",
            "confidence": 0.9,
        })
        res = dispatcher.handle_message("算了不弄了", session_id=sid)
        assert res["intent"] == "clarify", res
        session = _peek(sid)
        assert session.current_slots == {}, "中止后槽位应清空"
        assert session.current_target_tool is None, "中止后目标工具应清空"
        assert session.soft_pending is None
        assert fake.calls == [], "中止不应进 loop"
    finally:
        fake.restore()
        _reset(sid)
    print("test_abort_clears_slots: ok")


if __name__ == "__main__":
    test_yellow_armed_then_yes_enters_loop()
    test_yellow_empty_slots_yes_asks_missing()
    test_green_missing_uses_code_reply()
    test_multi_turn_slot_merge()
    test_abort_clears_slots()
    print("dispatcher_triage_routing_test: PASS")
