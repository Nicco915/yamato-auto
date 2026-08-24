# -*- coding: utf-8 -*-
"""SKU 主数据 → 品名映射 反向同步测试（2026-08-12 双向打通）。

覆盖 update_sku（PUT /api/v1/mappings/skus/{id}）的反向回填行为：
- name_cn / hs_code / inspection_required 变更 → 同 sku_code 的 SKU 级映射行同步
- 品名级映射行（sku_code 为空）绝不被触碰（可能被多 SKU 共享兜底）
- 清空 name_cn 不回写映射键（保护报关匹配主键不被写空）
- 清空 hs_code 联动 is_incomplete=True
- 仅改重量不同步；无映射行时 synced_mappings=0
- 正向（映射 → SKU）回归：update_product 仍正常回填

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/sku_mapping_sync_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.db.models import Factory, FactorySKU, ProductMapping  # noqa: E402
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_sku_sync_test_")

client = TestClient(app)

_SKU_SEQ = itertools.count(1)
_FACTORY_SEQ = itertools.count(1)


def _new_sku() -> str:
    """每个测试用唯一 sku_code：sync 按 sku_code 全表匹配，复用会被前序用例污染。"""
    return f"4901234567{next(_SKU_SEQ):03d}"


def _seed(
    *,
    sku: str,
    sku_name_cn="旧品名",
    sku_hs="9404909000",
    sku_inspection=False,
    with_sku_mapping=True,
    with_name_mapping=True,
    extra_sku_mapping_rows=0,
):
    """造数：1 工厂 + 1 SKU + 映射行。返回 (sku_id, [mapping_ids])。"""
    with get_session() as s:
        f = Factory(factory_name=f"测试工厂-{next(_FACTORY_SEQ)}", short_name="テスト")
        s.add(f)
        s.flush()
        k = FactorySKU(
            factory_id=f.factory_id,
            sku_code=sku,
            name_cn=sku_name_cn,
            hs_code=sku_hs,
            inspection_required=sku_inspection,
        )
        s.add(k)
        s.flush()
        mapping_ids = []
        if with_sku_mapping:
            m = ProductMapping(
                product_name_cn=sku_name_cn,
                sku_code=sku,
                hs_code=sku_hs,
                inspection_required=sku_inspection,
            )
            s.add(m)
            s.flush()
            mapping_ids.append(m.id)
        for i in range(extra_sku_mapping_rows):
            m = ProductMapping(
                product_name_cn=sku_name_cn,
                sku_code=sku,
                hs_code=sku_hs,
                inspection_required=sku_inspection,
            )
            s.add(m)
            s.flush()
            mapping_ids.append(m.id)
        if with_name_mapping:
            # 品名级兜底行：与 SKU 同名但 sku_code 为空，绝不许被反向同步碰到
            nm = ProductMapping(
                product_name_cn=sku_name_cn,
                sku_code=None,
                hs_code="1111111111",
                inspection_required=True,
            )
            s.add(nm)
            s.flush()
            mapping_ids.append(nm.id)
        s.commit()
        return k.sku_id, mapping_ids


def _get_mapping(mapping_id: int) -> ProductMapping:
    with get_session() as s:
        m = s.get(ProductMapping, mapping_id)
        s.expunge(m)
        return m


def _put_sku(sku_id: int, **fields):
    body = {
        "name_cn": fields.get("name_cn"),
        "name_en": fields.get("name_en"),
        "hs_code": fields.get("hs_code"),
        "inspection_required": fields.get("inspection_required", False),
        "unit_net_weight": fields.get("unit_net_weight"),
        "unit_gross_weight": fields.get("unit_gross_weight"),
    }
    return client.put(f"/api/v1/mappings/skus/{sku_id}", json=body)


# ---------------------------------------------------------------------------
# 反向同步核心行为
# ---------------------------------------------------------------------------


def test_rename_syncs_sku_level_mapping():
    """改 name_cn → 同 sku_code 的映射行 product_name_cn 同步更新。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn="新品名", hs_code="9404909000")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "name_cn" in data["audited_fields"]
    assert data["synced_mappings"] == 1
    assert _get_mapping(ids[0]).product_name_cn == "新品名"


def test_name_level_mapping_untouched():
    """品名级映射行（sku_code 为空）不被反向同步——即使它与 SKU 同名。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn="新品名", hs_code="9404909000")
    assert r.status_code == 200, r.text
    # ids[1] 是品名级行：品名/税号/商检全部保持原样
    nm = _get_mapping(ids[1])
    assert nm.product_name_cn == "旧品名"
    assert nm.hs_code == "1111111111"
    assert nm.inspection_required is True


def test_clear_name_does_not_blank_mapping_key():
    """清空 name_cn → 映射行的 product_name_cn 保持旧值（不写空匹配键）。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn=None, hs_code="9404909000")
    assert r.status_code == 200, r.text
    assert "name_cn" in r.json()["audited_fields"]
    # SKU 本身被清空，但映射键不动
    assert _get_mapping(ids[0]).product_name_cn == "旧品名"


def test_hs_change_syncs_and_marks_incomplete():
    """清空 hs_code → 映射行 hs_code=None 且 is_incomplete=True。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn="旧品名", hs_code=None)
    assert r.status_code == 200, r.text
    assert "hs_code" in r.json()["audited_fields"]
    m = _get_mapping(ids[0])
    assert m.hs_code is None
    assert m.is_incomplete is True


def test_inspection_change_syncs():
    """inspection_required 变更 → 映射行同步。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn="旧品名", hs_code="9404909000", inspection_required=True)
    assert r.status_code == 200, r.text
    assert "inspection_required" in r.json()["audited_fields"]
    assert _get_mapping(ids[0]).inspection_required is True


def test_weight_only_change_no_sync():
    """仅改重量 → 不回填映射（synced_mappings=0）。"""
    sku_id, ids = _seed(sku=_new_sku())
    r = _put_sku(sku_id, name_cn="旧品名", hs_code="9404909000", unit_net_weight=1.234)
    assert r.status_code == 200, r.text
    assert r.json()["synced_mappings"] == 0
    assert _get_mapping(ids[0]).product_name_cn == "旧品名"


def test_multiple_sku_level_rows_all_synced():
    """同 sku_code 有多条映射行 → 全部更新（与正向 sync 同 scope）。"""
    sku_id, ids = _seed(sku=_new_sku(), extra_sku_mapping_rows=1)
    r = _put_sku(sku_id, name_cn="新品名", hs_code="9404909000")
    assert r.status_code == 200, r.text
    assert r.json()["synced_mappings"] == 2
    assert _get_mapping(ids[0]).product_name_cn == "新品名"
    assert _get_mapping(ids[1]).product_name_cn == "新品名"


def test_no_mapping_row_is_noop():
    """没有映射行 → synced_mappings=0，不报错。"""
    sku_id, _ = _seed(sku=_new_sku(), with_sku_mapping=False, with_name_mapping=False)
    r = _put_sku(sku_id, name_cn="新品名", hs_code="9404909000")
    assert r.status_code == 200, r.text
    assert r.json()["synced_mappings"] == 0


# ---------------------------------------------------------------------------
# 正向回归：映射 → SKU 仍正常（双向共存不打架）
# ---------------------------------------------------------------------------


def test_forward_sync_still_works():
    """编辑映射（update_product）仍回填 factory_skus——正向不被反向破坏。"""
    sku = _new_sku()
    sku_id, ids = _seed(sku=sku)
    r = client.put(
        f"/api/v1/mappings/products/{ids[0]}",
        json={
            "product_name_cn": "映射侧改名",
            "hs_code": "2222222222",
            "inspection_required": True,
            "sku_code": sku,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["synced_skus"] == 1
    with get_session() as s:
        k = s.get(FactorySKU, sku_id)
        assert k.name_cn == "映射侧改名"
        assert k.hs_code == "2222222222"
        assert k.inspection_required is True
