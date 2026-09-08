# -*- coding: utf-8 -*-
"""SKU 手动编辑品名 → 映射归属移动（relink）回归测试。

背景（2026-09-08 设计定型）：产品映射重构为「SKU → 单位代码」查找表，
SKU 侧字段变化不再反向回填映射行（sync_sku_to_mapping 已删除）；
update_sku（PUT /api/v1/mappings/skus/{id}）在 name_cn 变化时改调
relink_sku_to_name：先摘除旧归属（行保留），再按新品名挂接。
is_incomplete 新语义 = unit_code 为空。

覆盖：
1. 改品名为既有品名 Y → detached_from 含旧品名 X、action=='appended'，
   X 行摘除且保留、Y 行追加该 SKU；
2. 改品名为不存在的 Z → action=='created'，新行继承 hs/商检/英文名、
   unit_code 为空、is_incomplete 为 True；
3. 品名清空（空串）→ relink.action 为 None，旧行摘除且保留，
   SKU 不属于任何映射；
4. 只改单件重量 → relink 为 None，映射零变化；
5. create_product 不传 unit_code → is_incomplete True；
   传 unit_code（即便 hs_code 为空）→ False。

隔离（血泪红线 2026-08-11，照抄 tests/mapping_auto_link_test.py 头部模板）：
EXTRACTION_MOCK/DISPATCHER_MOCK 在 import app 之前；先 import 全部 app 模块，
再调 _test_isolation.isolate_to_tmp——绝不碰 app/data/ 真实库。

用法（在 worktree 根目录下）：
  python3 tests/sku_manual_relink_test.py
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
from app.db.models import (  # noqa: E402
    Factory,
    FactorySKU,
    ProductMapping,
    ProductMappingSku,
)
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）；
# get_session/TestClient 都是按 settings 惰性建连，此刻指向临时库
TMP = isolate_to_tmp("yamato_sku_manual_relink_test_")

client = TestClient(app)

_SKU_SEQ = itertools.count(1)
_FACTORY_SEQ = itertools.count(1)

NAME_X = "手动品名甲"
NAME_Y = "手动品名乙"
NAME_Z = "手动品名丙（不存在）"


def _new_sku() -> str:
    """每个用例唯一 sku_code：relink 按 sku_code 全表摘除，复用会被前序用例污染。"""
    return f"4909876543{next(_SKU_SEQ):03d}"


def _seed_sku_with_mapping(
    sku: str,
    *,
    name_cn=NAME_X,
    hs_code="9404909000",
    inspection=True,
    name_en="ITEM-RELINK",
    net=1.5,
):
    """造数：1 工厂 + 1 SKU + 含该 SKU 的品名 X 映射行（子表 + 旧列同步）。

    返回 (sku_id, mapping_x_id)。
    """
    with get_session() as s:
        f = Factory(factory_name=f"relink测试工厂-{next(_FACTORY_SEQ)}", short_name="テスト")
        s.add(f)
        s.flush()
        k = FactorySKU(
            factory_id=f.factory_id,
            sku_code=sku,
            name_cn=name_cn,
            hs_code=hs_code,
            inspection_required=inspection,
            name_en=name_en,
            unit_net_weight=net,
        )
        s.add(k)
        s.flush()
        m = ProductMapping(
            product_name_cn=name_cn,
            sku_code=sku,  # 旧列与列表保持一致（防启动迁移幽灵搬回约定）
            hs_code=hs_code,
            inspection_required=inspection,
            unit_code="007",
            is_incomplete=False,
        )
        s.add(m)
        s.flush()
        s.add(ProductMappingSku(mapping_id=m.id, sku_code=sku))
        s.commit()
        return k.sku_id, m.id


def _seed_name_mapping(name_cn: str, *, unit_code="007") -> int:
    """造一行不含任何 SKU 的既有品名映射行，返回 mapping_id。"""
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=name_cn,
            sku_code=None,
            hs_code="1111111111",
            inspection_required=False,
            unit_code=unit_code,
            is_incomplete=False,
        )
        s.add(m)
        s.commit()
        return m.id


def _mapping_row(mapping_id: int) -> dict:
    """读映射行快照：{product_name_cn, hs_code, inspection_required, name_en,
    unit_code, is_incomplete, sku_codes(子表+旧列兜底口径)}。"""
    with get_session() as s:
        m = s.get(ProductMapping, mapping_id)
        assert m is not None, f"映射行不存在: id={mapping_id}"
        codes = [l.sku_code for l in m.sku_links]
        if not codes and m.sku_code:
            codes = [m.sku_code]
        return {
            "product_name_cn": m.product_name_cn,
            "hs_code": m.hs_code,
            "inspection_required": bool(m.inspection_required),
            "name_en": m.name_en,
            "unit_code": m.unit_code,
            "is_incomplete": bool(m.is_incomplete),
            "sku_codes": codes,
        }


def _mappings_containing(sku_code: str) -> list[int]:
    """全表查包含该 SKU 的映射行 id（子表 + 旧列）。"""
    with get_session() as s:
        ids = {
            l.mapping_id
            for l in s.query(ProductMappingSku)
            .filter(ProductMappingSku.sku_code == sku_code)
            .all()
        }
        ids |= {
            m.id
            for m in s.query(ProductMapping)
            .filter(ProductMapping.sku_code == sku_code)
            .all()
        }
        return sorted(ids)


def _find_mapping_by_name(name_cn: str) -> dict | None:
    with get_session() as s:
        m = (
            s.query(ProductMapping)
            .filter(ProductMapping.product_name_cn == name_cn)
            .order_by(ProductMapping.id.desc())
            .first()
        )
        return _mapping_row(m.id) if m is not None else None


def _put_sku(sku_id: int, **fields):
    """全量字段提交（SkuUpsert 语义）；缺省沿用「原值不变」的常见组合。"""
    body = {
        "name_cn": fields.get("name_cn", NAME_X),
        "name_en": fields.get("name_en", "ITEM-RELINK"),
        "hs_code": fields.get("hs_code", "9404909000"),
        "inspection_required": fields.get("inspection_required", True),
        "unit_net_weight": fields.get("unit_net_weight", 1.5),
        "unit_gross_weight": fields.get("unit_gross_weight"),
    }
    return client.put(f"/api/v1/mappings/skus/{sku_id}", json=body)


# ---------------------------------------------------------------------------
# 用例 1：改品名为既有品名 → 摘除旧行 + 追加进既有行
# ---------------------------------------------------------------------------

def test_relink_append_to_existing_name():
    sku = _new_sku()
    sku_id, x_id = _seed_sku_with_mapping(sku)
    y_id = _seed_name_mapping(NAME_Y)

    r = _put_sku(sku_id, name_cn=NAME_Y)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "name_cn" in data["audited_fields"]
    relink = data["relink"]
    assert relink is not None, f"品名变更必须有 relink: {data}"
    assert relink["action"] == "appended", f"应挂入既有行: {relink}"
    assert [x_id, NAME_X] in relink["detached_from"], \
        f"detached_from 应含 X 行: {relink}"

    x = _mapping_row(x_id)
    assert sku not in x["sku_codes"], f"X 行应已摘除该 SKU: {x}"
    assert x["product_name_cn"] == NAME_X, "X 行本身应保留（摘空变兜底行，不删行）"
    y = _mapping_row(y_id)
    assert sku in y["sku_codes"], f"Y 行应追加该 SKU: {y}"
    assert y["unit_code"] == "007", f"Y 行既有 unit_code 不得被改动: {y}"
    assert y["hs_code"] == "1111111111", f"Y 行既有税号不得被回填: {y}"
    print("[断言通过] 改品名为既有品名：摘除 X（行保留）+ 追加进 Y，Y 行字段不动")


# ---------------------------------------------------------------------------
# 用例 2：改品名为不存在的 Z → 新建品名级行，一次性继承 + 待完善
# ---------------------------------------------------------------------------

def test_relink_create_new_name_row():
    sku = _new_sku()
    sku_id, x_id = _seed_sku_with_mapping(sku)

    r = _put_sku(sku_id, name_cn=NAME_Z)
    assert r.status_code == 200, r.text
    relink = r.json()["relink"]
    assert relink is not None
    assert relink["action"] == "created", f"应新建映射行: {relink}"
    assert [x_id, NAME_X] in relink["detached_from"]

    z = _find_mapping_by_name(NAME_Z)
    assert z is not None, "应新建品名 Z 映射行"
    assert sku in z["sku_codes"], f"新行应含该 SKU: {z}"
    # 一次性继承触发 SKU 的税号/商检/英文名
    assert z["hs_code"] == "9404909000", f"新行应继承税号: {z}"
    assert z["inspection_required"] is True, f"新行应继承商检: {z}"
    assert z["name_en"] == "ITEM-RELINK", f"新行应继承英文名: {z}"
    # unit_code 无源可继承 → 留空；is_incomplete 新语义 = unit_code 空
    assert z["unit_code"] is None, f"新行 unit_code 应留空待补: {z}"
    assert z["is_incomplete"] is True, f"新行应标待完善: {z}"
    print("[断言通过] 改品名为不存在品名：新建行继承 hs/商检/英文名，unit_code 空 + 待完善")


# ---------------------------------------------------------------------------
# 用例 3：品名清空 → 只摘除不挂接
# ---------------------------------------------------------------------------

def test_relink_clear_name_detach_only():
    sku = _new_sku()
    sku_id, x_id = _seed_sku_with_mapping(sku)

    r = _put_sku(sku_id, name_cn="")  # 空串 → None → 只摘除
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name_cn"] is None, f"SKU 品名应被清空: {data}"
    relink = data["relink"]
    assert relink is not None, f"有摘除动作时 relink 不应为 None: {data}"
    assert relink["action"] is None, f"品名清空不得挂接: {relink}"
    assert [x_id, NAME_X] in relink["detached_from"]

    x = _mapping_row(x_id)
    assert sku not in x["sku_codes"], f"X 行应摘除该 SKU: {x}"
    assert x["product_name_cn"] == NAME_X, "X 行应保留为品名级兜底行"
    assert _mappings_containing(sku) == [], "SKU 不应属于任何映射行"
    print("[断言通过] 品名清空：只摘除不挂接，旧行保留，SKU 无归属")


# ---------------------------------------------------------------------------
# 用例 4：只改单件重量 → relink 为 None，映射零变化
# ---------------------------------------------------------------------------

def test_weight_only_change_no_relink():
    sku = _new_sku()
    sku_id, x_id = _seed_sku_with_mapping(sku)
    before = _mapping_row(x_id)

    r = _put_sku(sku_id, unit_net_weight=2.5)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["audited_fields"] == ["unit_net_weight"], data["audited_fields"]
    assert data["relink"] is None, f"未改品名不得有 relink: {data}"

    after = _mapping_row(x_id)
    assert after == before, f"映射行零变化: {before} → {after}"
    assert _mappings_containing(sku) == [x_id]
    print("[断言通过] 只改重量：relink=None，映射行与子表零变化")


# ---------------------------------------------------------------------------
# 用例 5：create_product 的 is_incomplete 按 unit_code 判定
# ---------------------------------------------------------------------------

def test_create_product_incomplete_by_unit_code():
    r1 = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "单位代码缺失品名",
        "hs_code": "9404909000",  # 税号齐也不影响：判定看 unit_code
    })
    assert r1.status_code == 201, r1.text
    assert r1.json()["is_incomplete"] is True, \
        f"unit_code 缺失应标待完善: {r1.json()}"

    r2 = client.post("/api/v1/mappings/products", json={
        "product_name_cn": "税号缺失但单位代码齐",
        "unit_code": "006",
        # hs_code 故意不传：即便税号为空也不应标待完善
    })
    assert r2.status_code == 201, r2.text
    d2 = r2.json()
    assert d2["hs_code"] is None
    assert d2["is_incomplete"] is False, \
        f"unit_code 齐则不应标待完善（即便税号空）: {d2}"
    print("[断言通过] create_product：is_incomplete 按 unit_code 判定（税号空不影响）")


def main():
    test_relink_append_to_existing_name()
    test_relink_create_new_name_row()
    test_relink_clear_name_detach_only()
    test_weight_only_change_no_relink()
    test_create_product_incomplete_by_unit_code()
    print("\nsku_manual_relink_test: PASS")


if __name__ == "__main__":
    main()
