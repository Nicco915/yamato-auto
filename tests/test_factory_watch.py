# -*- coding: utf-8 -*-
"""缺资料文件夹监控 → 自动补充测试（2026-10-12 需求 4，实施计划阶段二
§2.1/§2.5）。

覆盖：
1. 稳定判定：首轮不触发、快照不变第二轮触发、快照变化重置重数两轮；
2. 触发：monkeypatch service.add_factories_to_batch 记录调用，调
   _scan_once() 两次断言被调且仅一次；
3. 冷却：add_factories 抛异常 → 冷却期内不再重试，冷却过期后恢复；
3b. 触发成功快照去重（2026-10-12 调整 A）：文件未变不重复触发、
   文件有变化允许再触发、触发失败不记快照（冷却到期可重试）；
4. 范围：非 awaiting 批次、已 done 工厂文件夹的新文件均不触发；
5. batch_store.list_by_status 单测。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_factory_watch.py -v

隔离红线（2026-08-11 事故教训，骨架照抄 test_board_error_recovery.py）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
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
_TMP = Path(tempfile.mkdtemp(prefix="yamato_factory_watch_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402
from app.orchestrator import factory_watch  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_factory_watch_test_")


_CREATED_TIDS: list[str] = []


@pytest.fixture(autouse=True)
def _clean_watch_state(tmp_path):
    """每个用例前清监控线程模块级状态（快照表/冷却表/停止旗标），结束后
    恢复 settings.upstream_root（红线 2：settings 单例钉根）并删本用例
    造的批次行（共享临时库，残留 awaiting 批次会被后续用例的扫描捞到）。

    顺带把 upstream_root 兜底指到空目录——同进程其他测试文件遗留的
    awaiting 批次本轮也会被扫到，绝不让兜底路径摸到真实上游目录。
    """
    factory_watch.stop_factory_watch()
    with factory_watch._state_lock:
        factory_watch._snapshots.clear()
        factory_watch._cooldowns.clear()
        factory_watch._last_triggered.clear()
    s = get_settings()
    original = s.upstream_root
    empty = tmp_path / "empty_upstream"
    empty.mkdir()
    s.upstream_root = str(empty)
    try:
        yield
    finally:
        s.upstream_root = original
        factory_watch.stop_factory_watch()
        with factory_watch._state_lock:
            factory_watch._snapshots.clear()
            factory_watch._cooldowns.clear()
            factory_watch._last_triggered.clear()
        for tid in _CREATED_TIDS:
            batch_store.delete_batch(tid)
        _CREATED_TIDS.clear()


@pytest.fixture()
def upstream(tmp_path):
    """每个用例独立上游工厂目录（经 checkpoint state 传入，不走 settings）。"""
    d = tmp_path / "upstream"
    d.mkdir()
    return d


def _make_awaiting_batch(tid: str, upstream: Path, *,
                         done: list[str], gap: list[str],
                         status: str = "awaiting_factories") -> None:
    """造一条 awaiting 批次行 + 「图已跑完」checkpoint（next 为空，
    as_node=NODE7）：downstream_requirements = done + gap，factory_outputs
    只含 done，upstream_root 指向临时目录。"""
    batch_store.upsert_batch(tid, status=status)
    _CREATED_TIDS.append(tid)
    graph = get_graph()
    cfg = {"configurable": {"thread_id": tid}}
    graph.update_state(cfg, {
        "downstream_requirements": {f: ["4900000000001"] for f in done + gap},
        "factory_outputs": {f: {"calculated_items": []} for f in done},
        "upstream_root": str(upstream),
        "final_output_path": "/tmp/out.xlsx",
    }, as_node=NODE7)


def _drop_doc(folder: Path, name: str = "packing.pdf", size: int = 128) -> Path:
    """往工厂文件夹丢一个受支持单据（默认 PDF），返回路径。"""
    p = folder / name
    p.write_bytes(b"x" * size)
    return p


def _record_calls(monkeypatch) -> list[str]:
    """monkeypatch service.add_factories_to_batch 为记录器，返回调用列表。"""
    calls: list[str] = []

    def fake_add(tid: str):
        calls.append(tid)
        return {"added": 1, "factories": [], "status": "pending_human_review"}

    monkeypatch.setattr(service, "add_factories_to_batch", fake_add)
    return calls


# ---------------------------------------------------------------------------
# 1. 稳定判定（半拷贝防护：连续两轮快照一致才算到达）
# ---------------------------------------------------------------------------

def test_stable_rounds_gate(upstream, monkeypatch):
    """首轮仅记录不触发；快照不变第二轮触发。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-STABLE-1"
    (upstream / "缺口厂").mkdir()
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()  # 文件夹为空：不匹配候选
    assert calls == []

    _drop_doc(upstream / "缺口厂")
    factory_watch._scan_once()  # 首轮仅记录快照
    assert calls == []

    factory_watch._scan_once()  # 第二轮快照一致 → 触发
    assert calls.count(tid) == 1


def test_snapshot_change_restarts_count(upstream, monkeypatch):
    """快照变化（模拟半拷贝）→ 覆写重数，下一轮仍不触发。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-STABLE-2"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    p = _drop_doc(folder, size=128)
    factory_watch._scan_once()  # 记录
    assert calls == []

    p.write_bytes(b"y" * 256)  # 大小变化（仍在拷贝中）
    factory_watch._scan_once()  # 不一致 → 覆写等下轮，不触发
    assert calls == []

    factory_watch._scan_once()  # 稳定后的第二轮 → 触发
    assert calls.count(tid) == 1


def test_empty_folder_not_candidate(upstream, monkeypatch):
    """命中但空文件夹（预建空文件夹场景）→ 永不候选；不支持的扩展名同理。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-STABLE-3"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    (folder / "说明.txt").write_text("不是受支持单据", encoding="utf-8")
    for _ in range(3):
        factory_watch._scan_once()
    assert calls == []

    _drop_doc(folder)
    factory_watch._scan_once()  # 记录
    factory_watch._scan_once()  # 触发
    assert calls.count(tid) == 1


# ---------------------------------------------------------------------------
# 2. 触发：调 _scan_once() 两次，add_factories_to_batch 被调且仅一次
# ---------------------------------------------------------------------------

def test_trigger_calls_add_factories_once(upstream, monkeypatch):
    calls = _record_calls(monkeypatch)
    tid = "FW-TRIG-1"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _drop_doc(folder)
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()
    factory_watch._scan_once()
    assert calls.count(tid) == 1


# ---------------------------------------------------------------------------
# 3. 冷却：触发抛异常 → 冷却期内不再重试；冷却过期后恢复
# ---------------------------------------------------------------------------

def test_trigger_failure_cooldown(upstream, monkeypatch):
    calls: list[str] = []

    def boom(tid: str):
        calls.append(tid)
        raise RuntimeError("模拟补充失败")

    monkeypatch.setattr(service, "add_factories_to_batch", boom)
    tid = "FW-COOL-1"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _drop_doc(folder)
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()  # 记录
    factory_watch._scan_once()  # 触发 → 抛异常 → 记冷却
    assert calls.count(tid) == 1

    # 冷却期内：反复扫描都不再触发
    for _ in range(4):
        factory_watch._scan_once()
    assert calls.count(tid) == 1

    # 冷却过期后恢复重试（直接把冷却时间戳拨到过去）
    with factory_watch._state_lock:
        factory_watch._cooldowns[tid] = 0.0
    factory_watch._scan_once()  # 记录（到达时快照已移除，重数两轮）
    factory_watch._scan_once()  # 再触发
    assert calls.count(tid) == 2


# ---------------------------------------------------------------------------
# 3b. 触发成功快照去重（2026-10-12 调整 A）：文件未变不重复触发
# ---------------------------------------------------------------------------

def test_no_retrigger_when_snapshot_unchanged(upstream, monkeypatch):
    """触发成功后批次仍在 awaiting（模拟人工驳回/跳过回到待补充）且文件夹
    内容未动 → 不再重复触发，不推翻人工决定。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-DEDUP-1"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _drop_doc(folder)
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()  # 记录
    factory_watch._scan_once()  # 触发
    assert calls.count(tid) == 1
    # 触发成功：记录当时的稳定快照
    assert factory_watch._last_triggered.get((tid, "缺口厂")) == {
        "packing.pdf": 128}

    # 批次仍在 awaiting、文件夹未动：反复扫描都不再触发
    for _ in range(4):
        factory_watch._scan_once()
    assert calls.count(tid) == 1


def test_retrigger_after_file_change(upstream, monkeypatch):
    """触发成功后文件夹有变化（新增文件/大小变）→ 允许再次触发。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-DEDUP-2"
    folder = upstream / "缺口厂"
    folder.mkdir()
    p = _drop_doc(folder, size=128)
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()
    factory_watch._scan_once()
    assert calls.count(tid) == 1

    # 场景一：大小变化（重新拷贝了单据）
    p.write_bytes(b"z" * 256)
    factory_watch._scan_once()  # 与触发快照有差异 → 进稳定计数（首轮记录）
    assert calls.count(tid) == 1
    factory_watch._scan_once()  # 第二轮一致 → 再次触发
    assert calls.count(tid) == 2
    assert factory_watch._last_triggered[(tid, "缺口厂")] == {"packing.pdf": 256}

    # 场景二：新增文件
    _drop_doc(folder, name="invoice.pdf", size=64)
    factory_watch._scan_once()
    factory_watch._scan_once()
    assert calls.count(tid) == 3


def test_trigger_failure_records_no_snapshot(upstream, monkeypatch):
    """触发失败走冷却且不记快照：冷却到期后即使文件没变也允许重试；
    重试成功后才记录快照。"""
    state = {"fail": True}
    calls: list[str] = []

    def flaky(tid: str):
        calls.append(tid)
        if state["fail"]:
            raise RuntimeError("模拟补充失败")
        return {"added": 1, "factories": [], "status": "pending_human_review"}

    monkeypatch.setattr(service, "add_factories_to_batch", flaky)
    tid = "FW-DEDUP-3"
    folder = upstream / "缺口厂"
    folder.mkdir()
    _drop_doc(folder)
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺口厂"])

    factory_watch._scan_once()  # 记录
    factory_watch._scan_once()  # 触发 → 抛异常 → 记冷却
    assert calls.count(tid) == 1
    # 失败不记触发快照
    assert (tid, "缺口厂") not in factory_watch._last_triggered

    # 冷却到期（拨回时间戳），文件没变：无快照记录 → 照常重数两轮重试
    with factory_watch._state_lock:
        factory_watch._cooldowns[tid] = 0.0
    state["fail"] = False
    factory_watch._scan_once()  # 记录
    factory_watch._scan_once()  # 重试成功
    assert calls.count(tid) == 2
    assert factory_watch._last_triggered.get((tid, "缺口厂")) == {
        "packing.pdf": 128}


# ---------------------------------------------------------------------------
# 4. 范围：非 awaiting 批次 / 已 done 工厂文件夹的新文件均不触发
# ---------------------------------------------------------------------------

def test_scope_non_awaiting_batch_not_scanned(upstream, monkeypatch):
    """running 批次（非 awaiting）即使有缺口+文件也不进扫描口径。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-SCOPE-RUN"
    folder = upstream / "跑动缺口厂"
    folder.mkdir()
    _drop_doc(folder)
    _make_awaiting_batch(tid, upstream, done=[], gap=["跑动缺口厂"],
                         status="running")

    factory_watch._scan_once()
    factory_watch._scan_once()
    assert tid not in calls


def test_scope_done_factory_new_file_ignored(upstream, monkeypatch):
    """已 done 工厂文件夹进新文件不触发（本模块只看缺口工厂）。"""
    calls = _record_calls(monkeypatch)
    tid = "FW-SCOPE-DONE"
    done_folder = upstream / "完成厂"
    done_folder.mkdir()
    _drop_doc(done_folder)
    # 缺口厂无文件夹（不匹配）；完成厂已在 factory_outputs，不在缺口口径
    _make_awaiting_batch(tid, upstream, done=["完成厂"], gap=["缺资料厂"])

    factory_watch._scan_once()
    factory_watch._scan_once()
    assert tid not in calls


# ---------------------------------------------------------------------------
# 5. batch_store.list_by_status 单测
# ---------------------------------------------------------------------------

def test_list_by_status():
    batch_store.upsert_batch("FW-LBS-1", status="awaiting_factories")
    batch_store.upsert_batch("FW-LBS-2", status="awaiting_factories")
    batch_store.upsert_batch("FW-LBS-3", status="completed")
    try:
        rows = batch_store.list_by_status("awaiting_factories")
        tids = {r["thread_id"] for r in rows}
        assert {"FW-LBS-1", "FW-LBS-2"} <= tids
        assert "FW-LBS-3" not in tids
        assert all(r["status"] == "awaiting_factories" for r in rows)
    finally:
        batch_store.delete_batch("FW-LBS-1")
        batch_store.delete_batch("FW-LBS-2")
        batch_store.delete_batch("FW-LBS-3")


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
