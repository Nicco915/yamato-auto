# -*- coding: utf-8 -*-
"""SKU ↔ 品名映射归属三原子操作单元测试（2026-09-08 映射重构：SKU → 单位代码查找表）。

直接调 app.db.sync 的三个新 API（get_session 建临时数据，无需跑图）：

attach_sku_to_mapping（按中文品名挂接）：
- 命中既有行 → 追加进 SKU 列表（幂等，不重复）；既有字段（unit_code/hs 等）一概不改；
- 未命中 → 新建品名级行：hs_code/inspection_required/name_en 从 SKU 一次性继承，
  unit_code=None，is_incomplete 恒为 True（新语义 = unit_code 空），旧列 sku_code 同步；
- 品名 None / 纯空格 → 返回 None，不产生任何映射行/子表行；
- SKU 已被其他品名行占用 → 返回 None 且不挂不建行；
- 同品名多行 → 挂到最近更新行（updated_at 同秒并列时 id 大者优先）。

detach_sku_from_mappings（从所有映射行摘除）：
- 一个 SKU 挂在多行（含仅旧列 sku_code 匹配的未迁移老数据行）→ 全部摘除；
- 旧列等于被摘 SKU 时同步为剩余列表首个 / None（防启动迁移幽灵搬回）；
- 摘空后映射行本身保留（品名级兜底行）；不影响其他 SKU 的关联。

relink_sku_to_name（detach + attach 组合）：
- SKU 从品名 X 行移到既有品名 Y 行：detached_from 含 X，action="appended"；
- 移到不存在的品名：action="created"，新行字段继承正确；
- 品名传 None：只 detach，action 为 None。

防幽灵回归：detach 后跑 ensure_mapping_skus_migrated()，
被摘 SKU 不会被启动迁移从旧列搬回任何映射行。

隔离（血泪红线 2026-08-11，与 tests/mapping_skus_migration_test.py 同模板）：
先 import 全部 app 模块，再调 validation/_test_isolation.isolate_to_tmp。
绝不触碰 app/data/ 真实库。

用法（在 worktree 根目录下）：
  python3 tests/sku_mapping_sync_test.py
  PYTHONPATH=. python3 -m pytest tests/sku_mapping_sync_test.py -q
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

from app.db.models import ProductMapping, ProductMappingSku  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.db.sync import (  # noqa: E402
    attach_sku_to_mapping,
    detach_sku_from_mappings,
    ensure_mapping_skus_migrated,
    relink_sku_to_name,
)

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）；
# engine 是惰性单例，首次 get_session 才按隔离后的 settings 建临时库
TMP = isolate_to_tmp("yamato_sku_mapping_sync_test_")

_SEQ = itertools.count(1)


def _sku() -> str:
    """每个用例唯一 SKU：detach/attach 按 sku_code 全表匹配，复用会被前序用例污染。"""
    return f"4907777{next(_SEQ):06d}"


def _links_of(mapping_id: int) -> list[str]:
    """子表 SKU 列表（按 id 排序）。"""
    with get_session() as s:
        rows = (
            s.query(ProductMappingSku)
            .filter(ProductMappingSku.mapping_id == mapping_id)
            .order_by(ProductMappingSku.id)
            .all()
        )
        return [r.sku_code for r in rows]


def _get_mapping(mapping_id: int) -> ProductMapping:
    with get_session() as s:
        m = s.get(ProductMapping, mapping_id)
        s.expunge(m)
        return m


def _mappings_by_name(name: str) -> list[ProductMapping]:
    with get_session() as s:
        rows = (
            s.query(ProductMapping)
            .filter(ProductMapping.product_name_cn == name)
            .order_by(ProductMapping.id)
            .all()
        )
        for m in rows:
            s.expunge(m)
        return rows


def _mapping_exists(mapping_id: int) -> bool:
    with get_session() as s:
        return s.get(ProductMapping, mapping_id) is not None


def _sku_link_rows(sku_code: str) -> list[tuple[int, str]]:
    """全库子表中含该 SKU 的 (mapping_id, sku_code) 行。"""
    with get_session() as s:
        return [
            (r.mapping_id, r.sku_code)
            for r in s.query(ProductMappingSku)
            .filter(ProductMappingSku.sku_code == sku_code)
            .all()
        ]


def _table_counts() -> tuple[int, int]:
    with get_session() as s:
        return (
            s.query(ProductMapping).count(),
            s.query(ProductMappingSku).count(),
        )


# ---------------------------------------------------------------------------
# 1. attach_sku_to_mapping：命中追加 + 幂等 + 既有字段不动
# ---------------------------------------------------------------------------

def test_attach_appends_to_existing_and_idempotent():
    name = "挂接既有品名"
    sku1, sku2 = _sku(), _sku()
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=name, hs_code="9999.99",
            inspection_required=True, name_en="EXISTING EN",
            unit_code="007", is_incomplete=False,
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        mid = m.id

    with get_session() as s:
        r = attach_sku_to_mapping(
            s, sku_code=sku1, name_cn=name,
            hs_code="1111.11", inspection_required=False,  # 与既有行不同：不得覆盖
            factory_name="挂接厂")
        assert r == "appended", r
        s.commit()
    m = _get_mapping(mid)
    assert _links_of(mid) == [sku1]
    assert m.sku_code == sku1                      # 旧列原本为空 → 同步为列表首个
    assert m.unit_code == "007"                    # 既有字段一概不改
    assert m.hs_code == "9999.99"
    assert m.inspection_required is True
    assert m.name_en == "EXISTING EN"
    assert m.is_incomplete is False

    with get_session() as s:
        r = attach_sku_to_mapping(
            s, sku_code=sku1, name_cn=name, factory_name="挂接厂")
        assert r is None, f"幂等重跑应返回 None: {r}"  # 已在列表 → 不动
        r = attach_sku_to_mapping(
            s, sku_code=sku2, name_cn=name,
            hs_code="2222.22", factory_name="挂接厂")
        assert r == "appended", r
        s.commit()
    m = _get_mapping(mid)
    assert _links_of(mid) == [sku1, sku2]          # 无重复子表行
    assert m.sku_code == sku1                      # 旧列已有值 → 保持列表首个
    assert m.hs_code == "9999.99"                  # 追加不反向回填
    print("[断言通过] attach：命中追加；幂等不重复；既有行字段不被改动")


def test_attach_creates_row_with_inheritance():
    sku = _sku()
    before_m, before_l = _table_counts()
    with get_session() as s:
        r = attach_sku_to_mapping(
            s, sku_code=sku, name_cn="  挂接新品名  ",   # 前后空格应 strip
            hs_code="1234.56", inspection_required=True,
            name_en="  CREATED EN  ", factory_name="挂接厂")
        assert r == "created", r
        s.commit()
    after_m, after_l = _table_counts()
    assert after_m == before_m + 1 and after_l == before_l + 1
    rows = _mappings_by_name("挂接新品名")
    assert len(rows) == 1
    m = rows[0]
    assert m.hs_code == "1234.56"                  # 从 SKU 一次性继承
    assert m.inspection_required is True
    assert m.name_en == "CREATED EN"               # 继承且 strip
    assert m.unit_code is None                     # 计量单位无源可继承，留空待人工补
    assert m.is_incomplete is True                 # 新语义 = unit_code 空，建行必 True
    assert m.sku_code == sku                       # 旧列与列表同步
    assert _links_of(m.id) == [sku]
    print("[断言通过] attach：未命中建行，字段继承正确，unit_code 留空，is_incomplete=True")


def test_attach_blank_name_no_action():
    sku = _sku()
    before_m, before_l = _table_counts()
    with get_session() as s:
        assert attach_sku_to_mapping(
            s, sku_code=sku, name_cn=None, hs_code="1234",
            factory_name="挂接厂") is None
        assert attach_sku_to_mapping(
            s, sku_code=sku, name_cn="   ", hs_code="1234",
            factory_name="挂接厂") is None
        s.commit()
    assert _table_counts() == (before_m, before_l)
    assert _sku_link_rows(sku) == []
    print("[断言通过] attach：品名 None/纯空格 → 不动作")


def test_attach_conflict_skip_when_sku_owned_by_other_name():
    sku = _sku()
    with get_session() as s:
        m = ProductMapping(product_name_cn="占用方品名", hs_code="4444.44")
        s.add(m)
        s.flush()
        m.sku_links.append(ProductMappingSku(sku_code=sku))
        s.commit()
        s.refresh(m)
        owner_id = m.id
    before_m, before_l = _table_counts()
    with get_session() as s:
        r = attach_sku_to_mapping(
            s, sku_code=sku, name_cn="抢挂品名", hs_code="5555.55",
            factory_name="挂接厂")
        assert r is None, f"被其他品名占用应跳过: {r}"
        s.commit()
    assert _table_counts() == (before_m, before_l)  # 不建行、不挂接
    assert _mappings_by_name("抢挂品名") == []
    assert _links_of(owner_id) == [sku]             # 原归属不动
    print("[断言通过] attach：SKU 被其他品名行占用 → 跳过不抢挂")


def test_attach_multi_row_picks_latest():
    sku = _sku()
    with get_session() as s:
        m1 = ProductMapping(product_name_cn="多行同名品名", hs_code="1111")
        m2 = ProductMapping(product_name_cn="多行同名品名", hs_code="2222")
        s.add_all([m1, m2])
        s.commit()
        s.refresh(m1)
        s.refresh(m2)
        older_id, latest_id = m1.id, m2.id
        r = attach_sku_to_mapping(
            s, sku_code=sku, name_cn="多行同名品名",
            hs_code="3333", factory_name="挂接厂")
        assert r == "appended", r
        s.commit()
    assert _links_of(latest_id) == [sku]            # updated_at 同秒并列 → id 大者优先
    assert _links_of(older_id) == []
    print("[断言通过] attach：同品名多行 → 挂到最近更新行（id 大者优先）")


# ---------------------------------------------------------------------------
# 2. detach_sku_from_mappings：多行摘除 + 旧列同步 + 行保留
# ---------------------------------------------------------------------------

def test_detach_from_multiple_rows_and_legacy_sync():
    sku, other = _sku(), _sku()
    with get_session() as s:
        # m1：子表 [sku, other]，旧列=sku → 摘后旧列同步为剩余列表首个 other
        m1 = ProductMapping(product_name_cn="摘除品名一", sku_code=sku)
        m1.sku_links.append(ProductMappingSku(sku_code=sku))
        m1.sku_links.append(ProductMappingSku(sku_code=other))
        # m2：仅旧列匹配的老数据行（无子表行）→ 摘后旧列 None
        m2 = ProductMapping(product_name_cn="摘除品名二", sku_code=sku)
        # m3：子表 [sku]，旧列为 None（旧列不等于被摘 SKU → 不碰）
        m3 = ProductMapping(product_name_cn="摘除品名三", sku_code=None)
        m3.sku_links.append(ProductMappingSku(sku_code=sku))
        # m4：与 sku 无关的行 → 完全不受影响
        m4 = ProductMapping(product_name_cn="无关品名", sku_code=other)
        m4.sku_links.append(ProductMappingSku(sku_code=other))
        s.add_all([m1, m2, m3, m4])
        s.commit()
        for m in (m1, m2, m3, m4):
            s.refresh(m)
        ids = (m1.id, m2.id, m3.id, m4.id)

    with get_session() as s:
        affected = detach_sku_from_mappings(s, sku)
        affected_ids = {m.id for m in affected}
        assert affected_ids == {ids[0], ids[1], ids[2]}, \
            f"应命中 m1/m2/m3（含仅旧列匹配行）: {affected_ids}"
        s.commit()

    m1, m2, m3, m4 = (_get_mapping(i) for i in ids)
    assert _links_of(ids[0]) == [other]
    assert m1.sku_code == other                     # 旧列 = 剩余列表首个
    assert _links_of(ids[1]) == []
    assert m2.sku_code is None                      # 摘空 → 旧列 None
    assert _links_of(ids[2]) == []
    assert m3.sku_code is None                      # 旧列本非被摘 SKU，保持原样
    # 摘空后映射行保留（品名级兜底行，不自动删行）
    assert all(_mapping_exists(i) for i in ids)
    # 不影响其他 SKU 的关联
    assert _links_of(ids[3]) == [other] and m4.sku_code == other
    assert _sku_link_rows(sku) == []
    print("[断言通过] detach：多行（含仅旧列匹配行）全部摘除；旧列同步；行保留；其他 SKU 不动")


def test_detach_nonexistent_sku_returns_empty():
    with get_session() as s:
        assert detach_sku_from_mappings(s, _sku()) == []
        assert detach_sku_from_mappings(s, "") == []
        assert detach_sku_from_mappings(s, "   ") == []
    print("[断言通过] detach：不存在/空 SKU → 返回空列表，不动作")


def test_detach_then_migration_no_ghost():
    """防幽灵：detach 清过旧列后，启动迁移不得把被摘 SKU 搬回子表。"""
    sku = _sku()
    with get_session() as s:
        # 子表+旧列双载的行，与仅旧列的老数据行，各一
        m1 = ProductMapping(product_name_cn="幽灵品名一", sku_code=sku)
        m1.sku_links.append(ProductMappingSku(sku_code=sku))
        m2 = ProductMapping(product_name_cn="幽灵品名二", sku_code=sku)
        s.add_all([m1, m2])
        s.commit()
    with get_session() as s:
        affected = detach_sku_from_mappings(s, sku)
        assert len(affected) == 2
        s.commit()

    added = ensure_mapping_skus_migrated()
    assert _sku_link_rows(sku) == [], \
        f"迁移不得把被摘 SKU 搬回（本次新增 {added} 行）"
    with get_session() as s:
        ghosts = (
            s.query(ProductMapping)
            .filter(ProductMapping.sku_code == sku)
            .all()
        )
        assert ghosts == [], f"旧列不得残留被摘 SKU: {ghosts}"
    print("[断言通过] 防幽灵：detach 后 ensure_mapping_skus_migrated 不搬回被摘 SKU")


# ---------------------------------------------------------------------------
# 3. relink_sku_to_name：detach + attach 组合
# ---------------------------------------------------------------------------

def test_relink_move_to_existing_name():
    sku, other = _sku(), _sku()
    with get_session() as s:
        mx = ProductMapping(product_name_cn="改属品名X", sku_code=sku)
        mx.sku_links.append(ProductMappingSku(sku_code=sku))
        my = ProductMapping(product_name_cn="改属品名Y", sku_code=other,
                            unit_code="008", is_incomplete=False)
        my.sku_links.append(ProductMappingSku(sku_code=other))
        s.add_all([mx, my])
        s.commit()
        s.refresh(mx)
        s.refresh(my)
        x_id, y_id = mx.id, my.id

    with get_session() as s:
        r = relink_sku_to_name(
            s, sku_code=sku, name_cn="改属品名Y", factory_name="改属厂")
        assert r["detached_from"] == [(x_id, "改属品名X")], r
        assert r["action"] == "appended", r
        s.commit()

    mx, my = _get_mapping(x_id), _get_mapping(y_id)
    assert _links_of(x_id) == [] and mx.sku_code is None   # 旧归属摘空，行保留
    assert _mapping_exists(x_id)
    assert _links_of(y_id) == [other, sku]                 # 追加进既有 Y 行
    assert my.unit_code == "008" and my.is_incomplete is False  # Y 行字段不动
    print("[断言通过] relink：从品名 X 移到既有品名 Y（detached_from 含 X，action=appended）")


def test_relink_move_to_new_name_creates():
    sku = _sku()
    with get_session() as s:
        mx = ProductMapping(product_name_cn="改属旧品名", sku_code=sku)
        mx.sku_links.append(ProductMappingSku(sku_code=sku))
        s.add(mx)
        s.commit()
        s.refresh(mx)
        x_id = mx.id

    with get_session() as s:
        r = relink_sku_to_name(
            s, sku_code=sku, name_cn="改属全新品名",
            hs_code="5555.66", inspection_required=True,
            name_en="RELINK EN", factory_name="改属厂")
        assert r["detached_from"] == [(x_id, "改属旧品名")], r
        assert r["action"] == "created", r
        s.commit()

    assert _links_of(x_id) == [] and _get_mapping(x_id).sku_code is None
    rows = _mappings_by_name("改属全新品名")
    assert len(rows) == 1
    m = rows[0]
    assert m.hs_code == "5555.66"                   # 字段继承正确
    assert m.inspection_required is True
    assert m.name_en == "RELINK EN"
    assert m.unit_code is None and m.is_incomplete is True
    assert m.sku_code == sku
    assert _links_of(m.id) == [sku]
    print("[断言通过] relink：移到不存在品名 → 建行（action=created），字段继承正确")


def test_relink_blank_name_detach_only():
    sku = _sku()
    with get_session() as s:
        mx = ProductMapping(product_name_cn="清空品名归属", sku_code=sku)
        mx.sku_links.append(ProductMappingSku(sku_code=sku))
        s.add(mx)
        s.commit()
        s.refresh(mx)
        x_id = mx.id

    before_m, _ = _table_counts()
    with get_session() as s:
        r = relink_sku_to_name(
            s, sku_code=sku, name_cn=None, factory_name="改属厂")
        assert r["detached_from"] == [(x_id, "清空品名归属")], r
        assert r["action"] is None, r                       # 品名空 → 只 detach
        s.commit()

    after_m, _ = _table_counts()
    assert after_m == before_m                              # 不建行
    assert _sku_link_rows(sku) == []                        # SKU 不属于任何映射
    assert _links_of(x_id) == [] and _get_mapping(x_id).sku_code is None
    assert _mapping_exists(x_id)                            # 行保留
    print("[断言通过] relink：品名 None → 只 detach，action=None，不建行")


def main():
    test_attach_appends_to_existing_and_idempotent()
    test_attach_creates_row_with_inheritance()
    test_attach_blank_name_no_action()
    test_attach_conflict_skip_when_sku_owned_by_other_name()
    test_attach_multi_row_picks_latest()
    test_detach_from_multiple_rows_and_legacy_sync()
    test_detach_nonexistent_sku_returns_empty()
    test_detach_then_migration_no_ghost()
    test_relink_move_to_existing_name()
    test_relink_move_to_new_name_creates()
    test_relink_blank_name_detach_only()
    print("\nsku_mapping_sync_test: PASS")


if __name__ == "__main__":
    main()
