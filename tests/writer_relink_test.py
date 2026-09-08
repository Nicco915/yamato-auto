# -*- coding: utf-8 -*-
"""老 SKU 人工改品名重挂映射回归测试（writer._upsert_db → sync.relink_sku_to_name）。

产品映射重构为「SKU → 单位代码」查找表（2026-09-08）：一个 SKU 最多归属一个
品名行。Node6 UPDATE 分支检测到 name_cn 变化后，主 commit 之后由
_relink_mappings 调 relink_sku_to_name（先摘除旧归属、行保留，再按新品名挂接）。

覆盖（单元级直调 writer._upsert_db，不跑整图）：
1. 老 SKU（品名 X，X 行子表含该 SKU）人工改品名为 Y（Y 已有映射行）
   → X 行子表摘除该 SKU（行保留）、Y 行追加该 SKU；
2. 同上前置但 Y 无映射行 → 新建品名级行（继承落库后 record 的
   hs_code/商检/英文名，unit_code None，is_incomplete True，旧列同步）；
3. 人工编辑只改重量（name_cn 不变）→ 映射零变化；
4. 老 SKU 未人工编辑（无 is_human_edited/update_history_weight）
   → 不触发 relink，映射零变化；
5. X 行摘空后行保留（ProductMapping 仍在，sku_links 为空）——随场景 1 验证。

隔离（血泪红线 2026-08-11，照抄 tests/mapping_auto_link_test.py 头部模板）：
先 import 全部 app 模块，再调 _test_isolation.isolate_to_tmp——
master db 全部指向临时目录，绝不碰 app/data/ 真实库。

用法（在 worktree 根目录下）：
  python3 tests/writer_relink_test.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"                      # 提取走 mock，不调 LLM
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from sqlalchemy import select  # noqa: E402

from app.db.models import (  # noqa: E402
    Factory,
    FactorySKU,
    ProductMapping,
    ProductMappingSku,
)
from app.db.session import get_session  # noqa: E402
from app.nodes import writer  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）；
# session 引擎为惰性单例，首次 get_session 才按隔离后的 settings 建临时库
TMP = isolate_to_tmp("yamato_writer_relink_test_")

# ---- 测试夹具常量（各场景独立工厂/SKU/品名，避免共享临时库相互串扰）----
F_A, SKU_A = "重挂厂甲", "4900000009001"   # 场景 1+5：改品名到已有映射行
F_B, SKU_B = "重挂厂乙", "4900000009002"   # 场景 2：改品名到无映射行（新建）
F_C, SKU_C = "重挂厂丙", "4900000009003"   # 场景 3：只改重量
F_D, SKU_D = "重挂厂丁", "4900000009004"   # 场景 4：未人工编辑
NAME_X_A, NAME_Y_A = "品名旧甲", "品名新甲"
NAME_X_B, NAME_Y_B = "品名旧乙", "品名新乙"
NAME_X_C = "品名丙"
NAME_X_D = "品名丁"


# ---------------------------------------------------------------------------
# 夹具与查询助手
# ---------------------------------------------------------------------------

def _seed_factory_sku(factory_name: str, sku: str, **fields) -> None:
    """造一条老 SKU 主数据记录（工厂不存在则建）。"""
    with get_session() as s:
        factory = s.scalar(
            select(Factory).where(Factory.factory_name == factory_name))
        if factory is None:
            factory = Factory(factory_name=factory_name)
            s.add(factory)
            s.flush()
        s.add(FactorySKU(factory_id=factory.factory_id, sku_code=sku, **fields))
        s.commit()


def _seed_mapping(name: str, skus: list[str], **fields) -> int:
    """预建映射行并挂接 SKU 列表，返回 mapping id。"""
    with get_session() as s:
        m = ProductMapping(product_name_cn=name, **fields)
        m.sku_links = [ProductMappingSku(sku_code=c) for c in skus]
        if skus:
            m.sku_code = skus[0]  # 旧列与列表首个保持一致
        s.add(m)
        s.commit()
        s.refresh(m)
        return m.id


def _mapping_rows(name: str) -> list[ProductMapping]:
    with get_session() as s:
        return list(s.scalars(
            select(ProductMapping)
            .where(ProductMapping.product_name_cn == name)
            .order_by(ProductMapping.id)
        ).all())


def _link_rows(mapping_id: int) -> list[ProductMappingSku]:
    with get_session() as s:
        return list(s.scalars(
            select(ProductMappingSku)
            .where(ProductMappingSku.mapping_id == mapping_id)
            .order_by(ProductMappingSku.id)
        ).all())


def _mapping_snapshot() -> tuple[list[tuple], list[tuple]]:
    """全表快照（映射行关键字段 + 子表关联），用于「零变化」断言。"""
    with get_session() as s:
        ms = [(m.id, m.product_name_cn, m.hs_code, m.unit_code, m.sku_code,
               m.inspection_required, m.name_en, m.is_incomplete)
              for m in s.scalars(select(ProductMapping).order_by(ProductMapping.id))]
        ls = [(l.mapping_id, l.sku_code)
              for l in s.scalars(select(ProductMappingSku).order_by(ProductMappingSku.id))]
    return ms, ls


def _run_upsert(factory_name: str, items: list[dict]) -> tuple[int, int]:
    """构造最小 state 直调 writer._upsert_db。"""
    state = {"current_factory_data": {"factory_name": factory_name,
                                      "calculated_items": items}}
    return writer._upsert_db(state)


# ---------------------------------------------------------------------------
# 场景 1 + 5：改品名到已有映射行 → 旧行摘除（行保留）、新行追加
# ---------------------------------------------------------------------------

def test_relink_to_existing_mapping():
    _seed_factory_sku(F_A, SKU_A, name_cn=NAME_X_A, hs_code="1111.11",
                      name_en="OLD A", inspection_required=False,
                      unit_net_weight=1.0, unit_gross_weight=2.0)
    x_id = _seed_mapping(NAME_X_A, [SKU_A], hs_code="1111.11",
                         unit_code="001", is_incomplete=False)
    y_id = _seed_mapping(NAME_Y_A, ["4900000009101"], hs_code="2222.22",
                         unit_code="009", is_incomplete=False)

    inserted, updated = _run_upsert(F_A, [{
        "sku": SKU_A,
        "is_human_edited": True,
        "name_cn": NAME_Y_A,
        "hs_code": "2222.22",
        "inspection_required": True,
        "calculation": {"calculated_unit_net": 1.5,
                        "calculated_unit_gross": 2.5},
    }])
    assert (inserted, updated) == (0, 1), (inserted, updated)

    # 主数据品名已刷新
    with get_session() as s:
        rec = s.scalar(select(FactorySKU).where(FactorySKU.sku_code == SKU_A))
        assert rec.name_cn == NAME_Y_A

    # X 行：SKU 被摘除，行保留（场景 5：摘空后 ProductMapping 仍在、sku_links 为空）
    rows_x = _mapping_rows(NAME_X_A)
    assert len(rows_x) == 1 and rows_x[0].id == x_id
    assert _link_rows(x_id) == [], f"X 行子表应摘空: {_link_rows(x_id)}"
    assert rows_x[0].unit_code == "001"  # 行字段不被摘除动作改动

    # Y 行：追加了该 SKU；既有字段（unit_code 等）不被改动
    rows_y = _mapping_rows(NAME_Y_A)
    assert len(rows_y) == 1 and rows_y[0].id == y_id
    links_y = [l.sku_code for l in _link_rows(y_id)]
    assert sorted(links_y) == sorted(["4900000009101", SKU_A]), links_y
    assert rows_y[0].unit_code == "009", "既有映射行 unit_code 不得被改动"
    assert rows_y[0].hs_code == "2222.22"
    print("[断言通过] 场景1+5：改品名到已有映射行——X 行摘除且行保留、"
          "Y 行追加、既有字段不动")


# ---------------------------------------------------------------------------
# 场景 2：改品名到无映射行 → 新建品名级行（继承落库后 record 现值）
# ---------------------------------------------------------------------------

def test_relink_to_new_name_creates_row():
    _seed_factory_sku(F_B, SKU_B, name_cn=NAME_X_B, hs_code="3333.33",
                      name_en="OLD B", inspection_required=False,
                      unit_net_weight=1.0, unit_gross_weight=2.0)
    x_id = _seed_mapping(NAME_X_B, [SKU_B], hs_code="3333.33")

    inserted, updated = _run_upsert(F_B, [{
        "sku": SKU_B,
        "is_human_edited": True,
        "name_cn": NAME_Y_B,
        "hs_code": "6402.20",          # 人工连同税号一起改，relink 以落库后现值为准
        "name_en": "NEW B",
        "inspection_required": True,
        "calculation": {"calculated_unit_net": 1.0,
                        "calculated_unit_gross": 2.0},
    }])
    assert (inserted, updated) == (0, 1), (inserted, updated)

    # 旧行摘除、行保留
    rows_x = _mapping_rows(NAME_X_B)
    assert len(rows_x) == 1 and rows_x[0].id == x_id
    assert _link_rows(x_id) == []

    # 新建品名级行：继承落库后 record 的 hs/商检/英文名
    rows_y = _mapping_rows(NAME_Y_B)
    assert len(rows_y) == 1, f"「{NAME_Y_B}」应新建恰 1 行: {len(rows_y)}"
    m = rows_y[0]
    assert m.hs_code == "6402.20", f"hs_code 应继承落库后现值: {m.hs_code}"
    assert m.inspection_required is True
    assert m.name_en == "NEW B"
    assert m.unit_code is None                 # 无源可继承，留空待人工补
    assert m.is_incomplete is True             # 新语义 = unit_code 为空
    assert m.sku_code == SKU_B                 # 旧列与列表首个同步
    assert [l.sku_code for l in _link_rows(m.id)] == [SKU_B]
    print("[断言通过] 场景2：新品名无映射行——新建品名级行字段继承正确、"
          "unit_code 留空、is_incomplete=True、旧列同步")


# ---------------------------------------------------------------------------
# 场景 3：人工编辑只改重量（name_cn 不变）→ 映射零变化
# ---------------------------------------------------------------------------

def test_weight_only_edit_no_relink():
    _seed_factory_sku(F_C, SKU_C, name_cn=NAME_X_C, hs_code="4444.44",
                      unit_net_weight=1.0, unit_gross_weight=2.0)
    _seed_mapping(NAME_X_C, [SKU_C], hs_code="4444.44")

    before = _mapping_snapshot()
    inserted, updated = _run_upsert(F_C, [{
        "sku": SKU_C,
        "is_human_edited": True,
        # 未提交 name_cn（item.get("name_cn") is None → 不刷新），只改重量
        "calculation": {"calculated_unit_net": 9.9,
                        "calculated_unit_gross": 8.8},
    }])
    assert (inserted, updated) == (0, 1), (inserted, updated)
    after = _mapping_snapshot()
    assert after == before, f"只改重量不得改动映射: {before} → {after}"

    with get_session() as s:
        rec = s.scalar(select(FactorySKU).where(FactorySKU.sku_code == SKU_C))
        assert float(rec.unit_net_weight) == 9.9    # 重量确实刷新（UPDATE 生效）
        assert rec.name_cn == NAME_X_C
    print("[断言通过] 场景3：只改重量——重量刷新但映射行与子表零变化")


# ---------------------------------------------------------------------------
# 场景 4：老 SKU 未人工编辑 → 不触发 relink，映射零变化
# ---------------------------------------------------------------------------

def test_unedited_old_sku_no_relink():
    _seed_factory_sku(F_D, SKU_D, name_cn=NAME_X_D, hs_code="5555.55",
                      unit_net_weight=1.0, unit_gross_weight=2.0)
    _seed_mapping(NAME_X_D, [SKU_D], hs_code="5555.55")

    before = _mapping_snapshot()
    inserted, updated = _run_upsert(F_D, [{
        "sku": SKU_D,
        # 无 is_human_edited / update_history_weight：即便 item 带了新品名也不许动
        "name_cn": "品名丁篡改",
        "calculation": {"calculated_unit_net": 9.9,
                        "calculated_unit_gross": 9.9},
    }])
    assert (inserted, updated) == (0, 0), (inserted, updated)
    after = _mapping_snapshot()
    assert after == before, f"未人工编辑不得改动映射: {before} → {after}"

    with get_session() as s:
        rec = s.scalar(select(FactorySKU).where(FactorySKU.sku_code == SKU_D))
        assert rec.name_cn == NAME_X_D            # 主数据也未被篡改
    print("[断言通过] 场景4：老 SKU 未人工编辑——主数据与映射零变化，不触发 relink")


def main():
    test_relink_to_existing_mapping()
    test_relink_to_new_name_creates_row()
    test_weight_only_edit_no_relink()
    test_unedited_old_sku_no_relink()
    print("\nwriter_relink_test: PASS")


if __name__ == "__main__":
    main()
