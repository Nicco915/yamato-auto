# -*- coding: utf-8 -*-
"""调度 Agent 确认卡「取消」服务端通道测试（2026-09 生产事故回归）。

事故：前端确认卡「取消」只动本地 UI，服务端 pending_action（内存 +
chat_sessions.pending_action_json 写穿）残留，后续写工具一律被
「已有一个待确认的操作」拒绝，用户无法发起任何新批次。

修复：dispatcher.cancel_pending + POST /api/v1/dispatcher/chat
{cancel: true}，前端 btnNo 点击时调用。

覆盖：
1. cancel_pending 清内存 pending_action + DB 写穿 NULL + 审计留痕
   （tool_history confirmed=False）+ 对话历史记录；
2. 幂等：无待确认操作时返回 ok（不报错）；
3. 无 session_id 时幂等返回 ok；
4. 取消后同一个会话可立即存新确认卡（不再被「已有一个待确认」拒绝）；
5. API 端点 {cancel: true} 走通。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_cancel_pending_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import json
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

# 血泪红线：先 import 全部 app 模块（load_dotenv override 会打回真实路径），
# 再 isolate_to_tmp
from app import dispatcher  # noqa: E402
from app.api.main import app  # noqa: E402
from app.db.models import ChatSession as _ChatSessionOrm  # noqa: E402
from app.db.session import get_session as _get_db_session  # noqa: E402
from app.dispatcher import lc_tools, sessions  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

_WATCH = Path(tempfile.mkdtemp(prefix="yamato_cancel_watch_"))

TMP = isolate_to_tmp("yamato_dispatcher_cancel_",
                     extra_env={"WATCH_DIR": str(_WATCH)})

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def _fresh_session_id() -> str:
    return f"test-cancel-{uuid.uuid4().hex[:12]}"


def _plant_pending(session_id: str, tool: str = "mark_batch_done") -> None:
    """模拟影子确认门存卡：往 session 放一张待确认 action 并写穿 DB。"""
    session = sessions.get_session(session_id)
    session.pending_action = {
        "kind": "dispatcher_tool",
        "tool": tool,
        "args": {"folder_name": "XD000-TEST"},
        "summary": "测试用待确认操作",
        "preview_lines": ["文件夹: XD000-TEST"],
        "warnings": [],
        "created_at": time.time(),
    }
    sessions.persist_pending(session)


def _db_pending_json(session_id: str):
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, session_id)
        return row.pending_action_json if row else None


def test_cancel_clears_memory_and_db():
    """取消后：内存 pending_action 清空、DB 写穿 NULL、审计留痕 confirmed=False。"""
    sid = _fresh_session_id()
    _plant_pending(sid)
    assert _db_pending_json(sid) is not None  # 写穿已生效

    result = dispatcher.cancel_pending(sid)

    assert result["status"] == "cancelled"
    session = sessions.get_session(sid)
    assert session.pending_action is None
    assert session.soft_pending is None
    assert _db_pending_json(sid) is None  # DB 写穿 NULL

    # 审计留痕：工具历史里有 confirmed=False 的取消记录
    cancel_records = [t for t in session.tool_history
                      if t["tool"] == "mark_batch_done" and t["confirmed"] is False]
    assert cancel_records, "取消应写入工具审计流水（confirmed=False）"
    assert cancel_records[-1]["result_summary"] == "用户取消，未执行"

    # 对话历史记录取消（LLM 后续轮次知道这张卡已作废）
    assert any("[取消操作]" in h["content"] for h in session.history
               if h["role"] == "user")


def test_cancel_idempotent_without_pending():
    """没有待确认操作时取消：幂等返回 ok，不报错。"""
    sid = _fresh_session_id()
    sessions.get_session(sid)
    result = dispatcher.cancel_pending(sid)
    assert result["status"] == "ok"
    assert "没有待确认" in result["message"]


def test_cancel_without_session_id():
    """无 session_id（临时会话）：幂等返回 ok。"""
    result = dispatcher.cancel_pending(None)
    assert result["status"] == "ok"


def test_cancel_unblocks_new_pending():
    """事故场景回归：取消后同会话可立即存新确认卡（不再被存卡锁拒绝）。"""
    sid = _fresh_session_id()
    folder = _WATCH / "XD000-TEST"
    folder.mkdir(parents=True, exist_ok=True)

    _plant_pending(sid)
    session = sessions.get_session(sid)

    # 取消前：再存卡被拒（存卡锁正常工作）
    outcome = lc_tools.build_pending_action(
        "mark_batch_done", {"folder_name": "XD000-TEST"}, session)
    assert outcome["ok"] is False
    assert "已有一个待确认" in outcome["msg_text"]

    # 取消后立即能存新卡
    dispatcher.cancel_pending(sid)
    outcome = lc_tools.build_pending_action(
        "mark_batch_done", {"folder_name": "XD000-TEST"}, session)
    assert outcome["ok"] is True, outcome["msg_text"]
    assert outcome["action"] is not None
    # 善后：清掉测试存的卡
    dispatcher.cancel_pending(sid)


def test_api_cancel_endpoint():
    """POST /api/v1/dispatcher/chat {cancel: true} 释放服务端 pending。"""
    sid = _fresh_session_id()
    _plant_pending(sid)

    resp = client.post("/api/v1/dispatcher/chat",
                       json={"cancel": True, "session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert sessions.get_session(sid).pending_action is None
    assert _db_pending_json(sid) is None

    # 再取消一次：幂等
    resp = client.post("/api/v1/dispatcher/chat",
                       json={"cancel": True, "session_id": sid})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
