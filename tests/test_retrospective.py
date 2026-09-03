# -*- coding: utf-8 -*-
"""复盘摘要生成服务测试。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_retrospective.py -v
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
os.environ["RAG_MOCK"] = "1"
os.environ["RETROSPECTIVE_MOCK"] = "1"

# 先 import app 模块（触发 llm_client load_dotenv override）
from app.config import get_settings  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_release4_test_")

import pytest  # noqa: E402

from app.db import batch_store  # noqa: E402
from app.orchestrator import retrospective  # noqa: E402


def test_atomic_lessons_alias_and_empty_folder():
    """别名写入与空文件夹应生成对应原子 lesson。"""
    batch_id = "B-alias"
    state = {
        "batch_id": batch_id,
        "batch": {},
        "pipeline": {},
        "factory_setup": {
            "alias_written": ["青岛测试 -> 测试别名"],
            "created": ["folderA"],
            "resolved": {"青岛测试": {"folder": "folderA"}},
        },
    }
    lessons = retrospective.atomic_lessons(state)
    categories = {lesson["category"] for lesson in lessons}

    assert "alias_learning" in categories
    assert "factory_match" in categories

    alias = next(lesson for lesson in lessons if lesson["category"] == "alias_learning")
    assert alias["factory_name"] == "青岛测试"
    assert alias["tags"] == ["auto_alias"]

    empty = next(lesson for lesson in lessons if lesson["category"] == "factory_match")
    assert empty["factory_name"] == "青岛测试"
    assert empty["tags"] == ["empty_folder"]


def test_atomic_lessons_downstream_diff():
    """下游推荐 full 时应生成带 full_refresh 标签的 lesson。"""
    state = {
        "batch_id": "B-diff",
        "batch": {},
        "pipeline": {},
        "downstream_diff": {
            "recommendation": "full",
            "reason": "主数据变化",
        },
    }
    lessons = retrospective.atomic_lessons(state)
    lesson = next(lesson for lesson in lessons if lesson["category"] == "downstream_diff")
    assert "full_refresh" in lesson["tags"]


def test_atomic_lessons_review_audit():
    """审核修改应生成带 manual_edit 和 new_sku 标签的 lesson。"""
    state = {
        "batch_id": "B-review",
        "batch": {},
        "pipeline": {},
        "review_audits": [
            {
                "factory_name": "F1",
                "edited_count": 2,
                "new_skus": ["SKU-A", "SKU-B"],
            }
        ],
    }
    lessons = retrospective.atomic_lessons(state)
    lesson = next(lesson for lesson in lessons if lesson["category"] == "review_audit")
    assert "manual_edit" in lesson["tags"]
    assert "new_sku" in lesson["tags"]


def test_atomic_lessons_split_and_declaration():
    """分票与报关单结果应生成 split_rule 和 declaration_gen lesson。"""
    state = {
        "batch_id": "B-split",
        "batch": {},
        "pipeline": {
            "split": {
                "proposal": {"groups": []},
                "declare_result": {
                    "count": 3,
                    "warnings": ["warn1"],
                },
            }
        },
    }
    lessons = retrospective.atomic_lessons(state)
    categories = {lesson["category"] for lesson in lessons}
    assert "split_rule" in categories
    assert "declaration_gen" in categories

    split = next(lesson for lesson in lessons if lesson["category"] == "split_rule")
    assert "split" in split["tags"]

    decl = next(lesson for lesson in lessons if lesson["category"] == "declaration_gen")
    assert "declaration" in decl["tags"]


def test_format_for_kb():
    """format_for_kb 应返回经验库所需字段。"""
    lesson = {
        "lesson_id": "L1",
        "batch_id": "B1",
        "factory_name": "青岛测试",
        "category": "alias_learning",
        "context": {"alias": "测试别名"},
        "decision": "自动写入别名",
        "outcome": {"recorded": True},
        "tags": ["auto_alias"],
    }
    entry = retrospective.format_for_kb(lesson)
    required_keys = {
        "title", "content", "category", "factory", "tags",
        "source_batch_id", "confidence",
    }
    assert required_keys <= set(entry.keys())
    assert entry["category"] == "alias_learning"
    assert entry["factory"] == "青岛测试"
    assert entry["source_batch_id"] == "B1"
    assert 0.0 <= entry["confidence"] <= 1.0


def test_summarize_batch_returns_structure():
    """summarize_batch 应返回完整的复盘结构。"""
    thread_id = "B-sum"
    batch_store.upsert_batch(thread_id=thread_id, status="completed")
    result = retrospective.summarize_batch(thread_id)

    assert result["batch_id"] == thread_id
    assert result["status"] == "completed"
    assert isinstance(result["atomic_lessons"], list)
    assert result["nl_summary"]
    assert result["generated_at"]


def test_summarize_batch_mock_llm_fallback(monkeypatch):
    """LLM 摘要失败时应降级为模板摘要且不抛异常。"""
    def _boom(prompt: str) -> str:
        raise RuntimeError("LLM 不可用")

    monkeypatch.delenv("RETROSPECTIVE_MOCK", raising=False)
    monkeypatch.setattr(retrospective, "_call_llm_summary", _boom)

    result = retrospective.summarize_batch("B-fallback")
    assert result["nl_summary"]
    assert result["batch_id"] == "B-fallback"


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
