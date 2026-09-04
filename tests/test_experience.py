# -*- coding: utf-8 -*-
"""RAG experience 经验库测试。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_experience.py -v
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

# 先 import app 模块（触发 llm_client load_dotenv override）
from app.config import get_settings  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_release4_test_")

import pytest  # noqa: E402

from app.dispatcher import experience, rag  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_experience_store(monkeypatch):
    """每个用例前清空 experience namespace，并把本地镜像指向临时目录。"""
    rag._MOCK_INDEX.namespaces.pop("experience", None)
    monkeypatch.setattr(experience, "_store_path", lambda: TMP / "experience_entries.json")


def test_add_experience_creates():
    """写入一条新经验应返回 created。"""
    result = experience.add_experience({
        "category": "issue",
        "content": "测试中创建经验的文本内容",
        "factory": "TestFactory",
        "tags": ["release4"],
        "title": "测试标题",
    })
    assert result["action"] == "created"
    assert result["count"] == 1
    assert result["id"]


def test_add_experience_merge():
    """相同 category + factory + content 写入两次应合并。"""
    entry = {
        "category": "issue",
        "content": "合并去重测试文本",
        "factory": "MergeFactory",
        "tags": ["merge"],
        "title": "合并测试",
    }
    first = experience.add_experience(entry)
    assert first["action"] == "created"

    second = experience.add_experience(entry)
    assert second["action"] == "merged"
    assert second["count"] >= 2
    assert second["id"] == first["id"]


def test_search_experience_filters():
    """search_experience 的后过滤条件应生效。"""
    experience.add_experience({
        "category": "issue",
        "content": "搜索过滤测试专用内容",
        "factory": "TestFactory",
        "tags": ["filter"],
        "title": "过滤测试",
    })

    hits = experience.search_experience(
        "搜索过滤测试专用内容",
        filters={"factory": "TestFactory", "category": "issue"},
    )
    assert len(hits) >= 1
    assert hits[0]["metadata"]["factory"] == "TestFactory"

    no_hits = experience.search_experience(
        "搜索过滤测试",
        filters={"factory": "Other"},
    )
    assert no_hits == []


def test_ingest_retrospective():
    """ingest_retrospective 应逐条写入 learning。"""
    retrospective = {
        "thread_id": "T1",
        "factory": "F1",
        "learnings": [
            {
                "category": "fix",
                "content": "学习点 A",
                "tags": ["tag_a"],
                "confidence": 0.9,
            },
            {
                "category": "best_practice",
                "content": "学习点 B",
                "tags": ["tag_b"],
                "confidence": 0.8,
            },
        ],
    }
    result = experience.ingest_retrospective(retrospective)
    assert "ingested" in result
    assert len(result["ingested"]) == len(retrospective["learnings"])
    for item in result["ingested"]:
        assert "action" in item


def test_add_experience_empty_content_error():
    """content 为空时应抛 ValueError。"""
    with pytest.raises(ValueError):
        experience.add_experience({
            "category": "issue",
            "content": "   ",
            "factory": "TestFactory",
        })


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
