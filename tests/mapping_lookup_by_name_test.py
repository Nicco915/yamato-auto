# -*- coding: utf-8 -*-
"""S3：产品映射 lookup-by-name 端点测试（审核页新 SKU 品名失焦自动带出税号/商检）。

覆盖：
1. 精确命中：返回 found=true + hs_code/inspection_required/name_en/unit_code；
2. 前后空白 strip 后仍能命中；
3. 未命中 found=false；品名为空 400；
4. 精确匹配非 LIKE——部分串不命中；
5. 同品名多行取 updated_at 最新一条，响应带 ambiguous=true。

前端规则（无浏览器环境不强测，注释固化契约）：
- 仅新 SKU 卡（is_new_sku || _added）品名失焦才触发查询；
- 【铁律】税号字段当前为空才发起查询并填充——绝不覆盖人工已填内容；
- 商检字段同理仅在为空时带出；命中后标 _modified（与手填同路径，提交契约一致）；
- 未命中 / 网络异常一律静默，不打断审核流。

隔离（血泪红线）：先 import 全部 app 模块，再 isolate_to_tmp，绝不碰真实库。

用法（在 app/ 目录下）：
  PYTHONPATH=. python3 -m pytest tests/mapping_lookup_by_name_test.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---- env 前置（需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.db.models import ProductMapping  # noqa: E402
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）；
# get_engine 惰性单例，首次 get_session 才按隔离后的 settings 建临时库并自动建表
TMP = isolate_to_tmp("yamato_mapping_lookup_test_")

client = TestClient(app)

_BASE_TS = datetime(2026, 8, 1, 12, 0, 0)


def _add_mapping(name: str, hs_code: str | None = None, inspection: bool = False,
                 name_en: str | None = None, unit_code: str | None = None,
                 updated_at: datetime | None = None) -> int:
    """直接写库种一条产品映射（updated_at 显式给定，规避 SQLite 秒级分辨率并列）。"""
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=name,
            hs_code=hs_code,
            inspection_required=inspection,
            name_en=name_en,
            unit_code=unit_code,
            is_incomplete=not hs_code,
        )
        if updated_at is not None:
            m.updated_at = updated_at
        s.add(m)
        s.commit()
        s.refresh(m)
        return m.id


def test_lookup_exact_hit():
    """精确命中：全字段带回，ambiguous 不带（单行）。"""
    _add_mapping("不锈钢保温杯", hs_code="9617009000", inspection=True,
                 name_en="STAINLESS STEEL BOTTLE", unit_code="007",
                 updated_at=_BASE_TS)
    r = client.get("/api/v1/mappings/products/lookup-by-name",
                   params={"name": "不锈钢保温杯"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] is True
    assert d["product_name_cn"] == "不锈钢保温杯"
    assert d["hs_code"] == "9617009000"
    assert d["inspection_required"] is True
    assert d["name_en"] == "STAINLESS STEEL BOTTLE"
    assert d["unit_code"] == "007"
    assert d["ambiguous"] is False


def test_lookup_strips_whitespace():
    """前后空白 strip 后命中（人工输入常带空格）。"""
    r = client.get("/api/v1/mappings/products/lookup-by-name",
                   params={"name": "  不锈钢保温杯  "})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] is True
    assert d["hs_code"] == "9617009000"


def test_lookup_miss_returns_found_false():
    """未命中 → found=false（前端静默，不打断审核流）。"""
    r = client.get("/api/v1/mappings/products/lookup-by-name",
                   params={"name": "映射表里不存在的品名"})
    assert r.status_code == 200, r.text
    assert r.json() == {"found": False}


def test_lookup_is_exact_not_like():
    """精确匹配而非 LIKE：部分串不命中，避免误带出相似品名的税号。"""
    r = client.get("/api/v1/mappings/products/lookup-by-name",
                   params={"name": "保温杯"})   # 库里是「不锈钢保温杯」
    assert r.status_code == 200, r.text
    assert r.json() == {"found": False}


def test_lookup_empty_name_400():
    """品名为空/纯空白 → 400 中文 detail。"""
    r = client.get("/api/v1/mappings/products/lookup-by-name", params={"name": "   "})
    assert r.status_code == 400
    assert "品名不能为空" in r.json()["detail"]


def test_lookup_ambiguous_picks_latest():
    """同品名多行：取 updated_at 最新一条，响应带 ambiguous=true。"""
    _add_mapping("玻璃烟灰缸", hs_code="7013990000", inspection=False,
                 updated_at=_BASE_TS)
    _add_mapping("玻璃烟灰缸", hs_code="7013100000", inspection=True,
                 name_en="GLASS ASHTRAY",
                 updated_at=_BASE_TS + timedelta(days=3))
    r = client.get("/api/v1/mappings/products/lookup-by-name",
                   params={"name": "玻璃烟灰缸"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["found"] is True
    assert d["ambiguous"] is True
    assert d["hs_code"] == "7013100000"          # 最新更新的一行胜出
    assert d["inspection_required"] is True
    assert d["name_en"] == "GLASS ASHTRAY"
