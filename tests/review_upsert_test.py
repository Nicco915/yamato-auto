# -*- coding: utf-8 -*-
"""Node5 合规字段编辑检测 + Node6 落库（_upsert_db）回归测试。

背景（2026-08-05 修复，commit 295ffc0/999a949）：
审核页只改中文品名/HS/商检时，旧检测只看三项底层数值，is_human_edited
恒为 False，Node6 跳过 UPDATE，数据库不更新。修复点：
- human_review._norm_text/_norm_bool：提交值归一化（空串→None，
  "0"/"false"/"否"→False，杜绝 bool("0")==True）；
- human_review._detect_compliance_edits：合规字段比对（老 SKU 原值从
  db_record 回退），命中即置 is_human_edited；
- writer._upsert_db：老 SKU 且 is_human_edited 时 UPDATE 合规字段。

隔离原则（与 tests/logging_context_test.py 相同，绝不碰 app/data/ 生产库）：
import 全部 app 模块后调用 validation/_test_isolation.isolate_to_tmp，
master.db 指向临时目录；session 引擎为惰性单例，首次 _upsert_db 才建连接。

用法（在 app/ 目录下）：
  python3 tests/review_upsert_test.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["EXTRACTION_MOCK"] = "1"  # 防御：不走 LLM（本测试不涉及提取线）

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from sqlalchemy import select  # noqa: E402

from app.db.models import Factory, FactorySKU  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.nodes.human_review import (  # noqa: E402
    _detect_compliance_edits,
    _norm_bool,
    _norm_text,
)
from app.nodes.writer import _upsert_db  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（llm_client 的 load_dotenv override 红线）；
# 引擎惰性，首次 get_session 才按隔离后的 settings 建临时库
TMP = isolate_to_tmp("yamato_review_upsert_test_")

FACTORY = "测试厂"


# ---------------------------------------------------------------------------
# 1. _norm_text / _norm_bool 单元测试
# ---------------------------------------------------------------------------

def test_norm_text():
    assert _norm_text(None) is None
    assert _norm_text("") is None
    assert _norm_text("   ") is None          # 纯空格 → None（防空串误报）
    assert _norm_text("\t\n") is None
    assert _norm_text(" 自行车 ") == "自行车"   # strip
    assert _norm_text("HS1234") == "HS1234"
    print("[断言通过] _norm_text：空串/纯空格→None，其余 strip 后保留")


def test_norm_bool():
    # None / 空串 / 纯空格 → None（未填写）
    assert _norm_bool(None) is None
    assert _norm_bool("") is None
    assert _norm_bool("   ") is None
    # 真值族
    assert _norm_bool(True) is True
    assert _norm_bool("1") is True
    assert _norm_bool("true") is True
    assert _norm_bool("TRUE") is True
    assert _norm_bool("yes") is True
    assert _norm_bool("是") is True
    assert _norm_bool(1) is True
    # 假值族（修复核心：bool("0")==True 的隐患）
    assert _norm_bool(False) is False
    assert _norm_bool("0") is False
    assert _norm_bool("false") is False
    assert _norm_bool("False") is False
    assert _norm_bool("no") is False
    assert _norm_bool("否") is False
    assert _norm_bool(0) is False
    # 无法识别 → None（视为未填写，不触发更新）
    assert _norm_bool("abc") is None
    assert _norm_bool("2") is None
    assert _norm_bool("待定") is None
    print("[断言通过] _norm_bool：真/假/无法识别三族归一化")


# ---------------------------------------------------------------------------
# 2. Node5 合规字段编辑检测（_detect_compliance_edits 等价路径）
# ---------------------------------------------------------------------------

def test_detect_compliance_edits():
    # 基座：老 SKU，主库原值在 db_record（payload 顶层亦为同值初值）
    base = {
        "sku": "4900000000001",
        "name_cn": "旧品名",
        "hs_code": "1234.56",
        "inspection_required": False,
        "db_record": {"name_cn": "旧品名", "hs_code": "1234.56",
                      "inspection_required": False},
    }

    # 改了 name_cn → 判 edited
    assert _detect_compliance_edits({**base, "name_cn": "新品名"}, base) is True
    # 改了 hs_code / name_en → 判 edited
    assert _detect_compliance_edits({**base, "hs_code": "9999.99"}, base) is True
    assert _detect_compliance_edits({**base, "name_en": "NEW NAME"}, base) is True
    # 提交值与原值相同 → 不判
    assert _detect_compliance_edits(dict(base), base) is False
    # 提交值仅首尾多空格，归一化后相同 → 不判
    assert _detect_compliance_edits({**base, "name_cn": "  旧品名 "}, base) is False
    # 提交空串/纯空格 → 归一化为 None，视为未改 → 不判
    assert _detect_compliance_edits({**base, "name_cn": ""}, base) is False
    assert _detect_compliance_edits({**base, "name_cn": "   "}, base) is False

    # 商检：提交 "1"（字符串）vs 原值 True（布尔）→ 归一化后相同 → 不判
    base_true = {**base, "inspection_required": True,
                 "db_record": {**base["db_record"], "inspection_required": True}}
    assert _detect_compliance_edits({**base_true, "inspection_required": "1"},
                                    base_true) is False
    # 商检：提交 "0" vs 原值 True → 判 edited（bool("0") 隐患的回归钉）
    assert _detect_compliance_edits({**base_true, "inspection_required": "0"},
                                    base_true) is True
    # 商检：提交 True vs 原值 False → 判 edited
    assert _detect_compliance_edits({**base, "inspection_required": True},
                                    base) is True
    # 商检：提交无法识别字符串 → None，视为未填 → 不判
    assert _detect_compliance_edits({**base, "inspection_required": "abc"},
                                    base) is False

    # 原值回退：base 顶层缺字段（None），从 db_record 取原值
    base_no_top = {"sku": "4900000000002", "db_record": {"name_cn": "库中名"}}
    assert _detect_compliance_edits({"sku": "4900000000002", "name_cn": "库中名"},
                                    base_no_top) is False
    assert _detect_compliance_edits({"sku": "4900000000002", "name_cn": "别的名"},
                                    base_no_top) is True

    # 新 SKU：db_record 为空，补录任意合规字段即判 edited
    base_new = {"sku": "4900000000003", "db_record": {}}
    assert _detect_compliance_edits({"sku": "4900000000003", "name_cn": "新品"},
                                    base_new) is True
    # 新 SKU 全部留空 → 不判（无值可写）
    assert _detect_compliance_edits({"sku": "4900000000003"}, base_new) is False
    print("[断言通过] _detect_compliance_edits：改动/同值/空串/回退/新SKU/布尔归一化")


# ---------------------------------------------------------------------------
# 3. writer._upsert_db 行为测试（临时 sqlite 库）
# ---------------------------------------------------------------------------

def _seed_factory_with_sku(sku: str, **fields) -> None:
    """造一条老 SKU 主数据记录（工厂不存在则建，存在则复用）。"""
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == FACTORY))
        if factory is None:
            factory = Factory(factory_name=FACTORY)
            session.add(factory)
            session.flush()
        session.add(FactorySKU(factory_id=factory.factory_id, sku_code=sku,
                               **fields))
        session.commit()


def _get_sku(sku: str) -> FactorySKU | None:
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == FACTORY))
        if factory is None:
            return None
        return session.scalar(
            select(FactorySKU).where(
                FactorySKU.factory_id == factory.factory_id,
                FactorySKU.sku_code == sku))


def _state_with_item(item: dict) -> dict:
    return {"current_factory_data": {"factory_name": FACTORY,
                                     "calculated_items": [item]}}


def test_upsert_old_sku_name_cn_update():
    """老 SKU + 只改 name_cn 且 is_human_edited=True → UPDATE 1，库中为新值。"""
    _seed_factory_with_sku("SKU-U1", name_cn="旧品名", hs_code="1234",
                           inspection_required=False,
                           unit_net_weight=1.5, unit_gross_weight=2.0)
    item = {
        "sku": "SKU-U1",
        "is_human_edited": True,
        "name_cn": "新品名",          # Node5 归一化后挂到 item 的值
        "calculation": {"calculated_unit_net": 1.5,
                        "calculated_unit_gross": 2.0},
    }
    inserted, updated = _upsert_db(_state_with_item(item))
    assert (inserted, updated) == (0, 1), (inserted, updated)
    rec = _get_sku("SKU-U1")
    assert rec.name_cn == "新品名"
    assert rec.hs_code == "1234"                    # 未提交的字段不被清掉
    assert rec.inspection_required is False
    print("[断言通过] 老SKU改name_cn：UPDATE 1，新值落库，其余字段不动")


def test_upsert_old_sku_no_edit_noop():
    """老 SKU + is_human_edited=False（直接批准，回归 8/3-8/4 场景）
    → INSERT 0 / UPDATE 0，库值不变。"""
    _seed_factory_with_sku("SKU-N1", name_cn="原品名", hs_code="5678",
                           inspection_required=True,
                           unit_net_weight=3.0, unit_gross_weight=4.0)
    item = {
        "sku": "SKU-N1",
        "is_human_edited": False,
        # 即便 item 上带了值（如 payload 顶层初值透传），未编辑也不许落库
        "name_cn": "原品名",
        "calculation": {"calculated_unit_net": 9.9,
                        "calculated_unit_gross": 9.9},
    }
    inserted, updated = _upsert_db(_state_with_item(item))
    assert (inserted, updated) == (0, 0), (inserted, updated)
    rec = _get_sku("SKU-N1")
    assert rec.name_cn == "原品名"
    assert rec.hs_code == "5678"
    assert rec.inspection_required is True
    assert float(rec.unit_net_weight) == 3.0        # 重量也不被刷新
    print("[断言通过] 老SKU未编辑直接批准：INSERT 0 / UPDATE 0，库值不变")


def test_upsert_old_sku_inspection_bool():
    """老 SKU + inspection_required 提交 True（已归一化布尔）
    且 is_human_edited=True → 库中存布尔 True。"""
    _seed_factory_with_sku("SKU-B1", name_cn="品名",
                           inspection_required=False)
    item = {
        "sku": "SKU-B1",
        "is_human_edited": True,
        "inspection_required": True,   # Node5 已归一化为真布尔
        "calculation": {},
    }
    inserted, updated = _upsert_db(_state_with_item(item))
    assert (inserted, updated) == (0, 1), (inserted, updated)
    rec = _get_sku("SKU-B1")
    assert rec.inspection_required is True
    assert isinstance(rec.inspection_required, bool)
    print("[断言通过] 老SKU商检改True：UPDATE 1，库中存布尔")


def test_upsert_new_sku_insert():
    """新 SKU（库中无记录）+ 补录三字段 → INSERT 1，三字段正确落库。"""
    item = {
        "sku": "SKU-NEW1",
        "is_human_edited": True,
        "is_new_sku": True,
        "name_cn": "补录品名",
        "hs_code": "8708.99",
        "inspection_required": True,
        "calculation": {"calculated_unit_net": 0.25,
                        "calculated_unit_gross": 0.3},
    }
    inserted, updated = _upsert_db(_state_with_item(item))
    assert (inserted, updated) == (1, 0), (inserted, updated)
    rec = _get_sku("SKU-NEW1")
    assert rec is not None
    assert rec.name_cn == "补录品名"
    assert rec.hs_code == "8708.99"
    assert rec.inspection_required is True
    assert float(rec.unit_net_weight) == 0.25
    assert float(rec.unit_gross_weight) == 0.3
    print("[断言通过] 新SKU补录：INSERT 1，品名/HS/商检/单重全部落库")


def main():
    test_norm_text()
    test_norm_bool()
    test_detect_compliance_edits()
    test_upsert_old_sku_name_cn_update()
    test_upsert_old_sku_no_edit_noop()
    test_upsert_old_sku_inspection_bool()
    test_upsert_new_sku_insert()
    print("\nreview_upsert_test: PASS")


if __name__ == "__main__":
    main()
