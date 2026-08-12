import os

import pytest


@pytest.fixture(autouse=True)
def test_mode_guard(monkeypatch):
    monkeypatch.setenv("YAMATO_TEST_MODE", "1")


def test_system_prompt_mentions_barcode_alias():
    from app.extraction.prompts import SYSTEM_PROMPT

    assert "BARCODE" in SYSTEM_PROMPT


def test_system_prompt_explains_standalone_barcode_ownership():
    from app.extraction.prompts import SYSTEM_PROMPT

    assert "独立成行" in SYSTEM_PROMPT or "独立一行" in SYSTEM_PROMPT
    assert "上方最近的商品行" in SYSTEM_PROMPT or "最近的商品行" in SYSTEM_PROMPT


def test_sku_code_rule_remains_rule_seven():
    from app.extraction.prompts import SYSTEM_PROMPT

    rule_lines = [line for line in SYSTEM_PROMPT.splitlines() if line and line[0].isdigit()]
    sku_rule = next(line for line in rule_lines if "sku_code" in line)

    assert sku_rule.startswith("7.")
