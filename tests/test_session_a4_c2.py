import os
from pathlib import Path

import pytest

os.environ.setdefault("YAMATO_TEST_MODE", "1")

from app.extraction import session as session_mod
from app.extraction.schemas import ExtractedItem
from app.extraction.target_identifier import FileProfile


def item(sku_code=None, qty=1, source="source.xlsx"):
    return ExtractedItem(sku_code=sku_code, sku_name="name", total_quantity=qty,
                         total_net_weight=1, total_gross_weight=2, source_file=source)


def test_merge_rejects_nonempty_non_13_digit_sku_with_review_and_dump():
    session = session_mod.FactorySession("f")
    invalid = item("123456789012", source="x.xlsx")
    new, updated = session_mod._merge(session, [invalid], "x.xlsx")
    assert new == [] and updated == []
    assert session.items == {}
    assert session.no_code_items[0]["sku_code"] == "123456789012"
    assert session.no_code_items[0]["needs_human_review"] is True
    assert "SKU_NON_13_DIGIT" in session.no_code_items[0]["review_reason"]
    assert session.issues[-1]["type"] == "SKU_NON_13_DIGIT"


def test_merge_preserves_none_sku_existing_behavior():
    session = session_mod.FactorySession("f")
    dump = item(None).model_dump()
    session_mod._merge(session, [item(None)], "x.xlsx")
    assert session.no_code_items == [dump]
    assert session.issues == []


@pytest.fixture
def routed(monkeypatch):
    def route(path, **kwargs):
        return type("R", (), {"items": [item("1234567890123", source=path)], "notes": [], "error": ""})()
    monkeypatch.setattr(session_mod, "_route_extract", route)
    monkeypatch.setattr(session_mod, "scan_file", lambda path: FileProfile(path, "xlsx", {"1234567890123"}, True, True, True))


def test_higher_name_score_supersedes_old_target_and_extracts(routed, monkeypatch):
    monkeypatch.setattr(session_mod, "_name_score", lambda path: 8 if "new" in path else 2)
    session = session_mod.FactorySession("f")
    old = "/tmp/old.xlsx"
    session.targets.append({"path": old, "barcodes": ["1234567890123"], "name_score": -1000, "forced": False})
    result = session_mod.process_file(session, "/tmp/new.xlsx")
    assert result.action == "replaced_target"
    assert session.targets[0]["path"] == "/tmp/new.xlsx"
    assert session.file_records.get(old) is None
    assert any(i["type"] == "TARGET_SUPERSEDED" for i in session.issues)
    assert "1234567890123" in session.items


def test_lower_or_equal_name_score_is_ignored_duplicate(routed, monkeypatch):
    monkeypatch.setattr(session_mod, "_name_score", lambda path: 2)
    session = session_mod.FactorySession("f")
    session.targets.append({"path": "/tmp/old.xlsx", "barcodes": ["1234567890123"], "name_score": 2, "forced": False})
    result = session_mod.process_file(session, "/tmp/new.xlsx")
    assert result.action == "ignored_duplicate"
    assert "/tmp/new.xlsx" in session.file_records


def test_strict_subset_is_ignored_subset(routed, monkeypatch):
    monkeypatch.setattr(session_mod, "_name_score", lambda path: 8)
    session = session_mod.FactorySession("f")
    session.targets.append({"path": "/tmp/old.xlsx", "barcodes": ["1234567890123", "2234567890123"], "name_score": 2, "forced": False})
    monkeypatch.setattr(session_mod, "scan_file", lambda path: FileProfile(path, "xlsx", {"1234567890123"}, True, True, True))
    result = session_mod.process_file(session, "/tmp/new.xlsx")
    assert result.action == "ignored_subset"
