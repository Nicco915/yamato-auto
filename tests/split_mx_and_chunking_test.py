# -*- coding: utf-8 -*-
"""F3 分票规则调整测试：普通柜合票「先 2 后 3」+ MX 柜一厂一票。

覆盖：
A. 普通柜合票（Rule 6 改写）：
   1. 1-7 个兼容整柜的分组结构：1→(1)、2→(2)、3→(3)、4→(2,2)、
      5→(2,3)、6→(2,2,2)、7→(2,2,3)；
   2. 3 柜一票是合法规则，不再产生 over_3_full 警告；
   3. 商检冲突边界：兼容跑 5 柜遇异种商检厂 → (2,3)+(1)；
   4. ≥2 家商检厂柜（Rule 4）中断兼容跑，前后各按「先 2 后 3」成票；
B. MX 柜一厂一票：
   5. 3 厂 MX 柜 → 3 票（factory_filter=厂名、inspection_filter=None、
      is_partial=True、票号与普通票统一连续编号）；
   6. 1 厂 MX 柜 → 1 票（整柜该厂全包）；
   7. MX 柜与普通柜完全隔离：普通柜按「先 2 后 3」合票，MX 柜单独成票，
      任何票不混装 MX 柜与普通柜；
   8. MX 柜跳过商检判断：柜内多家商检厂也不拆商检半票；
   9. 混柜（MX 行+普通行）→ 整柜按 MX 逻辑处理 + mixed_mx 中文警告；
C. aggregator / validate 语义：
   10. rows_for_ticket：factory_filter=F + inspection_filter=None
       返回柜内该厂全部行（不区分商检）；
   11. validate_confirmed_proposal 接受 MX 一厂一票方案（覆盖完整）；
   12. 审核界面手动合并多张一厂一票为一票 → 校验通过（可 confirm）；
   13. 审核界面把 MX 柜改成整柜票 → 校验通过；
   14. 新合票逻辑的普通方案（5 柜）→ 校验零错误零警告。

纯函数单测（engine.propose / aggregator.rows_for_ticket /
validate.validate_confirmed_proposal），不碰 DB。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/split_mx_and_chunking_test.py -v

隔离（血泪红线）：import app 模块前设 YAMATO_DOTENV_PATH 指临时空 .env。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# ---- 隔离门：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP_ENV = Path(tempfile.mkdtemp(prefix="yamato_split_mx_test_env_")) / ".env"
_TMP_ENV.write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP_ENV)
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.declare.aggregator import rows_for_ticket  # noqa: E402
from app.split.engine import propose  # noqa: E402
from app.split.schemas import RawItem  # noqa: E402
from app.split.validate import validate_confirmed_proposal  # noqa: E402

# ---- 测试数据 ----

PORT = "東京港"
CTYPE = "40HQ"

SJ_A = "商检厂A"
SJ_B = "商检厂B"
PLAIN_X = "普通厂X"
PLAIN_Y = "普通厂Y"

MX_A = "MX厂甲"
MX_B = "MX厂乙"
MX_C = "MX厂丙"


def _row(kanri: str, maker: str, sku: str, *,
         inspection: bool = False, is_mx: bool = False) -> RawItem:
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
        inspection=inspection,
        is_mx=is_mx,
    )


def _plain_rows(n: int, prefix: str = "K", maker: str = PLAIN_X) -> list[RawItem]:
    """n 个纯普通整柜（每柜一行）。"""
    return [_row(f"{prefix}{i}", maker, f"{prefix}{i}-s1") for i in range(1, n + 1)]


def _tickets(proposal):
    """按顺序取出全部票。"""
    return [t for pg in proposal.ports for t in pg.groups]


def _whole_sizes(proposal) -> list[int]:
    """全部票的整柜数序列（半票为 0）。"""
    return [t.full_containers for t in _tickets(proposal)]


# ===========================================================================
# A. 普通柜合票「先 2 后 3」
# ===========================================================================

@pytest.mark.parametrize("n,expected", [
    (1, [1]),
    (2, [2]),
    (3, [3]),
    (4, [2, 2]),
    (5, [2, 3]),
    (6, [2, 2, 2]),
    (7, [2, 2, 3]),
])
def test_chunking_first2_then3(n, expected):
    """n 个兼容整柜 → 先 2 后 3 分组结构。"""
    p = propose(_plain_rows(n), {})
    assert _whole_sizes(p) == expected
    # 票号连续编号
    assert [t.ticket_no for t in _tickets(p)] == [
        f"{PORT}-{i:02d}" for i in range(1, len(expected) + 1)
    ]
    # 柜不丢不重
    kanris = [it.kanri_no for t in _tickets(p) for it in t.items]
    assert sorted(kanris) == [f"K{i}" for i in range(1, n + 1)]


def test_three_containers_one_ticket_no_over_3_full_warning():
    """3 柜一票是合法规则：不产生 over_3_full 等任何警告。"""
    p = propose(_plain_rows(3), {})
    tickets = _tickets(p)
    assert len(tickets) == 1
    assert tickets[0].full_containers == 3
    assert tickets[0].warnings == []


def test_sj_conflict_boundary_uses_new_chunking():
    """兼容跑 5 柜（同商检厂）遇异种商检厂 → (2,3) + (1)。"""
    rows = [_row(f"K{i}", SJ_A, f"s{i}", inspection=True) for i in range(1, 6)]
    rows.append(_row("K6", SJ_B, "s6", inspection=True))
    p = propose(rows, {})
    assert _whole_sizes(p) == [2, 3, 1]
    tickets = _tickets(p)
    assert [it.kanri_no for it in tickets[0].items] == ["K1", "K2"]
    assert [it.kanri_no for it in tickets[1].items] == ["K3", "K4", "K5"]
    assert [it.kanri_no for it in tickets[2].items] == ["K6"]


def test_multi_sj_container_interrupts_run():
    """≥2 家商检厂柜（Rule 4）中断兼容跑：前后整柜各按先 2 后 3 成票。"""
    rows = _plain_rows(2, prefix="A")                     # A1、A2 纯普通
    rows += [
        _row("M1", SJ_A, "m1", inspection=True),
        _row("M1", SJ_B, "m2", inspection=True),
    ]
    rows += _plain_rows(4, prefix="Z")                    # Z1-Z4 纯普通
    p = propose(rows, {})
    tickets = _tickets(p)
    # 柜号排序 A<M<Z：A 跑 2 柜一票；M1 拆 2 张商检半票；Z 跑 4 柜 → (2,2)
    assert [it.kanri_no for it in tickets[0].items] == ["A1", "A2"]
    assert not tickets[0].items[0].is_partial
    assert [it.factory_filter for it in tickets[1].items] == [SJ_A]
    assert tickets[1].items[0].inspection_filter is True
    assert [it.factory_filter for it in tickets[2].items] == [SJ_B]
    assert _whole_sizes(p) == [2, 0, 0, 2, 2]
    assert [it.kanri_no for it in tickets[3].items] == ["Z1", "Z2"]
    assert [it.kanri_no for it in tickets[4].items] == ["Z3", "Z4"]


# ===========================================================================
# B. MX 柜一厂一票
# ===========================================================================

def _mx_container_rows(kanri: str = "MX1") -> list[RawItem]:
    """3 厂 MX 柜（每厂一行 MX 行）。"""
    return [
        _row(kanri, MX_B, "mx-b1", is_mx=True),
        _row(kanri, MX_A, "mx-a1", is_mx=True),
        _row(kanri, MX_C, "mx-c1", is_mx=True),
    ]


def test_mx_container_three_factories_three_tickets():
    """3 厂 MX 柜 → 3 张一厂一票：factory_filter 正确、inspection_filter=None。"""
    p = propose(_mx_container_rows(), {})
    tickets = _tickets(p)
    assert len(tickets) == 3
    # 按厂名排序一厂一票（Python 字符串序）
    assert [t.items[0].factory_filter for t in tickets] == sorted(
        [MX_A, MX_B, MX_C])
    for t in tickets:
        assert len(t.items) == 1
        it = t.items[0]
        assert it.kanri_no == "MX1"
        assert it.is_partial is True
        assert it.inspection_filter is None  # 整厂全包不过滤商检
        assert it.factory_exclude is None
        assert t.full_containers == 0
        assert t.sj_factories == []          # MX 跳过商检判断
        assert t.warnings == []              # 纯 MX 柜无混柜警告
    # 票号统一按港口顺序编号
    assert [t.ticket_no for t in tickets] == [
        f"{PORT}-01", f"{PORT}-02", f"{PORT}-03",
    ]


def test_mx_container_single_factory_one_ticket():
    """1 厂 MX 柜（多行）→ 1 张一厂一票（整柜该厂全包）。"""
    rows = [
        _row("MX1", MX_A, "mx-a1", is_mx=True),
        _row("MX1", MX_A, "mx-a2", is_mx=True),
    ]
    p = propose(rows, {})
    tickets = _tickets(p)
    assert len(tickets) == 1
    assert tickets[0].items[0].factory_filter == MX_A
    assert tickets[0].items[0].inspection_filter is None


def test_mx_isolated_from_plain_containers():
    """MX 柜与普通柜完全隔离：普通柜先 2 后 3 合票，MX 柜单独成票。"""
    rows = _plain_rows(4)                                # K1-K4 普通
    rows += [_row("MX1", MX_A, "mx-a1", is_mx=True)]     # 1 厂 MX 柜
    p = propose(rows, {})
    tickets = _tickets(p)
    assert _whole_sizes(p) == [2, 2, 0]
    # 普通票不含 MX 柜，MX 票不含普通柜
    plain_kanris = {
        it.kanri_no for t in tickets if t.full_containers > 0
        for it in t.items
    }
    assert plain_kanris == {"K1", "K2", "K3", "K4"}
    mx_t = tickets[2]
    assert [it.kanri_no for it in mx_t.items] == ["MX1"]
    assert mx_t.items[0].factory_filter == MX_A


def test_mx_container_skips_inspection_split():
    """MX 柜跳过商检判断：多家商检厂也不拆商检半票，仍一厂一票整厂全包。"""
    rows = [
        _row("MX1", SJ_A, "mx-1", inspection=True, is_mx=True),
        _row("MX1", SJ_B, "mx-2", inspection=True, is_mx=True),
    ]
    p = propose(rows, {})
    tickets = _tickets(p)
    assert len(tickets) == 2
    for t in tickets:
        assert t.items[0].inspection_filter is None      # 不是商检半票
        assert t.sj_factories == []
        # 商检行混票也不产生 mixed_sj 警告（MX 不看商检）
        assert all(w.rule != "mixed_sj" for w in t.warnings)


def test_mixed_mx_plain_container_warning():
    """混柜（MX 行+普通行）→ 整柜按 MX 逻辑一厂一票 + mixed_mx 中文警告。"""
    rows = [
        _row("MX1", MX_A, "mx-a1", is_mx=True),
        _row("MX1", PLAIN_X, "p-x1"),                    # 同柜普通行
    ]
    p = propose(rows, {})
    tickets = _tickets(p)
    # 整柜走 MX 逻辑：两厂各一票（普通厂的行也由一厂一票承载，不丢）
    assert len(tickets) == 2
    assert [t.items[0].factory_filter for t in tickets] == [MX_A, PLAIN_X]
    for t in tickets:
        rules = [w.rule for w in t.warnings]
        assert "mixed_mx" in rules
        msg = next(w.message for w in t.warnings if w.rule == "mixed_mx")
        assert "管理号 MX1 同时包含 MX 与普通货物" in msg
        assert "已按 MX 逻辑整柜处理" in msg


def test_pure_mx_container_no_mixed_warning():
    """纯 MX 柜（全部行 is_mx）不产生混柜警告。"""
    p = propose(_mx_container_rows(), {})
    for t in _tickets(p):
        assert all(w.rule != "mixed_mx" for w in t.warnings)


# ===========================================================================
# C. aggregator / validate 语义
# ===========================================================================

def test_aggregator_factory_filter_none_returns_whole_factory():
    """rows_for_ticket：factory_filter=F + inspection_filter=None
    返回柜内该厂全部行（不区分商检与否）。"""
    rows = [
        _row("MX1", MX_A, "a1", inspection=True, is_mx=True),
        _row("MX1", MX_A, "a2", inspection=False, is_mx=True),
        _row("MX1", MX_B, "b1", is_mx=True),
    ]
    p = propose(rows, {})
    t_a = next(t for t in _tickets(p) if t.items[0].factory_filter == MX_A)
    got = rows_for_ticket(t_a, rows, {})
    assert sorted(r.sku for r in got) == ["a1", "a2"]   # 商检/不商检行都含


def _mx_proposal_dict():
    rows = _mx_container_rows()
    p = propose(list(rows), {})
    return [r.model_dump() for r in rows], p.model_dump()


def test_validate_accepts_mx_proposal():
    """validate_confirmed_proposal 接受引擎产出的 MX 一厂一票方案
    （一厂一票的并集恰好覆盖全柜，零错误零警告）。"""
    raw_dicts, proposal = _mx_proposal_dict()
    errors, warnings = validate_confirmed_proposal(proposal, raw_dicts, {})
    assert errors == []
    assert warnings == []


def test_validate_accepts_user_merged_mx_tickets():
    """审核界面把 3 张一厂一票手动合并成 1 票（3 个 partial item）
    → 覆盖仍完整，校验通过（可 confirm）。"""
    raw_dicts, proposal = _mx_proposal_dict()
    groups = proposal["ports"][0]["groups"]
    assert len(groups) == 3
    merged = groups[0]
    merged["items"] = (
        groups[0]["items"] + groups[1]["items"] + groups[2]["items"]
    )
    proposal["ports"][0]["groups"] = [merged]
    errors, warnings = validate_confirmed_proposal(proposal, raw_dicts, {})
    assert errors == []
    assert warnings == []


def test_validate_accepts_user_whole_mx_ticket():
    """审核界面把 MX 柜多张一厂一票改成 1 张整柜票 → 校验通过。"""
    raw_dicts, proposal = _mx_proposal_dict()
    whole = {
        "ticket_no": proposal["ports"][0]["groups"][0]["ticket_no"],
        "port": PORT,
        "container_type": CTYPE,
        "items": [{"kanri_no": "MX1", "factory_filter": None,
                   "factory_exclude": None, "is_partial": False,
                   "inspection_filter": None}],
        "sj_factories": [],
        "full_containers": 1,
        "warnings": [],
    }
    proposal["ports"][0]["groups"] = [whole]
    errors, warnings = validate_confirmed_proposal(proposal, raw_dicts, {})
    assert errors == []
    assert warnings == []


def test_validate_accepts_new_chunking_plain_proposal():
    """新合票逻辑的普通方案（5 柜 → (2,3)）校验零错误零警告。"""
    rows = _plain_rows(5)
    raw_dicts = [r.model_dump() for r in rows]
    proposal = propose(list(rows), {}).model_dump()
    errors, warnings = validate_confirmed_proposal(proposal, raw_dicts, {})
    assert errors == []
    assert warnings == []
