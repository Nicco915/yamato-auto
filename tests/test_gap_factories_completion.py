# -*- coding: utf-8 -*-
"""缺口工厂完成返回（awaiting_factories 字段）测试。

背景 bug（2026-10 验收）：补充工厂续跑 → 单厂审核提交 → 图跑到 END，
resume_order 无条件返回「数据已成功落库并写入下游表格」，审核页 toast
「本批次全部完成」——但实际只有部分工厂完成，其余仍待补充。

覆盖：
1. service._gap_factories 三态：无 factory_filter / 有 factory_filter 取交集 /
   无缺口（返回空 list）；
2. resume_order 完成返回（假 graph 直通 END）：
   - 有缺口 → status 仍 "success" + awaiting_factories 名单 + message 带
     「待补充」提示；
   - 无缺口 → awaiting_factories 空 list + 原文案不变；
3. run_until_interrupt 完成返回同样带 awaiting_factories 字段；
4. dispatcher summarize 文案分支（纯函数直接构造 result dict）：
   - _sum_submit_review：gaps 非空 → 待补充文案 + 名单行进 summary_lines；
     gaps 空 → 原「审核已全部完成」文案；
   - _sum_batch_like（create_batch/rerun 完成分支）：gaps 非空 → 待补充文案。

绝不发真实 LLM 请求、绝不跑真图（假 graph 空 stream 直通 END）。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_gap_factories_completion.py -v

隔离红线（2026-08-11 事故教训，骨架照抄 test_batch_abort.py）：
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
_TMP = Path(tempfile.mkdtemp(prefix="yamato_gap_factories_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.dispatcher.summarize import _sum_batch_like, _sum_submit_review  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_gap_factories_test_")


@pytest.fixture(autouse=True)
def _no_auto_backup(monkeypatch):
    """结案自动备份统一打成无副作用空函数（骨架照抄 test_awaiting_factories.py）：
    batch_store 是函数内延迟 import，monkeypatch 必须打在
    app.orchestrator.backup 模块属性上。"""
    from app.orchestrator import backup
    monkeypatch.setattr(backup, "trigger_async", lambda reason, **kw: True)
    yield


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
# 假 graph / 假 snap：get_state 首次返回挂起 snap（next 非空），stream 空跑
# 直通 END，之后 get_state 返回完成 snap（next 为空）。绝不跑真图。
# ---------------------------------------------------------------------------

class _FakeSnap:
    def __init__(self, values=None, next_nodes=(), tasks=()):
        self.values = values if values is not None else {}
        self.next = tuple(next_nodes)
        self.tasks = tuple(tasks)


class _ResumeFakeGraph:
    """resume_order 用假 graph：挂起 → 空 stream 直通 END → 完成 snap。"""

    def __init__(self, pending_values: dict, final_values: dict):
        self._pending = _FakeSnap(pending_values, next_nodes=("node5_human_review",))
        self._final = _FakeSnap(final_values, next_nodes=())
        self._streamed = False

    def get_state(self, _cfg):
        return self._final if self._streamed else self._pending

    def stream(self, *_a, **_kw):
        self._streamed = True
        return iter(())

    def update_state(self, *_a, **_kw):
        return None


# ---------------------------------------------------------------------------
# 1. _gap_factories 三态
# ---------------------------------------------------------------------------

def test_gap_factories_no_filter():
    values = {
        "downstream_requirements": {"厂A": ["x"], "厂B": ["y"], "厂C": ["z"]},
        "factory_outputs": {"厂A": {}},
    }
    assert service._gap_factories(values) == ["厂B", "厂C"]


def test_gap_factories_with_factory_filter():
    """有 factory_filter 时缺口口径取交集：filter 外的未处理工厂不算缺口。"""
    values = {
        "downstream_requirements": {"厂A": ["x"], "厂B": ["y"], "厂C": ["z"]},
        "factory_filter": ["厂A", "厂B"],
        "factory_outputs": {"厂A": {}},
    }
    assert service._gap_factories(values) == ["厂B"]


def test_gap_factories_no_gap():
    values = {
        "downstream_requirements": {"厂A": ["x"], "厂B": ["y"]},
        "factory_outputs": {"厂A": {}, "厂B": {}},
    }
    assert service._gap_factories(values) == []


def test_gap_factories_empty_values():
    """防御：空 values / 缺键不抛，返回空 list。"""
    assert service._gap_factories({}) == []


# ---------------------------------------------------------------------------
# 2. resume_order 完成返回（假 graph 直通 END）
# ---------------------------------------------------------------------------

def _final_values(done: list[str], gap: list[str]) -> dict:
    return {
        "downstream_requirements": {f: ["4900000000001"] for f in done + gap},
        "factory_outputs": {f: {"calculated_items": []} for f in done},
        "validation_status": "已通过",
        "final_output_path": "/tmp/out.xlsx",
    }


def test_resume_success_with_gaps_carries_awaiting_factories(monkeypatch):
    """单厂提交图跑到 END、其余工厂待补充：status 仍 success（契约不变），
    awaiting_factories 透出缺口名单，message 带「待补充」提示。"""
    tid = "tid-resume-gap"
    graph = _ResumeFakeGraph(
        pending_values={"batch_id": tid,
                        "current_factory_data": {"factory_name": "厂A"}},
        final_values=_final_values(done=["厂A"], gap=["厂B", "厂C"]),
    )
    monkeypatch.setattr(service, "get_graph", lambda: graph)

    result = service.resume_order(tid, {"approved": True, "items": []})

    assert result["status"] == "success"
    assert result["awaiting_factories"] == ["厂B", "厂C"]
    assert "待补充" in result["message"]
    assert "2 家" in result["message"]
    assert "全部完成" not in result["message"]


def test_resume_success_no_gaps_keeps_original_message(monkeypatch):
    """全部工厂补齐：awaiting_factories 空 list，message 保持原文案。"""
    tid = "tid-resume-full"
    graph = _ResumeFakeGraph(
        pending_values={"batch_id": tid,
                        "current_factory_data": {"factory_name": "厂A"}},
        final_values=_final_values(done=["厂A", "厂B"], gap=[]),
    )
    monkeypatch.setattr(service, "get_graph", lambda: graph)

    result = service.resume_order(tid, {"approved": True, "items": []})

    assert result["status"] == "success"
    assert result["awaiting_factories"] == []
    assert result["message"] == "数据已成功落库并写入下游表格"


# ---------------------------------------------------------------------------
# 3. run_until_interrupt 完成返回同样带 awaiting_factories
# ---------------------------------------------------------------------------

def test_run_until_interrupt_completed_carries_awaiting_factories(monkeypatch):
    tid = "tid-run-gap"
    graph = _ResumeFakeGraph(
        pending_values={},
        final_values=_final_values(done=["厂A"], gap=["厂B"]),
    )
    monkeypatch.setattr(service, "get_graph", lambda: graph)

    result = service.run_until_interrupt(tid)

    assert result["status"] == "completed"
    assert result["awaiting_factories"] == ["厂B"]


# ---------------------------------------------------------------------------
# 4. dispatcher summarize 文案分支（纯函数）
# ---------------------------------------------------------------------------

def test_sum_submit_review_with_gaps():
    result = {
        "status": "success",
        "thread_id": "B-1",
        "awaiting_factories": ["厂B", "厂C"],
        "message": "当前工厂已落库并写入下游表格；本批次仍有 2 家工厂待补充",
        "final_output_path": "/tmp/out.xlsx",
    }
    out = _sum_submit_review({}, result)
    assert "待补充" in out["message"]
    assert "厂B" in out["message"] and "厂C" in out["message"]
    assert "全部完成" not in out["message"]
    assert any("待补充工厂" in line for line in out["summary_lines"])


def test_sum_submit_review_no_gaps_keeps_original():
    result = {
        "status": "success",
        "thread_id": "B-2",
        "awaiting_factories": [],
        "message": "数据已成功落库并写入下游表格",
    }
    out = _sum_submit_review({}, result)
    assert out["message"] == "批次 B-2 审核已全部完成：数据已落库并写入下游表格。"


def test_sum_submit_review_missing_field_defaults_no_gaps():
    """老调用方/老结果无 awaiting_factories 字段：按无缺口处理，不炸。"""
    result = {"status": "success", "thread_id": "B-3"}
    out = _sum_submit_review({}, result)
    assert "全部完成" in out["message"]


def test_sum_batch_like_completed_with_gaps():
    result = {
        "status": "completed",
        "thread_id": "B-4",
        "awaiting_factories": ["厂B"],
        "final_output_path": "/tmp/out.xlsx",
    }
    out = _sum_batch_like("create_batch", result)
    assert "待补充" in out["message"]
    assert "厂B" in out["message"]
    assert "全部工厂处理完成" not in out["message"]


def test_sum_batch_like_completed_no_gaps_keeps_original():
    result = {"status": "completed", "thread_id": "B-5",
              "awaiting_factories": []}
    out = _sum_batch_like("create_batch", result)
    assert "全部工厂处理完成" in out["message"]
