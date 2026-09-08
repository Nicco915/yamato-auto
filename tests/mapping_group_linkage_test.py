# -*- coding: utf-8 -*-
"""品名组 ↔ 产品映射联动回归测试（2026-09-08 设计定型）。

联动 1（待完善豁免）：is_incomplete = unit_code 空 AND 品名非品名组源品名；
联动 2（组员兜底）：品名组建/改时，组员品名缺映射行 → 自动补建品名级行；
                  组源行待完善随组身份变化重算（建组豁免/删组回算）；
联动 3（改名同步）：映射行品名改名 → product_groups.source_name_cn 与
                  product_group_members.product_name_cn 等值跟随。

覆盖：
1. 建组 → 组员甲一/甲二自动建映射行（unit_code 空、is_incomplete=True），
   响应 created_member_mappings 含两品名；
2. 预建「组源乙」映射行（unit_code 空、is_incomplete=True）→ 以其为 source
   建组 → 该行 is_incomplete 变 False（组源豁免）；
3. 删除该组 → 组源行 is_incomplete 回到 True（脱离组源身份回算）；
4. 改名同步：组源映射行改名 → source_name_cn 跟随（renamed_groups=1）；
   组员映射行改名 → members 跟随（renamed_members=1）；
5. create_product：品名是组源 → is_incomplete=False（即便 unit_code 空）；
   品名非组源且 unit_code 空 → True；
6. 改组换源：旧组源行重算回 True，新组源行豁免为 False。

隔离（血泪红线 2026-08-11，照抄 tests/sku_manual_relink_test.py 头部模板）：
EXTRACTION_MOCK/DISPATCHER_MOCK 在 import app 之前；先 import 全部 app 模块，
再调 _test_isolation.isolate_to_tmp——绝不碰 app/data/ 真实库。

用法（在仓库根目录下）：
  python3 tests/mapping_group_linkage_test.py
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
    ProductGroup,
    ProductGroupMember,
    ProductMapping,
)
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）；
# get_session/TestClient 都是按 settings 惰性建连，此刻指向临时库
TMP = isolate_to_tmp("yamato_mapping_group_linkage_test_")

client = TestClient(app)

_SEQ = itertools.count(1)

# 全部品名带独特前缀，防与并行/前序测试串扰
SRC_A = "联动组源甲"
MEM_A1 = "联动组员甲一"
MEM_A2 = "联动组员甲二"
SRC_B = "联动组源乙"
SRC_C = "联动组源丙"
SRC_C_NEW = "联动组源丙新"
MEM_C1 = "联动组员丙一"
MEM_C1_NEW = "联动组员丙一新"
SRC_D = "联动组源丁"
SRC_D_NEW = "联动组源丁新"


def _uid() -> int:
    return next(_SEQ)


def _mapping_row(name: str) -> ProductMapping | None:
    with get_session() as s:
        return (
            s.query(ProductMapping)
            .filter(ProductMapping.product_name_cn == name)
            .first()
        )


def _flag(name: str) -> bool | None:
    m = _mapping_row(name)
    return None if m is None else bool(m.is_incomplete)


def _create_group(source: str, members: list[str], name: str | None = None):
    r = client.post("/api/v1/mappings/groups", json={
        "name": name or f"联动组{_uid()}",
        "group_type": "set_split",
        "source_name_cn": source,
        "members": [
            {"product_name_cn": m, "display_order": i + 1, "split_price": 1.0}
            for i, m in enumerate(members)
        ],
    })
    assert r.status_code == 201, r.text
    return r.json()


def _create_mapping(name: str, unit_code: str | None = None) -> dict:
    r = client.post("/api/v1/mappings/products", json={
        "product_name_cn": name,
        "unit_code": unit_code,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_create_group_bootstraps_member_mappings():
    """用例 1：建组 → 缺失组员品名自动补建映射行（待完善，待补单位代码）。"""
    resp = _create_group(SRC_A, [MEM_A1, MEM_A2])
    created = resp.get("created_member_mappings") or []
    assert MEM_A1 in created and MEM_A2 in created, f"created_member_mappings={created}"
    for name in (MEM_A1, MEM_A2):
        m = _mapping_row(name)
        assert m is not None, f"组员 {name} 未建行"
        assert not (m.unit_code or "").strip(), "新行 unit_code 应为空"
        assert bool(m.is_incomplete) is True, "组员行（非组源）unit_code 空应待完善"
    # 重复建同名组员的组 → 幂等不重复建行
    resp2 = _create_group(SRC_A + "复", [MEM_A1])
    assert MEM_A1 not in (resp2.get("created_member_mappings") or [])
    print("[断言通过] 建组组员兜底：缺失补建 + 幂等")


def test_source_row_exempted_on_group_create():
    """用例 2：预建组源映射行（待完善）→ 建组后豁免为 False。"""
    _create_mapping(SRC_B)  # unit_code 空 → 此时 is_incomplete=True
    assert _flag(SRC_B) is True
    _create_group(SRC_B, ["联动组员乙一"])
    assert _flag(SRC_B) is False, "成为组源后应豁免待完善"
    print("[断言通过] 组源行建组后豁免待完善")


def test_delete_group_recomputes_source_flag():
    """用例 3：删组 → 组源行脱离身份，unit_code 空回算为待完善。"""
    resp = _create_group(SRC_B + "删", ["联动组员乙二"])
    _create_mapping(SRC_B + "删") if _mapping_row(SRC_B + "删") is None else None
    # 确保有组源行且当前豁免
    assert _mapping_row(SRC_B + "删") is not None
    assert _flag(SRC_B + "删") is False
    r = client.delete(f"/api/v1/mappings/groups/{resp['id']}")
    assert r.status_code == 200, r.text
    assert _flag(SRC_B + "删") is True, "删组后组源行应回算为待完善"
    print("[断言通过] 删组后组源行回算待完善")


def test_rename_mapping_syncs_group_tables():
    """用例 4：映射行改名 → 组源/组员字符串键跟随。"""
    resp = _create_group(SRC_C, [MEM_C1])
    gid = resp["id"]
    m_src = _create_mapping(SRC_C, unit_code="007")
    m_mem = _mapping_row(MEM_C1)  # 建组时已兜底建行
    assert m_mem is not None

    # 改组源映射行品名 → source_name_cn 跟随
    r = client.put(f"/api/v1/mappings/products/{m_src['id']}", json={
        "product_name_cn": SRC_C_NEW,
        "unit_code": "007",
    })
    assert r.status_code == 200, r.text
    assert r.json()["renamed_groups"] == 1
    with get_session() as s:
        g = s.get(ProductGroup, gid)
        assert g.source_name_cn == SRC_C_NEW, "source_name_cn 未跟随改名"

    # 改组员映射行品名 → members 跟随
    r = client.put(f"/api/v1/mappings/products/{m_mem.id}", json={
        "product_name_cn": MEM_C1_NEW,
    })
    assert r.status_code == 200, r.text
    assert r.json()["renamed_members"] == 1
    with get_session() as s:
        mem = (
            s.query(ProductGroupMember)
            .filter(ProductGroupMember.group_id == gid)
            .one()
        )
        assert mem.product_name_cn == MEM_C1_NEW, "组员品名未跟随改名"
    print("[断言通过] 映射行改名 → 品名组三表同步")


def test_create_product_incomplete_unified_rule():
    """用例 5：create_product 待完善 = unit_code 空 AND 非组源品名。"""
    src = f"联动组源戊{_uid()}"
    _create_group(src, [f"联动组员戊{_uid()}"])
    r = client.post("/api/v1/mappings/products", json={"product_name_cn": src})
    assert r.status_code == 201, r.text
    assert r.json()["is_incomplete"] is False, "组源品名即便 unit_code 空也豁免"

    plain = f"联动普通品名{_uid()}"
    r = client.post("/api/v1/mappings/products", json={"product_name_cn": plain})
    assert r.status_code == 201, r.text
    assert r.json()["is_incomplete"] is True, "非组源且 unit_code 空应待完善"
    print("[断言通过] create_product 待完善统一口径")


def test_update_group_source_switch_recalculates():
    """用例 6：改组换源 → 旧组源行回算待完善，新组源行豁免。"""
    resp = _create_group(SRC_D, [f"联动组员丁{_uid()}"])
    _create_mapping(SRC_D)      # 旧组源行（已豁免）
    _create_mapping(SRC_D_NEW)  # 新组源行（普通行，待完善）
    assert _flag(SRC_D) is False
    assert _flag(SRC_D_NEW) is True

    r = client.put(f"/api/v1/mappings/groups/{resp['id']}", json={
        "name": resp["name"],
        "group_type": "set_split",
        "source_name_cn": SRC_D_NEW,
        "members": [
            {"product_name_cn": f"联动组员丁新{_uid()}",
             "display_order": 1, "split_price": 1.0},
        ],
    })
    assert r.status_code == 200, r.text
    assert _flag(SRC_D) is True, "旧组源脱离身份后应回算待完善"
    assert _flag(SRC_D_NEW) is False, "新组源应豁免待完善"
    print("[断言通过] 改组换源：旧源回算、新源豁免")


def main():
    test_create_group_bootstraps_member_mappings()
    test_source_row_exempted_on_group_create()
    test_delete_group_recomputes_source_flag()
    test_rename_mapping_syncs_group_tables()
    test_create_product_incomplete_unified_rule()
    test_update_group_source_switch_recalculates()
    print("\nmapping_group_linkage_test: PASS")


if __name__ == "__main__":
    main()
