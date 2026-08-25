# -*- coding: utf-8 -*-
"""新 SKU 落库自动挂产品映射回归测试（S5：writer._upsert_db → sync.auto_link_new_sku_to_mapping）。

覆盖：
1. e2e（EXTRACTION_MOCK 跑真实图 Node1→挂起→resume 批准→Node6 落库）：
   - 新 SKU 落库且品名在映射中不存在 → 自动建品名级行（字段继承、
     is_incomplete 口径、子表含该 SKU、旧列 sku_code 同步）；
   - 第二个同品名新 SKU 落库 → 不建行，追加进既有行的 SKU 列表；
   - 同 SKU 跨工厂再次落库 → 幂等无重复子表行、不重复建行；
   - name_cn 为空的新 SKU → 不触发任何映射动作；
   - 已有映射行的 unit_code/hs_code 不被自动挂接改动（只挂接，不反向回填）；
2. 老 SKU 重跑批次（record 非 None）→ 不触发自动挂接，映射零变化；
3. 单元级（直接调 sync.auto_link_new_sku_to_mapping）：
   - name_cn 空/纯空格 → 不动作；
   - 同品名多行 → 挂到最近更新行（id 大者优先兜底）；
   - SKU 已被其他品名映射占用 → 跳过不抢挂。

隔离（血泪红线 2026-08-11，照抄 tests/factory_skip_test.py 头部模板）：
先 import 全部 app 模块，再调 _test_isolation.isolate_to_tmp——
checkpoint/master db、output、sessions 全部指向临时目录，绝不碰 app/data/ 真实库。

用法（在 app/ 目录下）：
  python3 tests/mapping_auto_link_test.py
  PYTHONPATH=. python3 -m pytest tests/mapping_auto_link_test.py -q
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

from openpyxl import Workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.api import service  # noqa: E402
from app.db.models import ProductMapping, ProductMappingSku  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.db.sync import auto_link_new_sku_to_mapping  # noqa: E402
from app.nodes import extraction_node as en  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（load_dotenv override 红线）；
# graph/engine 都是惰性单例，首次调用才按隔离后的 settings 建临时库
TMP = isolate_to_tmp("yamato_mapping_auto_link_test_")

# ---- 测试夹具常量：五个工厂各 1 SKU（13 位刚性口径）----
F1, F2, F3, F4, F5 = "挂接厂甲", "挂接厂乙", "挂接厂丙", "挂接厂丁", "挂接厂戊"
SKU1 = "4900000001001"   # F1：新建品名级映射「测试品名甲」
SKU2 = "4900000001002"   # F2：同品名，追加进既有行
# F3 复用 SKU1（跨工厂同 SKU 再次落库 → 幂等无重复）
SKU4 = "4900000001004"   # F4：name_cn 留空，不触发映射动作
SKU5 = "4900000001005"   # F5：追加进预建映射「测试品名乙」（unit_code 不动）
NAME_A = "测试品名甲"
NAME_B = "测试品名乙"
THREAD = "MAP-AUTO-E2E"
THREAD_RERUN = "MAP-AUTO-RERUN"


def _make_fixtures() -> tuple[str, str]:
    """生成最小下游 xlsx（五工厂各 1 SKU）与上游工厂文件夹，返回路径。"""
    xlsx = TMP / "downstream.xlsx"
    wb = Workbook()
    ws = wb.active
    # Node6 首次写入时会在 SHOHIN_MEI_E 后插入 中文品名/净重/毛重 三列
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    ws.append([F1, SKU1, "ITEM-1", 10])
    ws.append([F2, SKU2, "ITEM-2", 20])
    ws.append([F3, SKU1, "ITEM-1", 30])
    ws.append([F4, SKU4, "ITEM-4", 40])
    ws.append([F5, SKU5, "ITEM-5", 50])
    wb.save(xlsx)

    upstream = TMP / "upstream"
    for f in (F1, F2, F3, F4, F5):
        (upstream / f).mkdir(parents=True)
    return str(xlsx), str(upstream)


def _seed_mapping_b() -> int:
    """预建映射「测试品名乙」：带 unit_code/hs_code，验证自动挂接不改既有字段。"""
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=NAME_B,
            hs_code="8710.00",
            inspection_required=False,
            name_en="ITEM B",
            unit_code="007",
            is_incomplete=False,
        )
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


def _all_mappings() -> list[ProductMapping]:
    with get_session() as s:
        return list(s.scalars(select(ProductMapping).order_by(ProductMapping.id)).all())


def _all_links() -> list[ProductMappingSku]:
    with get_session() as s:
        return list(s.scalars(select(ProductMappingSku).order_by(ProductMappingSku.id)).all())


# ---------------------------------------------------------------------------
# 1. 单元级：sync.auto_link_new_sku_to_mapping 边界行为
# ---------------------------------------------------------------------------

def test_unit_blank_name_no_action():
    """name_cn 为空/纯空格 → 返回 None，不产生任何映射行/子表行。"""
    before_m, before_l = len(_all_mappings()), len(_all_links())
    with get_session() as s:
        assert auto_link_new_sku_to_mapping(
            s, factory_name="单元厂", sku_code="4900000008001",
            name_cn=None, hs_code="1234") is None
        assert auto_link_new_sku_to_mapping(
            s, factory_name="单元厂", sku_code="4900000008001",
            name_cn="   ", hs_code="1234") is None
        s.commit()
    assert len(_all_mappings()) == before_m
    assert len(_all_links()) == before_l
    print("[断言通过] 单元：name_cn 空/纯空格不触发任何映射动作")


def test_unit_multi_row_picks_latest():
    """同品名多行 → 挂到最近更新行（updated_at 同秒并列时 id 大者优先）。"""
    with get_session() as s:
        m1 = ProductMapping(product_name_cn="单元品名丁", hs_code="1111")
        m2 = ProductMapping(product_name_cn="单元品名丁", hs_code="2222")
        s.add_all([m1, m2])
        s.commit()
        s.refresh(m1)
        s.refresh(m2)
        older_id, latest_id = m1.id, m2.id
        r = auto_link_new_sku_to_mapping(
            s, factory_name="单元厂", sku_code="4900000008002",
            name_cn="单元品名丁", hs_code="3333")
        s.commit()
    assert r == "appended"
    assert _link_rows(latest_id)[0].sku_code == "4900000008002"
    assert _link_rows(older_id) == []
    print("[断言通过] 单元：同品名多行挂到最近更新行（id 大者优先）")


def test_unit_conflict_skip():
    """SKU 已被其他品名映射占用 → 跳过不抢挂（与 UI 409 同语义），不建新行。"""
    with get_session() as s:
        m = ProductMapping(product_name_cn="单元品名丙", hs_code="4444")
        m.sku_links.append(ProductMappingSku(sku_code="4900000008003"))
        s.add(m)
        s.commit()
    before_m = len(_all_mappings())
    with get_session() as s:
        r = auto_link_new_sku_to_mapping(
            s, factory_name="单元厂", sku_code="4900000008003",
            name_cn="单元品名丙新", hs_code="5555")
        s.commit()
    assert r is None
    assert len(_all_mappings()) == before_m          # 不新建「单元品名丙新」行
    assert _mapping_rows("单元品名丙新") == []
    print("[断言通过] 单元：SKU 被其他品名占用时跳过挂接，不建行")


# ---------------------------------------------------------------------------
# 2. 端到端：五工厂批次跑真实图，Node6 落库触发自动挂接
# ---------------------------------------------------------------------------

def test_e2e_auto_link_create_append_idempotent(monkeypatch):
    # 全量 pytest 下同 factory_skip_test：强制回 mock 提取线
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")

    xlsx, upstream = _make_fixtures()
    mapping_b_id = _seed_mapping_b()

    # ---- 第一程：run 到 F1 挂起 ----
    r1 = service.run_until_interrupt(
        THREAD, downstream_file_path=xlsx, upstream_root=upstream)
    assert r1["status"] == "pending_human_review", f"未挂起: {r1}"
    assert r1["review_data"]["factory_name"] == F1

    # ---- F1：新 SKU 补录品名甲（品名不存在 → 应新建品名级映射行）----
    r2 = service.resume_order(THREAD, {"approved": True, "items": [{
        "sku": SKU1, "name_cn": NAME_A, "hs_code": "1234.56",
        "inspection_required": "1", "name_en": "TEST ITEM A",
    }]})
    assert r2["status"] == "pending_human_review", f"未二次挂起: {r2}"
    assert r2["review_data"]["factory_name"] == F2

    # ---- F2：同品名第二个新 SKU（应追加进既有行，不建行）----
    r3 = service.resume_order(THREAD, {"approved": True, "items": [{
        "sku": SKU2, "name_cn": NAME_A, "hs_code": "1234.56",
        "inspection_required": "1",
    }]})
    assert r3["status"] == "pending_human_review", f"未三次挂起: {r3}"
    assert r3["review_data"]["factory_name"] == F3

    # ---- F3：跨工厂同 SKU1 再次落库（幂等：不重复建行、不重复子表行）----
    r4 = service.resume_order(THREAD, {"approved": True, "items": [{
        "sku": SKU1, "name_cn": NAME_A, "hs_code": "1234.56",
        "inspection_required": "1",
    }]})
    assert r4["status"] == "pending_human_review", f"未四次挂起: {r4}"
    assert r4["review_data"]["factory_name"] == F4

    # ---- F4：name_cn 留空的新 SKU（不触发任何映射动作）----
    r5 = service.resume_order(THREAD, {"approved": True, "items": []})
    assert r5["status"] == "pending_human_review", f"未五次挂起: {r5}"
    assert r5["review_data"]["factory_name"] == F5

    # ---- F5：品名带前后空格提交（strip 后命中预建行，unit_code 不动）----
    r6 = service.resume_order(THREAD, {"approved": True, "items": [{
        "sku": SKU5, "name_cn": f"  {NAME_B}  ", "hs_code": "9999.00",
        "inspection_required": "0",
    }]})
    assert r6["status"] == "success", f"批次未正常完成: {r6}"

    # ---- 断言：甲/乙两个品名各恰 1 行（无重复建行）----
    # 注：与单元测试共享同一临时库，全表行数不断言，按品名维度核实
    all_m = _all_mappings()
    assert len(_mapping_rows(NAME_A)) == 1
    assert len(_mapping_rows(NAME_B)) == 1

    # 甲：自动新建的品名级行，字段从 SKU1 继承
    rows_a = _mapping_rows(NAME_A)
    assert len(rows_a) == 1, f"「{NAME_A}」应恰 1 行: {len(rows_a)}"
    ma = rows_a[0]
    assert ma.hs_code == "1234.56"
    assert ma.inspection_required is True
    assert ma.name_en == "TEST ITEM A"
    assert ma.unit_code is None                      # 无源可继承，留空
    assert ma.is_incomplete is False                 # hs_code 非空
    assert ma.sku_code == SKU1                       # 旧列与列表首个一致
    links_a = [l.sku_code for l in _link_rows(ma.id)]
    assert sorted(links_a) == sorted([SKU1, SKU2]), \
        f"甲行 SKU 列表应为 [{SKU1}, {SKU2}]: {links_a}"
    assert len(links_a) == len(set(links_a)), f"子表不得有重复: {links_a}"
    print("[断言通过] e2e：新建品名级行字段继承正确；同品名追加；跨工厂同 SKU 幂等无重复")

    # 乙：预建行只被追加挂接，unit_code/hs_code/商检 不被改动（无反向回填）
    rows_b = _mapping_rows(NAME_B)
    assert len(rows_b) == 1 and rows_b[0].id == mapping_b_id
    mb = rows_b[0]
    assert mb.unit_code == "007", f"unit_code 不得被自动挂接改动: {mb.unit_code}"
    assert mb.hs_code == "8710.00", f"hs_code 不得被 SKU 值覆盖: {mb.hs_code}"
    assert mb.inspection_required is False
    assert mb.name_en == "ITEM B"
    links_b = [l.sku_code for l in _link_rows(mb.id)]
    assert links_b == [SKU5], f"乙行 SKU 列表应为 [{SKU5}]: {links_b}"
    print("[断言通过] e2e：预建行 unit_code/hs_code/商检不被自动挂接改动（strip 命中）")

    # F4：name_cn 为空 → 不触发映射动作（无含 SKU4 的子表行，无空名映射行）
    assert all(SKU4 not in [l.sku_code for l in _link_rows(m.id)] for m in all_m)
    assert all(m.product_name_cn.strip() for m in all_m)
    print("[断言通过] e2e：name_cn 为空的新 SKU 不触发任何映射动作")


def test_rerun_old_sku_no_mapping_action(monkeypatch):
    """老 SKU 重跑批次（record 非 None，UPDATE/不动分支）→ 不触发自动挂接。"""
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")

    # 单工厂小夹具：只含 F2（SKU2 已在上个批次落库 + 挂接）
    xlsx = TMP / "downstream_rerun.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    ws.append([F2, SKU2, "ITEM-2", 20])
    wb.save(xlsx)

    before = [(m.id, m.product_name_cn, m.unit_code) for m in _all_mappings()]
    before_links = [(l.mapping_id, l.sku_code) for l in _all_links()]

    r1 = service.run_until_interrupt(
        THREAD_RERUN, downstream_file_path=str(xlsx), upstream_root=str(TMP / "upstream"))
    assert r1["status"] == "pending_human_review", f"未挂起: {r1}"
    assert r1["review_data"]["factory_name"] == F2
    r2 = service.resume_order(THREAD_RERUN, {"approved": True, "items": []})
    assert r2["status"] == "success", f"批次未正常完成: {r2}"

    after = [(m.id, m.product_name_cn, m.unit_code) for m in _all_mappings()]
    after_links = [(l.mapping_id, l.sku_code) for l in _all_links()]
    assert after == before, f"老 SKU 重跑不得改动映射行: {before} → {after}"
    assert after_links == before_links, \
        f"老 SKU 重跑不得改动子表: {before_links} → {after_links}"
    print("[断言通过] 老 SKU 重跑批次：映射行与子表零变化")


def main():
    import pytest  # 脚本模式下提供 MonkeyPatch（pytest 运行时是注入的 fixture）
    test_unit_blank_name_no_action()
    test_unit_multi_row_picks_latest()
    test_unit_conflict_skip()
    with pytest.MonkeyPatch.context() as mp:
        test_e2e_auto_link_create_append_idempotent(mp)
    with pytest.MonkeyPatch.context() as mp:
        test_rerun_old_sku_no_mapping_action(mp)
    print("\nmapping_auto_link_test: PASS")


if __name__ == "__main__":
    main()
