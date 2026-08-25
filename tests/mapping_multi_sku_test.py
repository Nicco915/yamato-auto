# -*- coding: utf-8 -*-
"""产品映射多 SKU 化测试（S4）。

覆盖：
1. 多 SKU 建行/读取回环：POST sku_codes → GET 返回列表，sku_code 兼容字段=第一个；
2. 编辑增删 SKU：PUT 整体替换子表；
3. 冲突 409：SKU 已被其他映射占用 → 中文 detail（带 SKU/映射 id/品名），整体不落库；
4. 正向联动：sync_mapping_to_sku 回填列表每个 SKU；
5. 反向联动：sync_sku_to_mapping 命中所有含该 SKU 的映射行，品名级行不碰；
6. build_mapping_index：by_sku 多 SKU 命中（ORM/dict 两条路径）+ lookup 优先级回归；
7. 旧 sku_code 单值入参兼容：API 与 dispatcher 工具两条路径都落到子表。

隔离（血泪红线 2026-08-11，与 tests/mapping_skus_migration_test.py 同模板）：
先 import 全部 app 模块，再调 validation/_test_isolation.isolate_to_tmp。
绝不触碰 app/data/ 真实库。

用法（在 app/ 目录下）：
  PYTHONPATH=. python3 -m pytest tests/mapping_multi_sku_test.py -q
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"                      # 提取走 mock，不调 LLM
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.declare.mapping import build_mapping_index, lookup  # noqa: E402
from app.db.models import Factory, FactorySKU, ProductMapping, ProductMappingSku  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.db.sync import sync_mapping_to_sku, sync_sku_to_mapping  # noqa: E402
from app.dispatcher.tools import _exec_upsert_product_mapping  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_mapping_multi_sku_test_")

client = TestClient(app)

_SEQ = itertools.count(1)


def _sku() -> str:
    """每个用例唯一 SKU 段，避免 sync 按 sku_code 全表匹配时被前序用例污染。"""
    return f"4909999{next(_SEQ):06d}"


def _links_of(mapping_id: int) -> list[str]:
    with get_session() as s:
        rows = (
            s.query(ProductMappingSku)
            .filter(ProductMappingSku.mapping_id == mapping_id)
            .order_by(ProductMappingSku.id)
            .all()
        )
        return [r.sku_code for r in rows]


def _mapping_count() -> int:
    with get_session() as s:
        return s.query(ProductMapping).count()


# ---------------------------------------------------------------------------
# 1. 多 SKU 建行 / 读取回环
# ---------------------------------------------------------------------------

def test_create_with_multiple_skus_roundtrip():
    a, b = _sku(), _sku()
    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "多SKU品名甲",
        "hs_code": "9404909000",
        "sku_codes": [a, b, a, "  ", f" {b} "],  # 故意带重复/空白：应去重 strip
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["sku_codes"] == [a, b]
    assert data["sku_code"] == a  # 兼容字段=列表第一个
    assert _links_of(data["id"]) == [a, b]

    # 列表读取回环
    rows = client.get("/api/v1/mappings/products?q=多SKU品名甲").json()
    hit = [m for m in rows if m["id"] == data["id"]][0]
    assert hit["sku_codes"] == [a, b]
    assert hit["sku_code"] == a


def test_create_legacy_single_sku_param():
    """旧单值 sku_code 入参（dispatcher 老调用方）落到子表，兼容字段仍返回。"""
    a = _sku()
    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "旧入参品名",
        "sku_code": a,
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["sku_codes"] == [a]
    assert data["sku_code"] == a
    assert _links_of(data["id"]) == [a]


# ---------------------------------------------------------------------------
# 2. 编辑增删 SKU（整体替换）
# ---------------------------------------------------------------------------

def test_update_replaces_sku_list():
    a, b, c = _sku(), _sku(), _sku()
    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "编辑SKU品名",
        "sku_codes": [a, b],
    })
    mid = r.json()["id"]

    # 增 c 删 a → [b, c]
    r = client.put(f"/api/v1/mappings/products/{mid}", json={
        "product_name_cn": "编辑SKU品名",
        "sku_codes": [b, c],
    })
    assert r.status_code == 200, r.text
    assert r.json()["sku_codes"] == [b, c]
    assert r.json()["sku_code"] == b
    assert _links_of(mid) == [b, c]

    # 清空列表 → 品名级行
    r = client.put(f"/api/v1/mappings/products/{mid}", json={
        "product_name_cn": "编辑SKU品名",
        "sku_codes": [],
    })
    assert r.status_code == 200, r.text
    assert r.json()["sku_codes"] == []
    assert r.json()["sku_code"] is None
    assert _links_of(mid) == []


# ---------------------------------------------------------------------------
# 3. 冲突拦截 409：中文详情 + 整体不落库
# ---------------------------------------------------------------------------

def test_conflict_409_on_create_no_partial_write():
    a, b = _sku(), _sku()
    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "占用方品名", "sku_codes": [a],
    })
    owner_id = r.json()["id"]
    before = _mapping_count()

    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "抢注品名", "sku_codes": [b, a],  # a 被占用，b 是干净的
    })
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert a in detail and "占用方品名" in detail and f"id={owner_id}" in detail
    # 整体不落库：新映射行与干净的 b 都没写入
    assert _mapping_count() == before
    assert client.get("/api/v1/mappings/products?q=抢注品名").json() == []
    with get_session() as s:
        assert s.query(ProductMappingSku).filter(
            ProductMappingSku.sku_code == b).count() == 0


def test_conflict_409_on_update_and_self_excluded():
    a, b = _sku(), _sku()
    m1 = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "冲突品名一", "sku_codes": [a]}).json()["id"]
    m2 = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "冲突品名二", "sku_codes": [b]}).json()["id"]

    # m2 想挂上 m1 的 a → 409，m2 原有 [b] 不动
    r = client.put(f"/api/v1/mappings/products/{m2}", json={
        "product_name_cn": "冲突品名二", "sku_codes": [b, a]})
    assert r.status_code == 409, r.text
    assert a in r.json()["detail"] and "冲突品名一" in r.json()["detail"]
    assert _links_of(m2) == [b]

    # 自身 SKU 不算冲突：m1 原样保存成功
    r = client.put(f"/api/v1/mappings/products/{m1}", json={
        "product_name_cn": "冲突品名一", "sku_codes": [a], "hs_code": "9404909000"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 4. 正向联动：回填列表每个 SKU
# ---------------------------------------------------------------------------

def test_forward_sync_fills_every_sku_in_list():
    a, b = _sku(), _sku()
    with get_session() as s:
        f = Factory(factory_name=f"多SKU工厂-{a}", short_name="テ")
        s.add(f)
        s.flush()
        for code in (a, b):
            s.add(FactorySKU(factory_id=f.factory_id, sku_code=code,
                             name_cn="旧名", hs_code="0000000000"))
        s.commit()

    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "正向联动品名",
        "hs_code": "9404909000",
        "inspection_required": True,
        "name_en": "FORWARD SYNC ITEM",
        "sku_codes": [a, b],
    })
    assert r.status_code == 201, r.text

    # POST 不做回填（与旧行为一致，回填挂在 PUT）；PUT 触发逐 SKU 回填
    mid = r.json()["id"]
    r = client.put(f"/api/v1/mappings/products/{mid}", json={
        "product_name_cn": "正向联动品名",
        "hs_code": "9404909000",
        "inspection_required": True,
        "name_en": "FORWARD SYNC ITEM",
        "sku_codes": [a, b],
    })
    assert r.status_code == 200, r.text
    assert r.json()["synced_skus"] == 2
    with get_session() as s:
        for code in (a, b):
            k = s.query(FactorySKU).filter(FactorySKU.sku_code == code).one()
            assert k.name_cn == "正向联动品名"
            assert k.hs_code == "9404909000"
            assert k.inspection_required is True
            assert k.name_en == "FORWARD SYNC ITEM"


# ---------------------------------------------------------------------------
# 5. 反向联动：命中所有含该 SKU 的行；品名级行不碰
# ---------------------------------------------------------------------------

def test_reverse_sync_hits_all_rows_containing_sku():
    shared = _sku()
    other = _sku()
    with get_session() as s:
        f = Factory(factory_name=f"反向工厂-{shared}", short_name="テ")
        s.add(f)
        s.flush()
        k = FactorySKU(factory_id=f.factory_id, sku_code=shared,
                       name_cn="旧品名", hs_code="9404909000")
        s.add(k)
        s.flush()
        sku_id = k.sku_id
        # 两条映射的子表都含 shared（其中一条还多挂 other）；一条品名级行
        m1 = ProductMapping(product_name_cn="反向品名一", hs_code="1111111111")
        m2 = ProductMapping(product_name_cn="反向品名二", hs_code="2222222222")
        m3 = ProductMapping(product_name_cn="品名级兜底", hs_code="3333333333")
        s.add_all([m1, m2, m3])
        s.flush()
        s.add(ProductMappingSku(mapping_id=m1.id, sku_code=shared))
        s.add(ProductMappingSku(mapping_id=m2.id, sku_code=shared))
        s.add(ProductMappingSku(mapping_id=m2.id, sku_code=other))
        s.commit()
        ids = (m1.id, m2.id, m3.id)

    r = client.put(f"/api/v1/mappings/skus/{sku_id}", json={
        "name_cn": "反向新品名", "hs_code": "9999999999",
    })
    assert r.status_code == 200, r.text
    assert r.json()["synced_mappings"] == 2  # m1 + m2，品名级行不算

    with get_session() as s:
        r1, r2, r3 = (s.get(ProductMapping, i) for i in ids)
        assert r1.product_name_cn == "反向新品名" and r1.hs_code == "9999999999"
        assert r2.product_name_cn == "反向新品名" and r2.hs_code == "9999999999"
        # 品名级行（SKU 列表为空）绝不被触碰
        assert r3.product_name_cn == "品名级兜底" and r3.hs_code == "3333333333"


# ---------------------------------------------------------------------------
# 6. build_mapping_index：多 SKU 命中 + lookup 优先级回归
# ---------------------------------------------------------------------------

def test_build_index_multi_sku_dict_and_lookup_priority():
    rec_multi = {
        "product_name_cn": "索引品名", "factory_id": 1,
        "sku_codes": ["S001", "S002"], "unit_code": "007",
    }
    rec_legacy = {"product_name_cn": "旧索引品名", "factory_id": None,
                  "sku_code": "S009"}
    rec_name_only = {"product_name_cn": "纯品名", "factory_id": None}
    idx = build_mapping_index([rec_multi, rec_legacy, rec_name_only])

    # 多 SKU：列表每个 SKU 都进 by_sku
    assert idx["by_sku"]["S001"] is rec_multi
    assert idx["by_sku"]["S002"] is rec_multi
    # 旧单值 sku_code 兼容
    assert idx["by_sku"]["S009"] is rec_legacy
    assert "by_name" in idx and idx["by_name"]["纯品名"] is rec_name_only

    # lookup 优先级：SKU 精确 > 品名+工厂 > 品名
    idx2 = build_mapping_index([
        {"product_name_cn": "优先级品名", "factory_id": 7, "sku_codes": ["S100"],
         "mark": "sku"},
        {"product_name_cn": "优先级品名", "factory_id": 7, "mark": "name_factory"},
        {"product_name_cn": "优先级品名", "factory_id": None, "mark": "name"},
    ])
    assert lookup(idx2, sku="S100", name_cn="优先级品名", factory_id=7)["mark"] == "sku"
    assert lookup(idx2, sku="S999", name_cn="优先级品名", factory_id=7)["mark"] == "name_factory"
    assert lookup(idx2, sku="S999", name_cn="优先级品名")["mark"] == "name"
    assert lookup(idx2, sku="S999", name_cn="不存在") is None


def test_build_index_from_orm_with_sku_links():
    """ORM 路径：by_sku 从 sku_links 关系构建（session 内预取，模拟 service.py）。"""
    from sqlalchemy.orm import selectinload

    a, b = _sku(), _sku()
    with get_session() as s:
        m = ProductMapping(product_name_cn="ORM索引品名", hs_code="9404909000")
        s.add(m)
        s.flush()
        s.add(ProductMappingSku(mapping_id=m.id, sku_code=a))
        s.add(ProductMappingSku(mapping_id=m.id, sku_code=b))
        s.commit()
        rows = (
            s.query(ProductMapping)
            .options(selectinload(ProductMapping.sku_links))
            .filter(ProductMapping.id == m.id)
            .all()
        )
        idx = build_mapping_index(rows)  # session 内构建，sku_links 可用
        assert a in idx["by_sku"] and b in idx["by_sku"]
        assert lookup(idx, sku=b, name_cn="别的品名") is rows[0]


# ---------------------------------------------------------------------------
# 7. dispatcher 工具：旧 sku_code 单值兼容 + 冲突中文报错
# ---------------------------------------------------------------------------

def test_dispatcher_tool_legacy_sku_code_writes_subtable():
    a = _sku()
    r = _exec_upsert_product_mapping({
        "product_name_cn": "调度品名甲",
        "hs_code": "9404909000",
        "sku_code": a,
    })
    assert r.get("status") == "ok", r
    with get_session() as s:
        m = s.query(ProductMapping).filter(
            ProductMapping.product_name_cn == "调度品名甲").one()
        assert [l.sku_code for l in m.sku_links] == [a]

    # 数组入参整体替换
    b, c = _sku(), _sku()
    r = _exec_upsert_product_mapping({
        "product_name_cn": "调度品名甲",
        "sku_codes": [b, c],
    })
    assert r.get("status") == "ok", r
    assert _links_of(m.id) == [b, c]


def test_dispatcher_tool_conflict_returns_chinese_error():
    a = _sku()
    r = _exec_upsert_product_mapping({
        "product_name_cn": "调度占用方", "sku_code": a,
    })
    assert r.get("status") == "ok", r
    before = _mapping_count()

    r = _exec_upsert_product_mapping({
        "product_name_cn": "调度抢注方", "sku_codes": [a],
    })
    assert "error" in r, r
    assert a in r["error"] and "调度占用方" in r["error"]
    assert _mapping_count() == before  # 冲突不落库
