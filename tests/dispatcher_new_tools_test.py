# -*- coding: utf-8 -*-
"""调度 Agent 新工具测试：create_factory_alias / add_factories / query_master_data
+ get_batch_status 的 unprocessed_factories 派生。

覆盖：
- create_factory_alias：成功落库（factory_aliases 新行 + short_name 回填）；
  folder 非法/不存在 → error；short_name 冲突 → error；幂等重复调用
- add_factories：mock service 层——成功（返回补入名单+状态）、
  无待补充、批次不存在（ValueError）、运行中（RuntimeError）透传
- query_master_data：SKU 命中产品映射 + 工厂 SKU 表；工厂名/别名模糊命中；
  无命中返回提示
- get_batch_status：monkeypatch service 两层，验证 unprocessed_factories
  = downstream_requirements ∩ filter − pending − current − factory_outputs

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_new_tools_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from sqlalchemy import select  # noqa: E402

from app.db.models import (  # noqa: E402
    Factory,
    FactoryAlias,
    FactorySKU,
    ProductMapping,
    ProductMappingSku,
)
from app.db.session import get_session  # noqa: E402
# 血泪红线：dispatcher.tools 的 import 链（service→…→llm_client）会执行
# load_dotenv(override=True)，必须在 isolate_to_tmp 之前完成全部 app 模块
# import，否则隔离 env 会被打回真实路径（本测试曾因此污染生产 master.db）
from app.dispatcher import tools as dispatcher_tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 假 upstream_root：含两个一级子目录（供 validate_subfolder 校验）
# 目录必须先建好，再经 extra_env 随隔离一并设置 + cache_clear
_TMP_FOR_UPSTREAM = Path(__file__).resolve().parent  # 占位，真正 tmp 由隔离创建
import tempfile as _tempfile  # noqa: E402

_UPSTREAM_BASE = Path(_tempfile.mkdtemp(prefix="yamato_new_tools_upstream_"))
(_UPSTREAM_BASE / "中地36").mkdir(parents=True)
(_UPSTREAM_BASE / "正达").mkdir(parents=True)
(_UPSTREAM_BASE / "测试文件夹C").mkdir(parents=True)
UPSTREAM = _UPSTREAM_BASE

TMP = isolate_to_tmp("yamato_dispatcher_new_tools_",
                     extra_env={"UPSTREAM_ROOT": str(UPSTREAM)})

from app.config import get_settings  # noqa: E402
assert get_settings().upstream_root == str(UPSTREAM)


# ---------------------------------------------------------------------------
# create_factory_alias
# ---------------------------------------------------------------------------

def test_create_factory_alias_success():
    """正常路径：落 factory_aliases + 回填 short_name，返回自然语言结果。"""
    r = dispatcher_tools._exec_create_factory_alias(
        {"factory": "山東中地テスト", "folder": "中地36"})
    assert r.get("ok"), f"应成功: {r}"
    assert "中地36" in r["message"]
    with get_session() as sess:
        fac = sess.scalar(select(Factory).where(
            Factory.factory_name == "山東中地テスト"))
        assert fac is not None and fac.short_name == "中地36"
        alias = sess.scalar(select(FactoryAlias).where(
            FactoryAlias.alias == "山東中地テスト"))
        assert alias is not None and alias.use_folder_match is True


def test_create_factory_alias_invalid_folder():
    """folder 不存在/含路径穿越 → error，不落库。"""
    r = dispatcher_tools._exec_create_factory_alias(
        {"factory": "某工厂", "folder": "不存在目录"})
    assert "error" in r
    r2 = dispatcher_tools._exec_create_factory_alias(
        {"factory": "某工厂", "folder": "../upstream"})
    assert "error" in r2
    with get_session() as sess:
        assert sess.scalar(select(FactoryAlias).where(
            FactoryAlias.alias == "某工厂")) is None


def test_create_factory_alias_conflict():
    """工厂已有不一致 short_name → error（提示去主数据页），不落别名。"""
    with get_session() as sess:
        sess.add(Factory(factory_name="冲突工厂", short_name="正达"))
        sess.commit()
    r = dispatcher_tools._exec_create_factory_alias(
        {"factory": "冲突工厂", "folder": "中地36"})
    assert "error" in r
    assert "正达" in r["error"]


def test_create_factory_alias_idempotent():
    """重复调用幂等：不报错，别名行不重复。
    用独占文件夹「测试文件夹C」，避免与其他用例在同库下产生 short_name 冲突。"""
    args = {"factory": "幂等工厂", "folder": "测试文件夹C"}
    r1 = dispatcher_tools._exec_create_factory_alias(args)
    r2 = dispatcher_tools._exec_create_factory_alias(args)
    assert r1.get("ok") and r2.get("ok")
    with get_session() as sess:
        rows = sess.scalars(select(FactoryAlias).where(
            FactoryAlias.alias == "幂等工厂")).all()
        assert len(rows) == 1


def test_create_factory_alias_preview_warns_on_bad_folder():
    """preview 对非法 folder 提前进 warnings，不抛异常。"""
    p = dispatcher_tools._preview_create_factory_alias(
        {"factory": "X", "folder": "不存在目录"})
    assert p["warnings"], "preview 应携带校验警告"
    p2 = dispatcher_tools._preview_create_factory_alias(
        {"factory": "X", "folder": "中地36"})
    assert p2["warnings"] == []


# ---------------------------------------------------------------------------
# add_factories（mock service 层）
# ---------------------------------------------------------------------------

def test_add_factories_success(monkeypatch):
    """成功：返回补入名单 + 挂起提示。"""
    def fake_add(batch_id, on_progress=None):
        assert batch_id == "B-1"
        return {"added": 2, "factories": ["中地", "正达"],
                "status": "pending_human_review", "thread_id": "B-1"}
    monkeypatch.setattr(dispatcher_tools.service, "add_factories_to_batch",
                        fake_add)
    r = dispatcher_tools._exec_add_factories({"thread_id": "B-1"})
    assert r.get("ok")
    assert "中地" in r["message"] and "审核" in r["message"]
    assert r["factories"] == ["中地", "正达"]


def test_add_factories_nothing_to_add(monkeypatch):
    monkeypatch.setattr(
        dispatcher_tools.service, "add_factories_to_batch",
        lambda batch_id, on_progress=None: {
            "added": 0, "factories": [], "message": "没有待补充的工厂"})
    r = dispatcher_tools._exec_add_factories({"thread_id": "B-1"})
    assert r.get("ok") and "没有待补充" in r["message"]


def test_add_factories_error_passthrough(monkeypatch):
    """批次不存在（ValueError）/运行中（RuntimeError）如实透传为 error。"""
    def raise_value(batch_id, on_progress=None):
        raise ValueError("批次不存在: B-X")
    monkeypatch.setattr(dispatcher_tools.service, "add_factories_to_batch",
                        raise_value)
    r = dispatcher_tools._exec_add_factories({"thread_id": "B-X"})
    assert "批次不存在" in r["error"]

    def raise_running(batch_id, on_progress=None):
        raise RuntimeError("批次 B-R 正在运行中")
    monkeypatch.setattr(dispatcher_tools.service, "add_factories_to_batch",
                        raise_running)
    r2 = dispatcher_tools._exec_add_factories({"thread_id": "B-R"})
    assert "正在运行中" in r2["error"]


def test_add_factories_preview_unknown_batch():
    """preview 对不存在批次给 warning（真实 service.get_order_state 查临时库）。"""
    p = dispatcher_tools._preview_add_factories({"thread_id": "不存在批次"})
    assert any("不存在" in w for w in p["warnings"])


# ---------------------------------------------------------------------------
# query_master_data
# ---------------------------------------------------------------------------

def _seed_master_data():
    """造主数据（幂等：已存在则直接复用，供同库多个用例重复调用）。"""
    with get_session() as sess:
        fac = sess.scalar(select(Factory).where(Factory.factory_name == "山東中地"))
        if fac is None:
            fac = Factory(factory_name="山東中地", short_name="中地")
            sess.add(fac)
            sess.flush()
            sess.add(FactorySKU(
                factory_id=fac.factory_id, sku_code="4549509518860",
                name_cn="坐垫", name_en="CUSHION", hs_code="9404904000",
                inspection_required=False,
                unit_net_weight=4.9, unit_gross_weight=7.4))
            sess.add(FactoryAlias(factory_id=fac.factory_id, alias="中地36",
                                  use_folder_match=True))
        m = sess.scalar(select(ProductMapping).where(
            ProductMapping.product_name_cn == "坐垫"))
        if m is None:
            m = ProductMapping(product_name_cn="坐垫", hs_code="9404904000",
                               inspection_required=False, name_en="CUSHION")
            sess.add(m)
            sess.flush()
            sess.add(ProductMappingSku(mapping_id=m.id,
                                       sku_code="4549509518860"))
        sess.commit()


def test_query_master_data_by_sku():
    """SKU 精确查：产品映射 + 工厂 SKU 两路命中。"""
    _seed_master_data()
    r = dispatcher_tools._fn_query_master_data({"query": "4549509518860"})
    assert not r.get("error"), r
    sources = {m["来源"] for m in r["sku_matches"]}
    assert "产品映射" in sources and "工厂SKU主数据" in sources
    sku_row = next(m for m in r["sku_matches"] if m["来源"] == "工厂SKU主数据")
    assert sku_row["工厂"] == "山東中地"
    assert sku_row["税号"] == "9404904000"
    assert sku_row["单件净重"] == 4.9


def test_query_master_data_by_factory_and_alias():
    """工厂名模糊 + 别名命中，含别名列表与 SKU 数。
    同库其他用例也创建了含「中地」的工厂（山東中地テスト），
    故断言目标工厂在结果中且字段正确，而非结果唯一。"""
    _seed_master_data()
    r = dispatcher_tools._fn_query_master_data({"query": "中地"})
    matches = {m["工厂名"]: m for m in r["factory_matches"]}
    assert "山東中地" in matches
    m = matches["山東中地"]
    assert "中地36" in m["别名"]
    assert m["主数据SKU数"] == 1


def test_query_master_data_no_match():
    """无命中 → 自然语言提示，不报错。"""
    r = dispatcher_tools._fn_query_master_data({"query": "不存在的XYZ123"})
    assert not r.get("error")
    assert r["sku_matches"] == [] and r["factory_matches"] == []
    assert "未找到" in r["message"]


# ---------------------------------------------------------------------------
# get_batch_status 的 unprocessed_factories
# ---------------------------------------------------------------------------

def test_get_batch_status_unprocessed(monkeypatch):
    """unprocessed = requirements ∩ filter − pending − current − factory_outputs。"""
    monkeypatch.setattr(
        dispatcher_tools.service, "list_batches",
        lambda: {"batches": [{"thread_id": "B-9", "status": "completed"}]})
    monkeypatch.setattr(
        dispatcher_tools.service, "get_order_state",
        lambda tid: {
            "exists": True,
            "next_nodes": [],
            "values": {
                "downstream_requirements": {"A": 1, "B": 1, "C": 1, "D": 1},
                "factory_filter": ["A", "B", "C"],  # D 被过滤
                "pending_factories": ["C"],
                "current_factory_data": {"factory_name": "B"},
                "factory_outputs": {"A": {}},
            },
        })
    r = dispatcher_tools._fn_get_batch_status({"thread_id": "B-9"})
    assert r["unprocessed_factories"] == [], (
        f"A 已写入、B 当前、C 待处理、D 被过滤，应无未处理: {r}")

    # B 完成且 C 被跳过（不在 pending/outputs）→ C 应出现在 unprocessed
    monkeypatch.setattr(
        dispatcher_tools.service, "get_order_state",
        lambda tid: {
            "exists": True,
            "next_nodes": [],
            "values": {
                "downstream_requirements": {"A": 1, "B": 1, "C": 1},
                "pending_factories": [],
                "current_factory_data": None,
                "factory_outputs": {"A": {}, "B": {}},
            },
        })
    r2 = dispatcher_tools._fn_get_batch_status({"thread_id": "B-9"})
    assert r2["unprocessed_factories"] == ["C"]


def test_get_batch_status_unknown_batch(monkeypatch):
    monkeypatch.setattr(dispatcher_tools.service, "list_batches",
                        lambda: {"batches": []})
    r = dispatcher_tools._fn_get_batch_status({"thread_id": "无"})
    assert "error" in r
