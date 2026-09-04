# -*- coding: utf-8 -*-
"""流水线状态推导服务测试。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_pipeline_state.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.db import batch_store  # noqa: E402
from app.orchestrator import pipeline_state  # noqa: E402
from app.orchestrator.pipeline_state import _is_stage_done  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_pipeline_state_test_")


def test_is_stage_done_basic():
    """阶段完成判定基础逻辑。"""
    assert _is_stage_done("parse_downstream", "folder_router") is True
    assert _is_stage_done("folder_router", "folder_router") is False
    assert _is_stage_done("extraction", "folder_router") is False
    assert _is_stage_done("human_review", "split_review") is True


def test_get_pipeline_state_not_exists():
    """不存在的 thread_id 返回 exists=False。"""
    result = pipeline_state.get_pipeline_state("NOT-EXIST-THREAD")
    assert result["exists"] is False
    assert result["thread_id"] == "NOT-EXIST-THREAD"


def test_get_pipeline_state_with_batch_only():
    """只有 batch 记录、没有 checkpoint 时返回 batch 状态。"""
    thread_id = "test-batch-only"
    batch_store.upsert_batch(
        thread_id=thread_id,
        watch_dir=str(TMP / "watch"),
        folder_name="test",
        status="running",
    )
    result = pipeline_state.get_pipeline_state(thread_id)
    assert result["exists"] is True
    assert result["thread_id"] == thread_id
    assert result["current_phase"] == "running"
    assert isinstance(result["stages"], list)
    assert len(result["stages"]) > 0


def test_stages_have_required_keys():
    """所有阶段条目包含必要字段。"""
    thread_id = "test-stages-keys"
    batch_store.upsert_batch(
        thread_id=thread_id,
        watch_dir=str(TMP / "watch2"),
        folder_name="test2",
        status="completed",
    )
    result = pipeline_state.get_pipeline_state(thread_id)
    for stage in result["stages"]:
        assert "name" in stage
        assert "label" in stage
        assert "status" in stage
        assert stage["status"] in ("done", "active", "pending")


def test_split_declaration_dir_fields():
    """分票段新增 declaration_dir / declarations_ready 字段：目录有内容才算 ready。"""
    from app.split.graph import NODE5_SPLIT, get_split_graph

    thread_id = "test-split-decl"
    batch_store.upsert_batch(
        thread_id=thread_id,
        watch_dir=str(TMP / "watch3"),
        folder_name="test3",
        status="completed",
    )
    # 直接往 checkpoint 写入一个最小分票状态（不跑图，只造 values）
    graph = get_split_graph()
    cfg = {"configurable": {"thread_id": f"split-{thread_id}"}}
    graph.update_state(cfg, {"status": "completed", "proposal": {"tickets": []}}, as_node=NODE5_SPLIT)

    result = pipeline_state.get_pipeline_state(thread_id)
    split = result["split"]
    assert split is not None
    assert split["declaration_dir"].endswith(str(Path("declarations") / f"split-{thread_id}"))
    assert split["declarations_ready"] is False

    # 目录建出且非空后 ready=True
    decl_dir = Path(split["declaration_dir"])
    decl_dir.mkdir(parents=True, exist_ok=True)
    (decl_dir / "decl.xlsx").write_text("x")
    result2 = pipeline_state.get_pipeline_state(thread_id)
    assert result2["split"]["declarations_ready"] is True


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
