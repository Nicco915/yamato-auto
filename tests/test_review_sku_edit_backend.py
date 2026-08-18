# -*- coding: utf-8 -*-
"""批次3 Step3.1：B（SKU 可编辑后端）+ D2（强制重提后端）单测。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_review_sku_edit_backend.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。

覆盖：
B（human_review._merge_human_items）：
- 改名合并：orig_sku→新 sku，base 数据保留 + base["sku"]=新 sku
- 旧键不残留：merged_items 里旧 sku 只出现零次（改名后不再出现）
- 重复拒绝：改成已存在的 sku → Error + error_msg，保留 orig_sku
- 非 13 位拒绝：sku="ABC" → Error + error_msg，保留 orig_sku
- 改名后 is_new_sku 判定（主库查不到 → True；查到 → 刷新 db_record）
- returned_skus 按 orig_sku：改名项的旧 sku 不被"未返回"逻辑重复追加
- D3 空卡路径：orig_sku 不在 original 里，13 位合法 → 正常新增 + 补 sku 键
- 空 sku 卡（无条码条目）不强制 13 位
reopen 审计（service._prepare_audit_changes_from_items）：
- 改名项显式记 sku change（旧→新），数值 diff 与 orig 项比对
D2（POST /reextract → service.reextract_document）：
- 白名单外路径 → 403；白名单内不存在 → 404
- mock _route_extract 返回 2 item → review item 形状（calculation/is_new_sku/db_record）
- 非法 sku（非 13 位）条目仍返回 + needs_human_review=True + review_reason 注明
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_sku_edit_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")

from fastapi import HTTPException  # noqa: E402

from app.extraction.schemas import ExtractedItem  # noqa: E402
from app.nodes import human_review as hr  # noqa: E402
from app.api import service  # noqa: E402
from app.review import router as review_router  # noqa: E402
from app.review.router import reextract_document as reextract_endpoint  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

isolate_to_tmp("yamato_sku_edit_test_")

FACTORY = "测试厂"
OLD = "4900000000001"
OTHER = "4900000000002"
NEW = "4900000000099"


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def _calc_item(sku, qty=10, net=20.0, gross=25.0, **kw):
    item = {
        "sku": sku,
        "extracted_data": {
            "total_quantity": qty,
            "total_net_weight": net,
            "total_gross_weight": gross,
            "weight_unit": "KG",
            "source_file": "/src/a.xlsx",
            "sku_name": "品名",
            "review_reason": None,
        },
        "calculation": {
            "net_formula": f"{net} / {qty}",
            "gross_formula": f"{gross} / {qty}",
            "calculated_unit_net": net / qty,
            "calculated_unit_gross": gross / qty,
        },
        "status": "Normal",
        "error_msg": None,
        "is_human_edited": False,
        "is_new_sku": False,
        "db_record": {"name_cn": "品名", "hs_code": "1234"},
    }
    item.update(kw)
    return item


def _h_item(sku, orig_sku=None, qty=10, net=20.0, gross=25.0, **kw):
    h = {
        "sku": sku,
        "extracted_data": {
            "total_quantity": qty,
            "total_net_weight": net,
            "total_gross_weight": gross,
        },
    }
    if orig_sku is not None:
        h["orig_sku"] = orig_sku
    h.update(kw)
    return h


@pytest.fixture
def lookup_not_found(monkeypatch):
    """改名重查主库：一律查不到（→ is_new_sku=True）。"""
    monkeypatch.setattr(hr, "_lookup_sku_record", lambda f, s: None)


# ---------------------------------------------------------------------------
# B：SKU 可编辑后端
# ---------------------------------------------------------------------------

def test_rename_merge_preserves_base_and_sets_sku(lookup_not_found, monkeypatch):
    record = {"name_cn": "新品名", "hs_code": "9999", "inspection_required": True}
    monkeypatch.setattr(hr, "_lookup_sku_record", lambda f, s: record)
    originals = [_calc_item(OLD), _calc_item(OTHER)]
    human = [_h_item(NEW, orig_sku=OLD), _h_item(OTHER)]

    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    by_sku = {i["sku"]: i for i in merged}

    m = by_sku[NEW]
    assert m["extracted_data"]["total_quantity"] == 10      # base 数据保留
    assert m["extracted_data"]["source_file"] == "/src/a.xlsx"
    assert m["sku"] == NEW                                   # 显式补键
    assert m["sku_renamed_from"] == OLD                      # 审计留痕
    assert m["is_human_edited"] is True                      # 改名视为编辑
    assert m["is_new_sku"] is False                          # 查到主库记录
    assert m["db_record"] == record                          # db_record 刷新
    assert m["status"] == "Normal"
    assert OTHER in by_sku


def test_rename_old_key_not_residue(lookup_not_found):
    originals = [_calc_item(OLD), _calc_item(OTHER)]
    human = [_h_item(NEW, orig_sku=OLD), _h_item(OTHER)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    skus = [i["sku"] for i in merged]
    assert OLD not in skus                # 旧键不残留
    assert skus.count(NEW) == 1
    assert skus.count(OTHER) == 1
    assert len(merged) == 2


def test_rename_to_existing_sku_rejected(lookup_not_found):
    originals = [_calc_item(OLD), _calc_item(OTHER)]
    human = [_h_item(OTHER, orig_sku=OLD), _h_item(OTHER)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    skus = [i["sku"] for i in merged]

    assert skus.count(OTHER) == 1         # 改名被拒：OTHER 仍是原卡那一份
    assert skus.count(OLD) == 1           # 被拒卡保留 orig_sku
    assert len(merged) == 2
    bad = [i for i in merged if i.get("status") == "Error"]
    assert len(bad) == 1
    assert bad[0]["sku"] == OLD           # 保留 orig_sku，不用新 sku 覆盖
    assert bad[0]["error_msg"] == "人工改名后 SKU 重复"
    assert "sku_renamed_from" not in bad[0]


def test_rename_to_same_sku_claimed_in_batch_rejected(lookup_not_found):
    """两张卡同时改成同一个新 sku → 后到的判重复。"""
    originals = [_calc_item(OLD), _calc_item(OTHER)]
    human = [_h_item(NEW, orig_sku=OLD), _h_item(NEW, orig_sku=OTHER)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    errors = [i for i in merged if i.get("status") == "Error"]
    assert len(errors) == 1
    assert errors[0]["sku"] == OTHER      # 第二张卡被拒，保留其 orig_sku
    assert errors[0]["error_msg"] == "人工改名后 SKU 重复"
    assert [i["sku"] for i in merged].count(NEW) == 1


def test_non_13_digit_sku_rejected(lookup_not_found):
    originals = [_calc_item(OLD)]
    human = [_h_item("ABC", orig_sku=OLD)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)

    assert len(merged) == 1
    m = merged[0]
    assert m["sku"] == OLD                # 保留 orig_sku
    assert m["status"] == "Error"
    assert m["error_msg"] == "SKU 必须是 13 位数字"


def test_rename_to_new_sku_marks_is_new(lookup_not_found):
    originals = [_calc_item(OLD)]
    human = [_h_item(NEW, orig_sku=OLD)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    m = merged[0]
    assert m["sku"] == NEW
    assert m["is_new_sku"] is True        # 主库查不到 → 新 SKU 合规字段路径
    assert m["db_record"] == {}
    assert m["sku_renamed_from"] == OLD


def test_returned_skus_judged_by_orig_sku(lookup_not_found):
    """改名卡提交后，未返回的 OTHER 原样保留；旧 sku 不被误判未返回而重复追加。"""
    originals = [_calc_item(OLD), _calc_item(OTHER)]
    human = [_h_item(NEW, orig_sku=OLD)]  # OTHER 未返回
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    skus = [i["sku"] for i in merged]
    assert skus.count(OLD) == 0
    assert skus.count(NEW) == 1
    assert skus.count(OTHER) == 1         # 未返回的原样保留一次
    assert len(merged) == 2


def test_d3_empty_card_new_sku_added(lookup_not_found):
    """D3 空卡：orig_sku 不在 original 里（全新卡），13 位合法 → 正常新增 + 补 sku 键。"""
    originals = [_calc_item(OLD)]
    human = [
        _h_item(OLD),
        _h_item(NEW, qty=None, net=None, gross=None),  # 空卡，数值留空人工后填
    ]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    by_sku = {i["sku"]: i for i in merged}
    assert set(by_sku) == {OLD, NEW}
    m = by_sku[NEW]
    assert m["sku"] == NEW                # 补 sku 键（修 writer 丢数据隐患）
    assert m["is_new_sku"] is True
    assert "sku_renamed_from" not in m    # 非改名（无 orig_sku）
    assert m["status"] != "Error" or m["error_msg"] != "SKU 必须是 13 位数字"


def test_empty_sku_card_not_forced_13_digit():
    """空 sku 卡（无条码条目）不强制 13 位校验。"""
    originals = [_calc_item(OLD)]
    human = [_h_item(OLD), _h_item(None, qty=1, net=1.0, gross=2.0)]
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    empty_card = [i for i in merged if not i.get("sku")]
    assert len(empty_card) == 1
    assert empty_card[0]["error_msg"] != "SKU 必须是 13 位数字"
    assert empty_card[0]["status"] == "Normal"


def test_no_rename_existing_behavior_unchanged():
    """无改名回归：数值修改 + 单重重算（原逻辑不变）。"""
    originals = [_calc_item(OLD)]
    human = [_h_item(OLD, qty=5)]  # 只改件数
    merged = hr._merge_human_items(human, originals, approved=True, factory_name=FACTORY)
    m = merged[0]
    assert m["sku"] == OLD
    assert m["is_human_edited"] is True
    assert m["extracted_data"]["total_quantity"] == 5
    assert m["calculation"]["calculated_unit_net"] == 20.0 / 5  # Node5 纯 Python 重算
    assert "sku_renamed_from" not in m


# ---------------------------------------------------------------------------
# reopen 审计：改名 change 显式留痕
# ---------------------------------------------------------------------------

def test_audit_changes_record_sku_rename():
    old_items = [_calc_item(OLD, qty=10)]
    new_items = [_h_item(NEW, orig_sku=OLD, qty=99)]
    changes = service._prepare_audit_changes_from_items(new_items, old_items)

    rename = [c for c in changes if c["field"] == "sku"]
    assert rename == [{"sku": NEW, "field": "sku", "old": OLD, "new": NEW}]
    # 数值 diff 与 orig 项比对（10 → 99），不落入"找不到对应项"静默分支
    qty_change = [c for c in changes if c["field"] == "total_quantity"]
    assert qty_change == [{"sku": NEW, "field": "total_quantity", "old": 10, "new": 99}]


# ---------------------------------------------------------------------------
# D2：重新识别这个文件
# ---------------------------------------------------------------------------

def _extracted(sku_code, qty=10, net=20.0, gross=25.0, **kw):
    return ExtractedItem(
        sku_code=sku_code, sku_name="品名", total_quantity=qty,
        total_net_weight=net, total_gross_weight=gross, **kw,
    )


@pytest.fixture
def whitelisted_file():
    """临时白名单目录 + 一个真实文件；显式配置白名单（不自动刷新）。"""
    d = Path(tempfile.mkdtemp(prefix="yamato_reextract_wl_"))
    f = d / "packing.xlsx"
    f.write_bytes(b"fake")
    review_router.configure_review([str(d)])
    yield f


def _mock_route_extract(monkeypatch, items):
    from app.extraction import agent as agent_mod
    monkeypatch.setattr(
        agent_mod, "_route_extract",
        lambda path, **kw: SimpleNamespace(items=items, error="", notes=[]),
    )


def test_reextract_forbidden_path_403(whitelisted_file):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(reextract_endpoint(
            "t1", {"path": "/etc/hosts", "factory_name": FACTORY}))
    assert ei.value.status_code == 403


def test_reextract_missing_file_404(whitelisted_file):
    missing = str(whitelisted_file.parent / "nope.xlsx")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(reextract_endpoint(
            "t1", {"path": missing, "factory_name": FACTORY}))
    assert ei.value.status_code == 404


def test_reextract_placeholder_path_400(whitelisted_file):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(reextract_endpoint(
            "t1", {"path": "no_items_extracted", "factory_name": FACTORY}))
    assert ei.value.status_code == 400


def test_reextract_returns_review_item_shape(whitelisted_file, monkeypatch):
    _mock_route_extract(monkeypatch, [
        _extracted("1234567890123"),
        _extracted("2234567890123", qty=4, net=8.0, gross=10.0),
    ])
    record = {"name_cn": "库中品名", "name_en": None, "name_jp": None,
              "hs_code": "1234.56", "inspection_required": False,
              "unit_net_weight": 2.0, "unit_gross_weight": 2.5}
    monkeypatch.setattr(service, "_load_factory_db_records",
                        lambda factory: {"1234567890123": record})

    result = asyncio.run(reextract_endpoint(
        "t1", {"path": str(whitelisted_file), "factory_name": FACTORY}))

    resolved = str(whitelisted_file.resolve())  # macOS /var→/private/var 符号链接
    assert result["source_file"] == resolved
    items = result["items"]
    assert len(items) == 2
    by_sku = {i["sku"]: i for i in items}

    old = by_sku["1234567890123"]         # 主库命中 → 老 SKU
    assert old["is_new_sku"] is False
    assert old["db_record"] == record
    assert old["name_cn"] == "库中品名"    # 老 SKU 顶层字段（build_review_items_payload 同构）
    assert old["calculation"]["calculated_unit_net"] == 20.0 / 10
    assert old["status"] == "Normal"
    assert old["needs_human_review"] is False
    assert old["extracted_data"]["source_file"] == resolved

    new = by_sku["2234567890123"]         # 主库查不到 → 新 SKU
    assert new["is_new_sku"] is True
    assert new["db_record"] == {}
    assert new["fields_to_fill"] == ["name_cn", "hs_code", "inspection_required"]
    assert new["calculation"]["calculated_unit_gross"] == 10.0 / 4

    # 与 build_review_payload items 元素同构的核心字段集
    core = {"sku", "extracted_data", "calculation", "status", "is_human_edited",
            "is_new_sku", "db_record", "error_msg", "unexpected_sku",
            "needs_human_review"}
    assert core <= set(old) and core <= set(new)


def test_reextract_invalid_sku_flagged_but_returned(whitelisted_file, monkeypatch):
    """非 13 位 sku 条目信息零丢失：仍返回 + needs_human_review=True + reason 注明。"""
    _mock_route_extract(monkeypatch, [_extracted("ABC123")])
    monkeypatch.setattr(service, "_load_factory_db_records", lambda factory: {})

    result = asyncio.run(reextract_endpoint(
        "t1", {"path": str(whitelisted_file), "factory_name": FACTORY}))
    items = result["items"]
    assert len(items) == 1
    m = items[0]
    assert m["sku"] == "ABC123"           # 非法值保留返回（与 compute_align 口径一致）
    assert m["needs_human_review"] is True
    assert "SKU_NON_13_DIGIT" in (m["extracted_data"]["review_reason"] or "")
    assert m["status"] == "Needs_Review"


def test_reextract_channel_error_400(whitelisted_file, monkeypatch):
    from app.extraction import agent as agent_mod

    def boom(path, **kw):
        raise RuntimeError("通道炸了")
    monkeypatch.setattr(agent_mod, "_route_extract", boom)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(reextract_endpoint(
            "t1", {"path": str(whitelisted_file), "factory_name": FACTORY}))
    assert ei.value.status_code == 400
    assert "通道炸了" in ei.value.detail
