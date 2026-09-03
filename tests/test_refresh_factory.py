# -*- coding: utf-8 -*-
"""刷新单个工厂 Skill 轻量测试。

当前只验证 preview/execute 的调用契约与参数校验；
完整图执行路径在 UI 人工走查中覆盖。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_refresh_factory.py -v
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

from app.dispatcher import tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_refresh_factory_test_")


def test_refresh_factory_preview_requires_args():
    result = tools._preview_refresh_factory({}, None)
    assert "缺少" in result["summary"]


def test_refresh_factory_execute_requires_args():
    result = tools._execute_refresh_factory({"thread_id": "T"}, None)
    assert result["status"] == "error"


def test_refresh_factory_execute_missing_batch():
    result = tools._execute_refresh_factory(
        {"thread_id": "NOT-EXIST", "factory": "中地"}, None
    )
    assert result["status"] == "error"
    assert "不存在" in result["message"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
