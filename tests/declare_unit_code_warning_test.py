# -*- coding: utf-8 -*-
"""报关生成「映射命中但单位代码为空」告警补强测试（S2）。

背景：declare/aggregator._enrich 按品名查产品映射带出 inspection /
unit_code，未命中记 warning 不阻断。后续会有功能自动创建「品名存在但
unit_code 为空」的映射行，届时 lookup 命中空行、「未命中产品映射」告警
消失，空单位代码会静默写进报关单——因此命中但 unit_code 为空
（None/空串/纯空白）时须追加独立 warning。

纯函数层测试（build_mapping_index + aggregate_ticket），不跑图、不起
服务、不碰 DB。

覆盖：
1. 命中且 unit_code 有值 → 无任何新 warning（回归）；
2. 命中但 unit_code 为空（None / "" / 纯空白）→ 有
   「命中产品映射但单位代码为空」warning，且报关行仍生成（不阻断）；
3. 未命中 → 仍是原「未命中产品映射」warning，且无新 warning（回归）。

隔离（血泪红线 2026-08-11，与 tests/factory_skip_test.py 同模式）：
先 import 全部 app 模块，再调 isolate_to_tmp——虽然本测试不走 DB，
仍按铁律隔离，防未来 import 链变化误触真实库。

用法（在 app/ 目录下）：
  python3 tests/declare_unit_code_warning_test.py
  PYTHONPATH=. python3 -m pytest tests/declare_unit_code_warning_test.py -q
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

from app.declare.aggregator import aggregate_ticket  # noqa: E402
from app.declare.mapping import build_mapping_index  # noqa: E402
from app.split.schemas import RawItem  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_declare_unit_code_test_")

# ---- 测试数据 ----

EMPTY_UNIT_WARNING = "命中产品映射但单位代码为空"
MISS_WARNING_PREFIX = "未命中产品映射："


def _row(name_cn: str, kanri: str = "K1") -> RawItem:
    """构造一行最小 RawItem（品名可变，其余从简）。"""
    return RawItem(
        kanri_no=kanri,
        port="東京港",
        container_type="40HQ",
        maker="测试工厂",
        sku="4900000000001",
        name_cn=name_cn,
        net_weight=1.0,
        gross_weight=1.2,
        pcs=10,
        qty_pieces=100,
        amount=500.0,
        currency="USD",
    )


def _mapping(name_cn: str, unit_code) -> dict:
    """构造一条产品映射记录（dict 形式，_get 兼容）。"""
    return {
        "product_name_cn": name_cn,
        "sku_code": "",
        "factory_id": None,
        "inspection_required": False,
        "unit_code": unit_code,
    }


def _aggregate(name_cn: str, unit_code) -> "object":
    """单品名聚合：一个映射 + 一行源数据，返回 AggregateResult。"""
    index = build_mapping_index([_mapping(name_cn, unit_code)])
    return aggregate_ticket([_row(name_cn)], groups=[], mapping_index=index)


# ---- 用例 ----

def test_hit_with_unit_code_no_new_warning():
    """命中且 unit_code 有值 → 无任何 warning（回归），unit_code 带出。"""
    res = _aggregate("棉质衬衫", "033")
    assert len(res.rows) == 1
    assert res.rows[0].unit_code == "033"
    assert res.warnings == [], f"不应有任何 warning: {res.warnings}"


def test_hit_with_none_unit_code_warns():
    """命中但 unit_code=None → 新 warning，且行仍生成（不阻断）。"""
    res = _aggregate("棉质衬衫", None)
    assert len(res.rows) == 1, "报关行必须仍生成（不阻断）"
    assert res.rows[0].unit_code == ""
    assert any(EMPTY_UNIT_WARNING in w for w in res.warnings), (
        f"缺「单位代码为空」warning: {res.warnings}"
    )
    assert not any(w.startswith(MISS_WARNING_PREFIX) for w in res.warnings)


def test_hit_with_empty_string_unit_code_warns():
    """命中但 unit_code="" → 新 warning。"""
    res = _aggregate("棉质衬衫", "")
    assert len(res.rows) == 1
    assert any(EMPTY_UNIT_WARNING in w for w in res.warnings)


def test_hit_with_whitespace_unit_code_warns():
    """命中但 unit_code 为纯空白 → 新 warning（视同为空）。"""
    res = _aggregate("棉质衬衫", "   ")
    assert len(res.rows) == 1
    assert any(EMPTY_UNIT_WARNING in w for w in res.warnings)


def test_miss_keeps_original_warning_only():
    """未命中 → 仍是原「未命中产品映射」warning，且无新 warning（回归）。"""
    index = build_mapping_index([_mapping("别的品名", "033")])
    res = aggregate_ticket([_row("棉质衬衫")], groups=[], mapping_index=index)
    assert len(res.rows) == 1
    assert res.rows[0].unit_code == ""
    assert any(
        w == f"{MISS_WARNING_PREFIX}棉质衬衫" for w in res.warnings
    ), f"缺「未命中产品映射」warning: {res.warnings}"
    assert not any(EMPTY_UNIT_WARNING in w for w in res.warnings)


def test_warning_contains_product_name():
    """新 warning 文案带品名，与既有 warning 同风格。"""
    res = _aggregate("棉质衬衫", None)
    assert any("棉质衬衫" in w and EMPTY_UNIT_WARNING in w
               for w in res.warnings)


# ---- 脚本直跑入口 ----

def main():
    test_hit_with_unit_code_no_new_warning()
    test_hit_with_none_unit_code_warns()
    test_hit_with_empty_string_unit_code_warns()
    test_hit_with_whitespace_unit_code_warns()
    test_miss_keeps_original_warning_only()
    test_warning_contains_product_name()
    print("\ndeclare_unit_code_warning_test: PASS")


if __name__ == "__main__":
    main()
