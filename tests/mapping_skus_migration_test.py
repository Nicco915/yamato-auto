# -*- coding: utf-8 -*-
"""产品映射 SKU 关联子表（product_mapping_skus）+ 启动幂等迁移测试（S1 地基）。

覆盖：
1. 建表后子表可写可查；UniqueConstraint(mapping_id, sku_code) 生效
   （重复插入同 (mapping_id, sku) 直接 commit 报 IntegrityError；
   经 ensure_mapping_skus_migrated 路径则幂等跳过不报错）；
2. ensure_mapping_skus_migrated：把旧列 sku_code 只读搬迁进子表；
   重复运行幂等（第二次 0 行）；sku_code 为空（NULL/空串）的行不产生子表行；
   旧列值搬迁后不清空（回滚保险）；
3. relationship 级联删除：删 ProductMapping 行后其子表行消失。

隔离（血泪红线 2026-08-11，与 tests/factory_skip_test.py 同模式）：
先 import 全部 app 模块，再调 validation/_test_isolation.isolate_to_tmp
（llm_client 的 load_dotenv override 已在 import 时执行完毕，此刻重设
env + 清缓存才有效，且带真实库路径断言守卫）。绝不触碰 app/data/ 真实库。

用法（在 app/ 目录下）：
  PYTHONPATH=. python3 -m pytest tests/mapping_skus_migration_test.py -q
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

import pytest  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.db.models import ProductMapping, ProductMappingSku  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.db.sync import ensure_mapping_skus_migrated  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）；
# engine 是惰性单例，首次 get_session 才按隔离后的 settings 建临时库（自动 create_all 建新表）
TMP = isolate_to_tmp("yamato_mapping_skus_test_")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _clear_tables() -> None:
    """清空子表与映射表，回到干净的初始状态。"""
    with get_session() as s:
        s.query(ProductMappingSku).delete()
        s.query(ProductMapping).delete()
        s.commit()


def _link_rows() -> list[tuple[int, str]]:
    with get_session() as s:
        return [(r.mapping_id, r.sku_code) for r in s.query(ProductMappingSku).all()]


# ---------------------------------------------------------------------------
# 1. 子表可写可查 + 唯一约束
# ---------------------------------------------------------------------------

def test_sku_link_table_writable_and_unique():
    _clear_tables()
    with get_session() as s:
        m = ProductMapping(product_name_cn="测试品名甲")
        s.add(m)
        s.flush()
        s.add(ProductMappingSku(mapping_id=m.id, sku_code="4900000000001"))
        s.add(ProductMappingSku(mapping_id=m.id, sku_code="4900000000002"))
        s.commit()
        mid = m.id

    rows = _link_rows()
    assert sorted(rows) == [(mid, "4900000000001"), (mid, "4900000000002")]

    # 唯一约束：重复插入同 (mapping_id, sku_code) 报 IntegrityError
    with get_session() as s:
        s.add(ProductMappingSku(mapping_id=mid, sku_code="4900000000001"))
        with pytest.raises(IntegrityError):
            s.commit()
    # 失败后表中仍只有原来两行
    assert len(_link_rows()) == 2


# ---------------------------------------------------------------------------
# 2. 迁移：旧列搬迁 + 幂等 + 空值跳过 + 旧列不清空
# ---------------------------------------------------------------------------

def test_migration_moves_legacy_sku_column():
    _clear_tables()
    with get_session() as s:
        m1 = ProductMapping(product_name_cn="品名A", sku_code="4900000000011")
        m2 = ProductMapping(product_name_cn="品名B", sku_code="4900000000012")
        m3 = ProductMapping(product_name_cn="品名C")            # NULL：不产生子表行
        m4 = ProductMapping(product_name_cn="品名D", sku_code="")  # 空串：不产生子表行
        s.add_all([m1, m2, m3, m4])
        s.commit()
        ids = (m1.id, m2.id, m3.id, m4.id)

    added = ensure_mapping_skus_migrated()
    assert added == 2
    rows = sorted(_link_rows())
    assert rows == [(ids[0], "4900000000011"), (ids[1], "4900000000012")]

    # 旧列只读搬迁：值不清空（回滚保险）
    with get_session() as s:
        assert s.get(ProductMapping, ids[0]).sku_code == "4900000000011"
        assert s.get(ProductMapping, ids[1]).sku_code == "4900000000012"

    # 幂等：重复运行 0 行，子表无重复
    assert ensure_mapping_skus_migrated() == 0
    assert len(_link_rows()) == 2


def test_migration_incremental_after_manual_link():
    """子表已有同 (mapping_id, sku) 行时，迁移幂等跳过、不重复插入。"""
    _clear_tables()
    with get_session() as s:
        m = ProductMapping(product_name_cn="品名E", sku_code="4900000000021")
        s.add(m)
        s.flush()
        s.add(ProductMappingSku(mapping_id=m.id, sku_code="4900000000021"))  # 已存在
        s.commit()
    assert ensure_mapping_skus_migrated() == 0
    assert len(_link_rows()) == 1


# ---------------------------------------------------------------------------
# 3. relationship 级联删除
# ---------------------------------------------------------------------------

def test_cascade_delete_removes_links():
    _clear_tables()
    with get_session() as s:
        m = ProductMapping(product_name_cn="品名F")
        s.add(m)
        s.flush()
        s.add(ProductMappingSku(mapping_id=m.id, sku_code="4900000000031"))
        s.add(ProductMappingSku(mapping_id=m.id, sku_code="4900000000032"))
        s.commit()
        mid = m.id
    assert len(_link_rows()) == 2

    with get_session() as s:
        s.delete(s.get(ProductMapping, mid))
        s.commit()
    assert _link_rows() == []
