# -*- coding: utf-8 -*-
"""缺资料工厂自动跳过测试（2026-10-12 需求 1，实施计划阶段一 §1.1/§1.2/§1.9）。

覆盖：
1. _route_after_compute 三路单测：extraction_ok × failure_reason ×
   is_final_attempt × has_more 组合 → NODE4B / NODE4C / NODE5；
2. no_folder_matched 端到端：2 厂（1 厂有 mock 单据、1 厂无文件夹）→
   无文件夹厂不挂人工、自动跳过，审计留痕 factory_auto_skipped，
   factory_outputs 只有有单据厂，批次状态归类 awaiting_factories；
3. no_documents 端到端：预建空文件夹厂同口径自动跳过（reason=no_documents）；
4. no_items_extracted 回归：有单据但提取为空（fake session 返空）→
   仍到 Node5 挂起人工，不被自动跳过。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_auto_skip_no_materials.py -v

隔离红线（2026-08-11 事故教训，骨架照抄 test_board_error_recovery.py）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env；
- import 后调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录。
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
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_autoskip_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from sqlalchemy import select  # noqa: E402

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.db.models import ReviewAudit  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.graph import (  # noqa: E402
    NODE4B, NODE4C, NODE5, _route_after_compute)
from app.nodes import extraction_node as en  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_autoskip_test_")

HEADER = ["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"]


def _write_manifest(path: Path, rows: list[tuple[str, str, str, int]]) -> str:
    """生成最小下游装箱单 xlsx，返回路径字符串。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for factory, sku, name, qty in rows:
        ws.append([factory, sku, name, qty])
    wb.save(path)
    return str(path)


def _make_upstream(tag: str, folders_with_docs: dict[str, int]) -> str:
    """建上游目录：{文件夹名: 单据数}（0 = 预建空文件夹）。"""
    root = TMP / f"upstream_{tag}"
    for folder, n_docs in folders_with_docs.items():
        d = root / folder
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n_docs):
            (d / f"doc{i}.pdf").write_bytes(b"%PDF-1.4 fake")
    return str(root)


def _force_mock_extraction(monkeypatch) -> None:
    """全量 pytest 下强制回 mock（与 add_factories_test 同模式）。"""
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")


def _audit_rows(thread_id: str) -> list[ReviewAudit]:
    with get_session() as session:
        return list(session.scalars(
            select(ReviewAudit)
            .where(ReviewAudit.thread_id == thread_id)
            .order_by(ReviewAudit.audit_id)
        ).all())


# ---------------------------------------------------------------------------
# 1. _route_after_compute 三路分流单测
# ---------------------------------------------------------------------------

def _state(*, ok=False, reason=None, final=False, pending=(), deferred=()):
    return {
        "current_factory_data": {
            "factory_name": "路由厂",
            "extraction_ok": ok,
            "failure_reason": reason,
            "is_final_attempt": final,
        },
        "pending_factories": list(pending),
        "deferred_factories": list(deferred),
    }


@pytest.mark.parametrize("state,expected", [
    # 失败 + 非二遍 + 后面还有厂 → Node4B 暂缓（两种缺资料原因同口径）
    (_state(reason="no_folder_matched", pending=["X"]), NODE4B),
    (_state(reason="no_documents", deferred=[{"factory_name": "X"}]), NODE4B),
    (_state(reason="no_items_extracted", pending=["X"]), NODE4B),
    # 缺资料 + 二遍仍失败 → Node4C 自动跳过（即使后面还有暂缓厂）
    (_state(reason="no_folder_matched", final=True, deferred=[{"factory_name": "X"}]), NODE4C),
    # 缺资料 + 最后一个工厂首遍即缺资料（不空转）→ Node4C
    (_state(reason="no_folder_matched"), NODE4C),
    (_state(reason="no_documents"), NODE4C),
    # 有单据但提取为空 / 提取异常：即使二遍或最后一家仍挂人工 → Node5
    (_state(reason="no_items_extracted", final=True), NODE5),
    (_state(reason="no_items_extracted"), NODE5),
    (_state(reason="extraction_error: boom", final=True), NODE5),
    # 成功 → Node5
    (_state(ok=True), NODE5),
])
def test_route_after_compute_three_way(state, expected):
    assert _route_after_compute(state) == expected


# ---------------------------------------------------------------------------
# 2. no_folder_matched 端到端：无文件夹厂自动跳过，不挂人工
# ---------------------------------------------------------------------------

def test_e2e_no_folder_matched_auto_skip(monkeypatch):
    _force_mock_extraction(monkeypatch)
    # 厂名刻意取零相似度（rapidfuzz ratio=0），防 B 被 fuzzy 匹配到 A 的文件夹
    F_A, F_B = "阿尔法食品厂", "欧米茄机械株式会社"
    THREAD = "AUTOSKIP-E2E-NOFOLDER"

    xlsx = _write_manifest(TMP / "autoskip_nofolder.xlsx", [
        (F_A, "4900000001001", "ITEM-A", 10),
        (F_B, "4900000001002", "ITEM-B", 20),
    ])
    # 只给 A 备单据；B 不建文件夹
    upstream = _make_upstream("nofolder", {F_A: 1})

    r1 = service.run_until_interrupt(THREAD, downstream_file_path=xlsx,
                                     upstream_root=upstream)
    assert r1["status"] == "pending_human_review"
    assert r1["review_data"]["factory_name"] == F_A

    # approve A 后：B 是最后一个工厂、首遍即缺资料 → Node4C 直接自动跳过
    # → 两队列皆空 → Node7 → END，不再挂起人工
    r2 = service.resume_order(THREAD, {"approved": True, "items": []})
    assert r2["status"] == "success", r2

    state = service.get_order_state(THREAD)
    assert not state["next_nodes"], state["next_nodes"]
    outputs = state["values"].get("factory_outputs") or {}
    assert set(outputs) == {F_A}, sorted(outputs)
    skipped = state["values"].get("auto_skipped_factories") or []
    assert [s["factory_name"] for s in skipped] == [F_B]
    assert skipped[0]["failure_reason"] == "no_folder_matched"

    # 审计留痕：B 一条 factory_auto_skipped（approved=False）
    b_audits = [r for r in _audit_rows(THREAD) if r.factory_name == F_B]
    assert len(b_audits) == 1
    assert b_audits[0].result_status == "factory_auto_skipped"
    assert b_audits[0].approved is False

    # 批次状态归类：待补充（B 缺资料），详情页 B 角色 auto_skipped
    from app.orchestrator.pipeline_state import get_pipeline_state
    ps = get_pipeline_state(THREAD)
    assert ps["batch"]["status"] == "awaiting_factories", ps["batch"]
    detail = service.get_batch_detail(THREAD)
    assert detail["status"] == "awaiting_factories"
    roles = {f["factory"]: f["role"] for f in detail["factories"]}
    assert roles[F_A] == "done" and roles[F_B] == "auto_skipped", roles
    print("[断言通过] no_folder_matched 端到端：自动跳过 + 留痕 + 待补充归类")


# ---------------------------------------------------------------------------
# 3. no_documents 端到端：预建空文件夹厂同口径自动跳过
# ---------------------------------------------------------------------------

def test_e2e_no_documents_auto_skip(monkeypatch):
    _force_mock_extraction(monkeypatch)
    # 厂名刻意取零相似度（rapidfuzz ratio=0），防 B 被 fuzzy 匹配到 A 的文件夹
    F_A, F_B = "北海罐头厂", "南极冷库株式会社"
    THREAD = "AUTOSKIP-E2E-NODOC"

    xlsx = _write_manifest(TMP / "autoskip_nodoc.xlsx", [
        (F_A, "4900000001101", "ITEM-A", 10),
        (F_B, "4900000001102", "ITEM-B", 20),
    ])
    # A 有单据；B 预建空文件夹（无受支持单据）
    upstream = _make_upstream("nodoc", {F_A: 1, F_B: 0})

    r1 = service.run_until_interrupt(THREAD, downstream_file_path=xlsx,
                                     upstream_root=upstream)
    assert r1["review_data"]["factory_name"] == F_A
    r2 = service.resume_order(THREAD, {"approved": True, "items": []})
    assert r2["status"] == "success", r2

    state = service.get_order_state(THREAD)
    assert not state["next_nodes"]
    outputs = state["values"].get("factory_outputs") or {}
    assert set(outputs) == {F_A}
    skipped = state["values"].get("auto_skipped_factories") or []
    assert [s["factory_name"] for s in skipped] == [F_B]
    assert skipped[0]["failure_reason"] == "no_documents"

    b_audits = [r for r in _audit_rows(THREAD) if r.factory_name == F_B]
    assert len(b_audits) == 1
    assert b_audits[0].result_status == "factory_auto_skipped"
    print("[断言通过] no_documents 端到端：空文件夹厂自动跳过（reason=no_documents）")


# ---------------------------------------------------------------------------
# 4. no_items_extracted 回归：有单据但提取为空仍挂人工审核
# ---------------------------------------------------------------------------

class _EmptySession:
    """fake FactorySession：有单据但一条都没提出来。"""

    items: dict = {}
    no_code_items: list = []
    issues: list = []
    status = "complete"

    def coverage(self):
        return {"extracted": 0, "expected": 1, "missing": ["X"], "extra": []}


def test_no_items_extracted_still_interrupts(monkeypatch):
    F_A = "空取厂甲"
    THREAD = "AUTOSKIP-E2E-EMPTY"

    xlsx = _write_manifest(TMP / "autoskip_empty.xlsx", [
        (F_A, "4900000001201", "ITEM-A", 10),
    ])
    upstream = _make_upstream("empty", {F_A: 1})

    # 绕过 mock 短路：_session_mod 非 None + _run_factory_session 返空会话
    monkeypatch.setattr(en, "_session_mod", object())
    monkeypatch.setattr(en, "_run_factory_session",
                        lambda *a, **kw: _EmptySession())

    r1 = service.run_until_interrupt(THREAD, downstream_file_path=xlsx,
                                     upstream_root=upstream)
    # 不被自动跳过：仍到 Node5 挂起人工补录
    assert r1["status"] == "pending_human_review", r1
    assert r1["review_data"]["factory_name"] == F_A

    state = service.get_order_state(THREAD)
    assert state["next_nodes"] == ["node5_human_review"], state["next_nodes"]
    assert not (state["values"].get("auto_skipped_factories") or [])
    cur = state["values"].get("current_factory_data") or {}
    assert cur.get("failure_reason") == "no_items_extracted"
    assert not [r for r in _audit_rows(THREAD)
                if r.result_status == "factory_auto_skipped"]
    print("[断言通过] no_items_extracted 回归：有单据提取为空仍挂人工审核")


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
