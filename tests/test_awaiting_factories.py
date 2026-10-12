# -*- coding: utf-8 -*-
"""「待补充」状态与强制结案测试（2026-10-12 需求 2，实施计划阶段一 §1.3/§1.4/§1.6/§1.9）。

覆盖：
1. pipeline_state 新分类：export_done + 缺口工厂的审计留痕为
   factory_auto_skipped / factory_skipped / 无审计 → awaiting_factories；
   缺口全部为人工驳回 → completed_with_errors（语义收窄）；混合 → awaiting；
2. force_completed 防自愈打回：强制结案标志置位后保持 completed；
3. service.force_complete_batch：状态流转 awaiting→completed + 留痕
   batch_force_completed + 非待补充状态 ValueError；
4. board_state：awaiting 批次进 in_progress 且透出
   factories_failed / created_at / monitoring=True。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_awaiting_factories.py -v

隔离红线（2026-08-11 事故教训，骨架照抄 test_board_error_recovery.py）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env；
- import 后调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录。
"""
from __future__ import annotations

import json
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
_TMP = Path(tempfile.mkdtemp(prefix="yamato_awaiting_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.db.models import ReviewAudit  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402
from app.orchestrator import board, pipeline_state  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_awaiting_test_")


@pytest.fixture()
def watch(tmp_path):
    """每个用例独立监控目录；结束后恢复 settings.watch_dir。"""
    w = tmp_path / "watch"
    w.mkdir()
    original = get_settings().watch_dir
    get_settings().watch_dir = str(w)
    try:
        yield w
    finally:
        get_settings().watch_dir = original


def _make_finished_state(thread_id: str, done: list[str], gap: list[str]) -> None:
    """造「图已跑完」checkpoint：next 为空（as_node=NODE7），
    downstream_requirements = done + gap，factory_outputs 只含 done。"""
    graph = get_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    requirements = {f: ["4900000000001"] for f in done + gap}
    graph.update_state(cfg, {
        "downstream_requirements": requirements,
        "factory_outputs": {f: {"calculated_items": []} for f in done},
        "final_output_path": "/tmp/out.xlsx",
    }, as_node=NODE7)


def _add_audit(thread_id: str, factory: str | None, *, approved: bool,
               result_status: str | None) -> None:
    """插一条 review_audits 留痕。"""
    with get_session() as session:
        session.add(ReviewAudit(
            thread_id=thread_id,
            factory_name=factory,
            approved=approved,
            edited_count=0,
            changes_json="[]",
            new_skus_json="[]",
            result_status=result_status,
        ))
        session.commit()


def _status_of(thread_id: str) -> str:
    return pipeline_state.get_pipeline_state(thread_id)["batch"]["status"]


# ---------------------------------------------------------------------------
# 1. pipeline_state 新分类口径
# ---------------------------------------------------------------------------

def test_classify_auto_skipped_goes_awaiting():
    tid = "AWT-CLS-AUTOSKIP"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["缺资料厂"])
    _add_audit(tid, "缺资料厂", approved=False,
               result_status="factory_auto_skipped")
    assert _status_of(tid) == "awaiting_factories"
    assert batch_store.get_batch(tid)["status"] == "awaiting_factories"


def test_classify_manual_skipped_goes_awaiting():
    tid = "AWT-CLS-SKIP"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["人工跳过厂"])
    _add_audit(tid, "人工跳过厂", approved=False, result_status="factory_skipped")
    assert _status_of(tid) == "awaiting_factories"


def test_classify_no_audit_goes_awaiting():
    tid = "AWT-CLS-NOAUDIT"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["未处理厂"])
    assert _status_of(tid) == "awaiting_factories"


def test_classify_all_rejected_goes_completed_with_errors():
    """缺口全部为人工驳回（有审计、approved=False、非跳过类）→ 收窄后的
    completed_with_errors。"""
    tid = "AWT-CLS-REJECTED"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["驳回厂甲", "驳回厂乙"])
    _add_audit(tid, "驳回厂甲", approved=False, result_status="rejected")
    _add_audit(tid, "驳回厂乙", approved=False, result_status="rejected")
    assert _status_of(tid) == "completed_with_errors"


def test_classify_mixed_goes_awaiting():
    tid = "AWT-CLS-MIXED"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["缺资料厂", "驳回厂"])
    _add_audit(tid, "缺资料厂", approved=False,
               result_status="factory_auto_skipped")
    _add_audit(tid, "驳回厂", approved=False, result_status="rejected")
    assert _status_of(tid) == "awaiting_factories"


def test_classify_force_completed_not_reverted():
    """force_completed=1：自愈保持 completed，不打回 awaiting。"""
    tid = "AWT-CLS-FORCE"
    batch_store.upsert_batch(tid, status="awaiting_factories")
    _make_finished_state(tid, done=["完成厂"], gap=["缺资料厂"])
    _add_audit(tid, "缺资料厂", approved=False,
               result_status="factory_auto_skipped")
    # 未置标志前是 awaiting
    assert _status_of(tid) == "awaiting_factories"
    # 置标志 + 结案后再读：自愈不得打回
    assert batch_store.set_force_completed(tid)
    batch_store.update_status(tid, "completed")
    assert _status_of(tid) == "completed"
    assert batch_store.get_batch(tid)["status"] == "completed"


# ---------------------------------------------------------------------------
# 2. service.force_complete_batch
# ---------------------------------------------------------------------------

def test_force_complete_batch_flow():
    tid = "AWT-FC-FLOW"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["缺资料厂"])
    _add_audit(tid, "缺资料厂", approved=False,
               result_status="factory_auto_skipped")
    assert _status_of(tid) == "awaiting_factories"

    r = service.force_complete_batch(tid)
    assert r["ok"] is True and r["status"] == "completed", r
    assert "已强制结案" in r["message"]

    row = batch_store.get_batch(tid)
    assert row["status"] == "completed"
    assert row["force_completed"] is True
    assert row["completed_at"] is not None

    # 防自愈打回：再读仍是 completed
    assert _status_of(tid) == "completed"

    # 留痕：batch_force_completed（factory_name=None）
    with get_session() as session:
        rows = [x for x in session.query(ReviewAudit)
                .filter(ReviewAudit.thread_id == tid).all()
                if x.result_status == "batch_force_completed"]
    assert len(rows) == 1 and rows[0].factory_name is None
    assert json.loads(rows[0].changes_json) == [{"force_complete": True}]


def test_force_complete_batch_rejects_non_awaiting():
    # 批次不存在
    with pytest.raises(ValueError):
        service.force_complete_batch("AWT-FC-MISSING")
    # 全部完成（无缺口）→ completed，非待补充 → ValueError
    tid = "AWT-FC-DONE"
    batch_store.upsert_batch(tid, status="running")
    _make_finished_state(tid, done=["完成厂"], gap=[])
    assert _status_of(tid) == "completed"
    with pytest.raises(ValueError):
        service.force_complete_batch(tid)


# ---------------------------------------------------------------------------
# 3. board_state：awaiting 批次进 in_progress 并透出增强字段
# ---------------------------------------------------------------------------

def test_board_state_awaiting_in_progress_fields(watch):
    tid = "AWT-BOARD-1"
    (watch / "待补批次").mkdir()
    batch_store.upsert_batch(tid, watch_dir=str(watch),
                             folder_name="待补批次", status="running")
    _make_finished_state(tid, done=["完成厂"], gap=["缺资料厂"])
    _add_audit(tid, "缺资料厂", approved=False,
               result_status="factory_auto_skipped")

    state = board.board_state()
    prog = {d["folder_name"]: d for d in state["in_progress"]}
    assert "待补批次" in prog, state
    card = prog["待补批次"]
    assert card["status"] == "awaiting_factories"
    assert card["factories_failed"] == 1
    assert card["monitoring"] is True
    assert card["created_at"]  # 透出建批时间（ISO 字符串）
    # 自愈回写：batches 行也被校正
    assert batch_store.get_batch(tid)["status"] == "awaiting_factories"


def test_board_state_completed_not_monitoring(watch):
    """对照组：正常完成批次进 done 档，不在执行中列。"""
    tid = "AWT-BOARD-2"
    (watch / "完成批次").mkdir()
    batch_store.upsert_batch(tid, watch_dir=str(watch),
                             folder_name="完成批次", status="running")
    _make_finished_state(tid, done=["完成厂"], gap=[])

    state = board.board_state()
    assert "完成批次" in {d["folder_name"] for d in state["done"]}
    assert "完成批次" not in {d["folder_name"] for d in state["in_progress"]}


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
