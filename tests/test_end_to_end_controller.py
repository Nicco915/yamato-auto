# -*- coding: utf-8 -*-
"""端到端总控编排 controller 测试。

使用 MagicMock 替换底层 LangGraph，不跑真实图。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_end_to_end_controller.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"
os.environ["RAG_MOCK"] = "1"

# 先 import app 模块（触发 llm_client load_dotenv override）
from app.config import get_settings  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_release4_test_")

import pytest  # noqa: E402

from app.orchestrator import controller  # noqa: E402
from app.split.graph import NODE3_SPLIT  # noqa: E402


def _fake_graph(state_values, next_nodes=(), has_interrupt=False):
    """构造一个伪造的分票图对象。"""
    graph = MagicMock()
    snap = MagicMock()
    snap.values = state_values
    snap.next = next_nodes
    snap.tasks = []
    if has_interrupt:
        task = MagicMock()
        task.interrupts = [MagicMock(value={})]
        snap.tasks = [task]
    graph.get_state.return_value = snap
    graph.stream.return_value = iter([])
    graph.update_state.return_value = None
    return graph


def test_advance_batch_not_found(monkeypatch):
    """批次不存在时应抛 ValueError。"""
    monkeypatch.setattr(
        controller, "get_pipeline_state", lambda tid: {"exists": False}
    )
    with pytest.raises(ValueError):
        controller.advance_batch("NO")


def test_advance_batch_human_review(monkeypatch):
    """提取阶段人工审核中断应返回 /review 人工入口。"""
    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "human_review",
            "extract": {
                "has_interrupt": True,
                "current_factory": "青岛测试",
            },
            "split": None,
        },
    )
    result = controller.advance_batch("T")
    assert result["status"] == "needs_human_action"
    assert result["phase"] == "human_review"
    assert result["link"] == "/review?thread_id=T"
    assert "青岛测试" in result["message"]


def test_advance_batch_export_starts_split(monkeypatch):
    """export_done 且无分票线程时应启动分票图并停在 split_review。"""
    def _make_graph():
        graph = _fake_graph({}, next_nodes=())
        # 第一次 get_state 返回空（未启动），第二次返回中断快照
        interrupted_snap = MagicMock()
        interrupted_snap.values = {"proposal": {"groups": []}}
        interrupted_snap.next = (NODE3_SPLIT,)
        task = MagicMock()
        task.interrupts = [MagicMock(value={})]
        interrupted_snap.tasks = [task]
        graph.get_state.side_effect = [
            MagicMock(values={}, next=(), tasks=[]),
            interrupted_snap,
        ]
        graph.stream.return_value = iter([{"__interrupt__": {}}])
        return graph

    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "export_done",
            "extract": {
                "final_output_path": "/tmp/filled.xlsx",
                "has_interrupt": False,
            },
            "split": None,
        },
    )
    monkeypatch.setattr(controller, "get_split_graph", _make_graph)

    result = controller.advance_batch("T")
    assert result["status"] == "needs_human_action"
    assert result["phase"] == "split_review"
    assert result["link"] == "/split?thread_id=T"


def test_advance_batch_split_review(monkeypatch):
    """分票审核阶段中断应返回 /split 人工入口。"""
    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "split_review",
            "extract": {},
            "split": {"has_interrupt": True},
        },
    )
    result = controller.advance_batch("T")
    assert result["status"] == "needs_human_action"
    assert result["phase"] == "split_review"
    assert result["link"] == "/split?thread_id=T"


def test_advance_batch_resume_split_to_completed(monkeypatch):
    """split_persisting 阶段 resume 后图跑完应返回 completed。"""
    completed_snap = MagicMock()
    completed_snap.values = {"batch_id": "T", "status": "completed"}
    completed_snap.next = ()
    completed_snap.tasks = []

    graph = _fake_graph({})
    graph.get_state.return_value = completed_snap

    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "split_persisting",
            "extract": {},
            "split": {"has_interrupt": False},
        },
    )
    monkeypatch.setattr(controller, "get_split_graph", lambda: graph)

    result = controller.advance_batch("T", action={"approved": True})
    assert result["status"] == "completed"


def test_advance_batch_running_phase_raises(monkeypatch):
    """运行中阶段不可推进，应抛 RuntimeError。"""
    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "extraction",
            "extract": {"has_interrupt": False},
            "split": None,
        },
    )
    with pytest.raises(RuntimeError):
        controller.advance_batch("T")


def test_advance_batch_completed(monkeypatch):
    """已完成批次应直接返回 completed。"""
    monkeypatch.setattr(
        controller,
        "get_pipeline_state",
        lambda tid: {
            "exists": True,
            "current_phase": "completed",
            "extract": {},
            "split": None,
        },
    )
    result = controller.advance_batch("T")
    assert result["status"] == "completed"


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
