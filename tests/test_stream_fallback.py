# -*- coding: utf-8 -*-
"""跑图异常统一兜底 + FatalLLMError 401 文案 + LLM 超时收紧 测试。

对应实施计划《2026-10-14-批次终止与日志轮转-实施计划.md》阶段一（1.4 节）：
1. _mark_batch_error 文案分支：FatalLLMError(kind="401") → 含「401」「密钥」；
   kind="403" / kind=None → 含「403」；普通 ValueError → 含异常类型名；
2. 裸奔跑图路径兜底（代表两条，四路径同模式）：
   - rerun_with_paths：stream 抛 RuntimeError → 上抛 + 批次行 error + 留痕；
   - add_factories_to_batch（completed 续跑分支）：stream 抛 FatalLLMError
     → 上抛 + 批次行 error + error_message 含「403」；
   两条均 monkeypatch service.get_graph 假 graph，绝不发真实 LLM 请求；
3. 常量与 client 参数：REQUEST_TIMEOUT==180、MAX_API_RETRIES==2、
   _get_client() 显式传 timeout=180 且 max_retries=0。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_stream_fallback.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_stream_fb_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.extraction import llm_client  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_stream_fb_test_")


@pytest.fixture(autouse=True)
def _settings_singleton_guard():
    """红线 2：settings 单例钉根——保存/恢复全部字段，防全量混跑串味。"""
    s = get_settings()
    before = dict(s.__dict__)
    try:
        yield
    finally:
        s.__dict__.clear()
        s.__dict__.update(before)


# ---------------------------------------------------------------------------
# 假 graph：get_state 返回固定假 snap，update_state 空操作，stream 抛指定异常
# ---------------------------------------------------------------------------

class _FakeSnap:
    def __init__(self, values=None, next_nodes=(), tasks=()):
        self.values = values if values is not None else {}
        self.next = tuple(next_nodes)
        self.tasks = tuple(tasks)


class _FakeGraph:
    def __init__(self, snap: _FakeSnap, stream_exc: Exception):
        self._snap = snap
        self._stream_exc = stream_exc

    def get_state(self, _cfg):
        return self._snap

    def update_state(self, *_a, **_kw):
        return None

    def stream(self, *_a, **_kw):
        raise self._stream_exc


# ---------- 1. _mark_batch_error 文案分支 ----------

def test_mark_batch_error_fatal_401():
    tid = "tid-mbe-401"
    batch_store.upsert_batch(tid, status="running")
    service._mark_batch_error(
        tid, llm_client.FatalLLMError("LLM 调用被拒绝（AuthenticationError）：x",
                                      kind="401"))
    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "401" in row["error_message"]
    assert "密钥" in row["error_message"]


def test_mark_batch_error_fatal_403():
    tid = "tid-mbe-403"
    batch_store.upsert_batch(tid, status="running")
    service._mark_batch_error(
        tid, llm_client.FatalLLMError("LLM 调用被拒绝（PermissionDeniedError）：x",
                                      kind="403"))
    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "403" in row["error_message"]


def test_mark_batch_error_fatal_kind_none_falls_back_to_403():
    """kind 为空（历史构造点）落 403 旧文案兜底。"""
    tid = "tid-mbe-none"
    batch_store.upsert_batch(tid, status="running")
    service._mark_batch_error(tid, llm_client.FatalLLMError("x"))
    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "403" in row["error_message"]


def test_mark_batch_error_plain_value_error():
    tid = "tid-mbe-value"
    batch_store.upsert_batch(tid, status="running")
    service._mark_batch_error(tid, ValueError("坏数据"))
    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "ValueError" in row["error_message"]


# ---------- 2. 裸奔跑图路径兜底（代表两条） ----------

def test_rerun_with_paths_stream_error_marks_batch_error(monkeypatch):
    """stream 抛 RuntimeError → 异常原样上抛，批次行 status=error 且留痕。"""
    tid = "tid-rerun-fallback"
    batch_store.upsert_batch(tid, status="running")
    snap = _FakeSnap(
        values={"batch_id": tid, "downstream_file_path": "x.xlsx"},
        next_nodes=("node5",),
    )
    fake = _FakeGraph(snap, RuntimeError("boom"))
    monkeypatch.setattr(service, "get_graph", lambda: fake)

    with pytest.raises(RuntimeError, match="boom"):
        service.rerun_with_paths(tid, upstream_root="/tmp/u")

    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "RuntimeError" in row["error_message"]
    assert "boom" in row["error_message"]


def test_add_factories_to_batch_stream_error_marks_batch_error(monkeypatch):
    """completed 续跑分支：stream 抛 FatalLLMError(403) → 上抛 + 批次行
    status=error 且 error_message 含「403」。"""
    tid = "tid-add-fallback"
    batch_store.upsert_batch(tid, status="completed")
    service._write_batch_config(tid, {
        "thread_id": tid,
        "downstream_file_path": "/tmp/ContentsOfTheContainer.xlsx",
        "upstream_root": "/tmp/upstream",
    })
    # 装箱单解析 / 输出副本同步 / 自动建夹全部打空操作（绝不碰真实文件）
    monkeypatch.setattr(
        "app.nodes.parse_downstream.parse_downstream_file",
        lambda _p: ({"F1": {"total_qty": 1}}, {"F1": {"row": 2}}))
    monkeypatch.setattr(
        service, "_auto_create_factory_folders", lambda *a, **k: None)
    monkeypatch.setattr(
        service, "_sync_output_copy_rows", lambda *a, **k: None)
    # 假 snap：completed 续跑分支（next 为空、无 interrupt、values 非空）
    snap = _FakeSnap(
        values={
            "batch_id": tid,
            "factory_outputs": {},
            "pending_factories": [],
            "downstream_requirements": {},
            "downstream_row_map": {},
        },
        next_nodes=(),
        tasks=(),
    )
    fake = _FakeGraph(snap, llm_client.FatalLLMError("boom-403", kind="403"))
    monkeypatch.setattr(service, "get_graph", lambda: fake)

    with pytest.raises(llm_client.FatalLLMError):
        service.add_factories_to_batch(tid)

    row = batch_store.get_batch(tid)
    assert row["status"] == "error"
    assert "403" in row["error_message"]


# ---------- 3. 常量与 client 参数 ----------

def test_timeout_and_retry_constants():
    assert llm_client.REQUEST_TIMEOUT == 180
    assert llm_client.MAX_API_RETRIES == 2


def test_get_client_passes_timeout_and_max_retries_zero(monkeypatch):
    """_get_client() 必须显式传 timeout=180 且 max_retries=0（SDK 内部重试
    关闭，全部重试语义集中在应用层 _create_with_retry）；不发真实请求。"""
    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(llm_client, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        llm_client, "get_settings",
        lambda: {"api_key": "sk-test", "base_url": "http://example.invalid/v1"})

    llm_client._get_client()

    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "http://example.invalid/v1"
    assert captured["timeout"] == 180
    assert captured["max_retries"] == 0
