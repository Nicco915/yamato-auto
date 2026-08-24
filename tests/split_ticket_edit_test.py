# -*- coding: utf-8 -*-
"""分票审核页「创建/删除票号」+ confirm 前覆盖校验（零容错）测试。

覆盖：
A. 纯函数（app.split.validate，不碰 DB）：
   1. 引擎产出的正常方案 → 无错误无警告；
   2. 删掉一票（柜行丢失）→ 硬错误指明遗漏柜；
   3. 一柜进两票 → 硬错误指明重复柜；
   4. 空票（items 空）→ 硬错误；
   5. 未知柜号 → 硬错误；
   6. 多商检柜「商检半票 + 非商检剩余票」组合覆盖完整 → 通过
      （与 fd2c1e7 口径对齐）；
   7. 一票混两种商检工厂 → 软警告（非硬错误）；
   8. renumber_tickets：票号按港口内数组顺序重编（与引擎规则 7 一致）。
B. 路由层（TestClient + 真实分票图，隔离临时库）：
   9. 正常 confirm → 200，落库 Declaration 票号连续（重编生效）；
   10. 缺票 confirm → 400，detail 指明遗漏柜；
   11. 空票 confirm → 400；force=true 也不过（硬错误）；
   12. 软警告无 force → 400 拒收；有 force → 200 放行。

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
state 造假方式：graph.update_state(as_node=propose_split) 注入
raw_items/sj_map/proposal，再 stream(None) 走到 human_review interrupt——
等价于跑完 load_filled/propose_split 挂起，绕开 filled Excel 读取。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/split_ticket_edit_test.py -v
"""
from __future__ import annotations

import copy
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
from sqlalchemy import select  # noqa: E402

from app.api.main import app  # noqa: E402
from app.db.models import Declaration  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.split.engine import propose  # noqa: E402
from app.split.graph import NODE2_SPLIT, get_split_graph  # noqa: E402
from app.split.schemas import RawItem  # noqa: E402
from app.split.validate import (  # noqa: E402
    renumber_tickets,
    validate_confirmed_proposal,
)

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_split_ticket_edit_test_")

client = TestClient(app)

# ---- 测试数据 ----

SJ_A = "商检厂A"
SJ_B = "商检厂B"
PLAIN_X = "普通厂X"
PLAIN_Y = "普通厂Y"

SJ_MAP = {SJ_A: True, SJ_B: True, PLAIN_X: False, PLAIN_Y: False}

PORT = "東京港"
CTYPE = "40HQ"


def _row(kanri: str, maker: str, sku: str) -> RawItem:
    """构造一行 RawItem（同港同箱型，重量/箱数从简）。"""
    return RawItem(
        kanri_no=kanri,
        port=PORT,
        container_type=CTYPE,
        maker=maker,
        sku=sku,
        net_weight=1.0,
        gross_weight=1.2,
        pcs=10,
    )


# K1：单商检 + 普通（整柜）；K2：纯普通（整柜）；
# K3：双商检 + 普通（2 商检半票 + 1 非商检剩余票）
ROWS = [
    _row("K1", SJ_A, "s1"),
    _row("K1", PLAIN_X, "s2"),
    _row("K2", PLAIN_Y, "s3"),
    _row("K3", SJ_A, "s4"),
    _row("K3", SJ_B, "s5"),
    _row("K3", PLAIN_X, "s6"),
]
RAW_DICTS = [r.model_dump() for r in ROWS]


def _engine_proposal() -> dict:
    """引擎产出的正常方案（dict），含 K3 的 2 商检半票 + 1 剩余票。"""
    p = propose(list(ROWS), dict(SJ_MAP))
    d = p.model_dump()
    assert len(d["ports"]) == 1
    assert len(d["ports"][0]["groups"]) == 4, (
        f"预期 4 票（1 整柜票 + 2 半票 + 1 剩余票），"
        f"实际 {len(d['ports'][0]['groups'])}"
    )
    return d


def _validate(proposal: dict):
    return validate_confirmed_proposal(proposal, RAW_DICTS, dict(SJ_MAP))


# ===================================================================
# A. 纯函数校验
# ===================================================================

def test_valid_proposal_passes():
    """引擎产出的正常方案（含剩余票组合）→ 无错误无警告。"""
    errors, warnings = _validate(_engine_proposal())
    assert errors == [], f"正常方案不应有硬错误: {errors}"
    assert warnings == [], f"正常方案不应有软警告: {warnings}"


def test_missing_ticket_rejected():
    """删掉剩余票（K3 非商检行丢失）→ 硬错误指明遗漏柜 K3。"""
    proposal = _engine_proposal()
    groups = proposal["ports"][0]["groups"]
    # 找到含 factory_exclude 的剩余票并删除
    remainder_idx = next(
        i for i, t in enumerate(groups)
        if any(it.get("factory_exclude") for it in t["items"])
    )
    groups.pop(remainder_idx)

    errors, _ = _validate(proposal)
    assert errors, "删票后未检出遗漏"
    assert any("K3" in e and "遗漏" in e for e in errors), f"错误未指明 K3 遗漏: {errors}"


def test_duplicate_container_rejected():
    """同一整柜进两张票 → 硬错误指明重复柜 K2。"""
    proposal = _engine_proposal()
    groups = proposal["ports"][0]["groups"]
    # K2 已在整柜票里，再把它（整柜）塞进第一张半票
    partial_t = next(
        t for t in groups
        if any(it.get("factory_filter") for it in t["items"])
    )
    partial_t["items"].append({
        "kanri_no": "K2", "factory_filter": None,
        "factory_exclude": None, "is_partial": False,
    })

    errors, _ = _validate(proposal)
    assert any("K2" in e and "重复" in e for e in errors), f"错误未指明 K2 重复: {errors}"


def test_empty_ticket_rejected():
    """新建的空票（items 空）未拖入柜 → 硬错误。"""
    proposal = _engine_proposal()
    proposal["ports"][0]["groups"].append({
        "ticket_no": "", "port": PORT, "container_type": "",
        "items": [], "sj_factories": [], "full_containers": 0, "warnings": [],
    })

    errors, _ = _validate(proposal)
    assert any("空票" in e for e in errors), f"未检出空票: {errors}"


def test_unknown_container_rejected():
    """票内含源数据中不存在的柜号 → 硬错误。"""
    proposal = _engine_proposal()
    proposal["ports"][0]["groups"][0]["items"].append({
        "kanri_no": "GHOST-9", "factory_filter": None,
        "factory_exclude": None, "is_partial": False,
    })

    errors, _ = _validate(proposal)
    assert any("GHOST-9" in e and "不存在" in e for e in errors), (
        f"未检出未知柜号: {errors}"
    )


def test_remainder_partial_combo_full_coverage():
    """商检半票 + 非商检剩余票组合覆盖完整 → 通过（fd2c1e7 口径回归）。"""
    proposal = _engine_proposal()
    groups = proposal["ports"][0]["groups"]
    k3_tickets = [
        t for t in groups
        if any(it["kanri_no"] == "K3" for it in t["items"])
    ]
    assert len(k3_tickets) == 3, f"K3 应拆成 3 票，实际 {len(k3_tickets)}"
    errors, warnings = _validate(proposal)
    assert errors == [], f"剩余票组合应覆盖完整: {errors}"


def test_mixed_sj_is_soft_warning():
    """一票混两种商检工厂 → 软警告（非硬错误），force 可放行。"""
    proposal = {
        "ports": [{
            "port": PORT,
            "groups": [
                {  # K1(SJ_A) + K3(SJ_A+SJ_B+普通) 整柜 → 混商检
                    "ticket_no": f"{PORT}-01", "port": PORT,
                    "container_type": CTYPE,
                    "items": [
                        {"kanri_no": "K1", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                        {"kanri_no": "K3", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                    ],
                    "sj_factories": [SJ_A, SJ_B], "full_containers": 2,
                    "warnings": [],
                },
                {  # K2 整柜
                    "ticket_no": f"{PORT}-02", "port": PORT,
                    "container_type": CTYPE,
                    "items": [
                        {"kanri_no": "K2", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                    ],
                    "sj_factories": [], "full_containers": 1, "warnings": [],
                },
            ],
        }],
    }
    errors, warnings = _validate(proposal)
    assert errors == [], f"混商检不应是硬错误: {errors}"
    assert any("商检" in w for w in warnings), f"缺 mixed_sj 软警告: {warnings}"


def test_renumber_tickets_pure():
    """票号重编：按港口内数组顺序 {port}-{i:02d}（与引擎规则 7 一致）。"""
    proposal = _engine_proposal()
    for t in proposal["ports"][0]["groups"]:
        t["ticket_no"] = "乱序-99"
    renumber_tickets(proposal)
    nos = [t["ticket_no"] for t in proposal["ports"][0]["groups"]]
    assert nos == [f"{PORT}-0{i}" for i in range(1, 5)]


# ===================================================================
# B. 路由层（TestClient + 真实分票图，隔离临时库）
# ===================================================================

_tid_seq = 0


def _seed_pending(proposal: dict) -> str:
    """造一个挂起在 human_review 的分票任务，返回 split_thread_id。"""
    global _tid_seq
    _tid_seq += 1
    tid = f"split-TESTEDIT-{_tid_seq}"
    graph = get_split_graph()
    cfg = {"configurable": {"thread_id": tid}}
    graph.update_state(cfg, {
        "split_thread_id": tid,
        "batch_id": tid.removeprefix("split-"),
        "source_file_path": "fake.xlsx",
        "raw_items": copy.deepcopy(RAW_DICTS),
        "sj_map": dict(SJ_MAP),
        "proposal": proposal,
        "status": "pending_review",
    }, as_node=NODE2_SPLIT)
    # 继续走到 human_review interrupt 挂起
    for _ in graph.stream(None, cfg, stream_mode="updates"):
        pass
    return tid


def _confirm(tid: str, proposal: dict, force: bool = False):
    return client.post(
        f"/api/v1/split/{tid}/confirm",
        json={"proposal": proposal, "force": force},
    )


def test_router_confirm_ok_and_renumbered():
    """正常 confirm → 200；票号被打乱也由后端重编，落库票号连续。"""
    proposal = _engine_proposal()
    for t in proposal["ports"][0]["groups"]:
        t["ticket_no"] = "乱序-99"
    tid = _seed_pending(proposal)

    r = _confirm(tid, proposal)
    assert r.status_code == 200, f"confirm 失败: {r.status_code} {r.text}"

    with get_session() as s:
        decls = s.scalars(
            select(Declaration)
            .where(Declaration.split_thread_id == tid)
            .order_by(Declaration.id)
        ).all()
    nos = [d.ticket_no for d in decls]
    assert nos == [f"{PORT}-0{i}" for i in range(1, 5)], (
        f"落库票号不连续（后端重编未生效）: {nos}"
    )
    assert all(d.status == "confirmed" for d in decls)


def test_router_confirm_missing_ticket_400():
    """删掉剩余票 confirm → 400，detail 指明遗漏柜 K3。"""
    proposal = _engine_proposal()
    groups = proposal["ports"][0]["groups"]
    remainder_idx = next(
        i for i, t in enumerate(groups)
        if any(it.get("factory_exclude") for it in t["items"])
    )
    groups.pop(remainder_idx)
    tid = _seed_pending(proposal)

    r = _confirm(tid, proposal)
    assert r.status_code == 400, f"应 400，实际 {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    assert "K3" in detail and "遗漏" in detail, f"detail 未指明遗漏柜: {detail}"


def test_router_confirm_empty_ticket_400_even_with_force():
    """空票 confirm → 400（硬错误，force=true 也不过）。"""
    proposal = _engine_proposal()
    proposal["ports"][0]["groups"].append({
        "ticket_no": "", "port": PORT, "container_type": "",
        "items": [], "sj_factories": [], "full_containers": 0, "warnings": [],
    })
    tid = _seed_pending(proposal)

    r = _confirm(tid, proposal, force=True)
    assert r.status_code == 400, f"硬错误 force 也不应放行: {r.status_code}"
    assert "空票" in r.json()["detail"]


def _mixed_sj_proposal() -> dict:
    """覆盖完整但一票混两种商检工厂的方案（软警告）。"""
    return {
        "ports": [{
            "port": PORT,
            "groups": [
                {
                    "ticket_no": f"{PORT}-01", "port": PORT,
                    "container_type": CTYPE,
                    "items": [
                        {"kanri_no": "K1", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                        {"kanri_no": "K3", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                    ],
                    "sj_factories": [SJ_A, SJ_B], "full_containers": 2,
                    "warnings": [],
                },
                {
                    "ticket_no": f"{PORT}-02", "port": PORT,
                    "container_type": CTYPE,
                    "items": [
                        {"kanri_no": "K2", "factory_filter": None,
                         "factory_exclude": None, "is_partial": False},
                    ],
                    "sj_factories": [], "full_containers": 1, "warnings": [],
                },
            ],
        }],
    }


def test_router_soft_warning_needs_force():
    """软警告（混商检）：无 force → 400 拒收；有 force → 200 放行。"""
    tid = _seed_pending(_mixed_sj_proposal())

    r1 = _confirm(tid, _mixed_sj_proposal(), force=False)
    assert r1.status_code == 400, f"软警告无 force 应拒收: {r1.status_code}"
    assert "强制确认" in r1.json()["detail"], f"detail 应提示强制确认: {r1.text}"

    # 被拒后任务仍挂起，可带 force 再次 confirm
    r2 = _confirm(tid, _mixed_sj_proposal(), force=True)
    assert r2.status_code == 200, f"force 应放行: {r2.status_code} {r2.text}"

    with get_session() as s:
        decls = s.scalars(
            select(Declaration).where(Declaration.split_thread_id == tid)
        ).all()
    assert decls and all(d.force_confirmed for d in decls), (
        "force 确认落库应标记 force_confirmed"
    )
