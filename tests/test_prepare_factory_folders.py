# -*- coding: utf-8 -*-
"""工厂文件夹预处理 Skill 测试。

覆盖：
- 高置信工厂直接命中；
- 低置信工厂返回候选；
- 无匹配工厂创建空文件夹；
- 别名自动写入与撤销。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_prepare_factory_folders.py -v
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

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Factory, FactoryAlias  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.orchestrator.factory_setup import (  # noqa: E402
    prepare_factory_folders,
    undo_alias_write,
)
from app.nodes.parse_downstream import parse_downstream_file  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

TMP = isolate_to_tmp("yamato_factory_setup_test_")

client = TestClient(app)


def _seed_factory(name: str, short_name: str | None = None, aliases: list[str] | None = None):
    with get_session() as s:
        f = Factory(factory_name=name, short_name=short_name)
        s.add(f)
        s.commit()
        for alias in aliases or []:
            s.add(FactoryAlias(factory_id=f.factory_id, alias=alias, use_folder_match=True))
        s.commit()
        return f.factory_id


def _make_downstream(path: Path, factories: list[str]):
    wb = Workbook()
    ws = wb.active
    ws.append([get_settings().col_factory, get_settings().col_sku])
    for factory in factories:
        ws.append([factory, "1234567890123"])
    wb.save(path)


def test_prepare_resolves_high_confidence():
    downstream = TMP / "downstream1.xlsx"
    _make_downstream(downstream, ["青島中地"])

    upstream = TMP / "up1"
    upstream.mkdir()
    (upstream / "青島中地").mkdir()

    _seed_factory("青島中地", short_name="中地")

    result = prepare_factory_folders(str(downstream), str(upstream))
    assert "error" not in result
    assert "青島中地" in result["resolved"]
    assert result["resolved"]["青島中地"]["folder"] == "青島中地"


def test_prepare_creates_empty_folder_and_alias():
    downstream = TMP / "downstream2.xlsx"
    _make_downstream(downstream, ["青島新星"])

    upstream = TMP / "up2"
    upstream.mkdir()

    _seed_factory("青島新星", short_name="新星")

    result = prepare_factory_folders(str(downstream), str(upstream))
    assert "error" not in result
    assert (upstream / "新星").is_dir()
    assert any("新星" in a for a in result["alias_written"])

    # 撤销别名
    assert undo_alias_write("青島新星", "新星")


def test_prepare_returns_candidates_for_low_confidence():
    downstream = TMP / "downstream3.xlsx"
    _make_downstream(downstream, ["青島正達工芸品"])

    upstream = TMP / "up3"
    upstream.mkdir()
    (upstream / "正達").mkdir()

    # 不写别名，让匹配落到 fuzzy/contains
    result = prepare_factory_folders(str(downstream), str(upstream))
    assert "error" not in result
    assert "青島正達工芸品" in result["candidates"]


def test_prepare_dry_run_no_changes():
    downstream = TMP / "downstream4.xlsx"
    _make_downstream(downstream, ["青島新星2"])

    upstream = TMP / "up4"
    upstream.mkdir()

    result = prepare_factory_folders(
        str(downstream), str(upstream), auto_create_empty=False, auto_write_alias=False
    )
    assert not (upstream / "青島新星2").exists()
    assert result["unmatched"] == ["青島新星2"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
