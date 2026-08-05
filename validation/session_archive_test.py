# -*- coding: utf-8 -*-
"""批次启动归档旧 session 缓存测试（2026-08-05，方案A）。

覆盖：
1. 基本归档：sessions/*.json 整体移动到 sessions/_archive/{批次号}/，
   原位置清空，返回归档数；子目录（含 _archive 自身）不被触碰；
2. sessions 目录不存在 → 静默返回 0，不报错；
3. sessions 目录为空（无 *.json）→ 静默返回 0；
4. 归档目标目录已存在 → 直接并入（同名文件覆盖）；
5. 危险批次号字符过滤：归档目录始终落在 SESSIONS_DIR/_archive 内
   （防目录穿越，复用 _safe_path_tag 同一套过滤）；
6. run_until_interrupt 守卫：已有 checkpoint state 的 thread 跳过归档，
   全新 thread 才触发归档；
7. 在途批次提醒：归档后枚举到其他 pending_review/running 批次时打
   醒目 warning（归档仍照常执行）。

隔离原则同 preextract_progress_test（import 完成后 isolate_to_tmp）；
全程只操作临时目录下的 sessions，绝不碰真实 app/data/sessions。

用法（在 app/ 目录下）：
  python3 validation/session_archive_test.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

os.environ["EXTRACTION_MOCK"] = "1"  # 提取走 mock，不调 LLM

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api import service  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_session_archive_")
SESS = service.SESSIONS_DIR  # isolate_to_tmp 已 patch 到临时目录


def _mk_session(name: str, content: str = "{}") -> Path:
    path = SESS / name
    path.write_text(content, encoding="utf-8")
    return path


def test_basic_archive() -> None:
    """基本归档：*.json 移到 _archive/{批次}/，原位置清空，子目录不动。"""
    print("===== 1. 基本归档 =====")
    _mk_session("工厂A.json", '{"status": "complete_auto"}')
    _mk_session("工厂B.json", '{"status": "collecting"}')
    _mk_session("_preextract_progress_旧批次.json")  # 旧进度文件也一并归档
    # 子目录及其中文件不应被动（_archive 是子目录，天然排除）
    sub = SESS / "其他子目录"
    sub.mkdir()
    (sub / "不该动.json").write_text("{}", encoding="utf-8")

    moved = service._archive_sessions_for_new_batch("批次-001")
    assert moved == 3, f"归档数应为 3，实际 {moved}"

    dest = SESS / "_archive" / "批次-001"
    for name in ("工厂A.json", "工厂B.json", "_preextract_progress_旧批次.json"):
        assert (dest / name).is_file(), f"归档目标缺文件: {dest / name}"
        assert not (SESS / name).exists(), f"原位置未清空: {SESS / name}"
    assert (dest / "工厂A.json").read_text(encoding="utf-8") == \
        '{"status": "complete_auto"}'
    # 顶层再无 *.json；子目录原样保留
    leftovers = [p for p in SESS.iterdir() if p.is_file()]
    assert not leftovers, f"顶层残留文件: {leftovers}"
    assert (sub / "不该动.json").is_file(), "子目录文件被误动"
    print(f"  ✓ 3 个文件已归档到 {dest}，原位置清空，子目录未动")


def test_missing_sessions_dir() -> None:
    """sessions 目录不存在 → 静默返回 0，不报错。"""
    print("\n===== 2. sessions 目录不存在 =====")
    orig = service.SESSIONS_DIR
    service.SESSIONS_DIR = TMP / "不存在的sessions目录"
    try:
        moved = service._archive_sessions_for_new_batch("批次-不存在")
        assert moved == 0, f"应返回 0，实际 {moved}"
        assert not service.SESSIONS_DIR.exists(), "不应顺手创建 sessions 目录"
    finally:
        service.SESSIONS_DIR = orig
    print("  ✓ 目录不存在时静默返回 0，未抛异常")


def test_empty_sessions_dir() -> None:
    """sessions 目录无 *.json → 静默返回 0，不创建归档目录。"""
    print("\n===== 3. sessions 目录为空 =====")
    assert not [p for p in SESS.iterdir() if p.is_file()], "前置：顶层已无文件"
    moved = service._archive_sessions_for_new_batch("批次-空目录")
    assert moved == 0, f"应返回 0，实际 {moved}"
    assert not (SESS / "_archive" / "批次-空目录").exists(), \
        "空归档不应创建目标目录"
    print("  ✓ 空目录静默返回 0，未创建空归档目录")


def test_merge_into_existing_dest() -> None:
    """归档目标目录已存在 → 直接并入，同名文件覆盖。"""
    print("\n===== 4. 目标目录已存在：并入覆盖 =====")
    dest = SESS / "_archive" / "批次-002"
    dest.mkdir(parents=True)
    (dest / "工厂A.json").write_text('{"old": true}', encoding="utf-8")
    (dest / "既有文件.json").write_text("{}", encoding="utf-8")

    _mk_session("工厂A.json", '{"new": true}')
    _mk_session("工厂C.json")
    moved = service._archive_sessions_for_new_batch("批次-002")
    assert moved == 2, f"归档数应为 2，实际 {moved}"
    assert (dest / "工厂A.json").read_text(encoding="utf-8") == '{"new": true}', \
        "同名文件应被新内容覆盖"
    assert (dest / "既有文件.json").is_file(), "目标目录既有文件不应被清"
    assert (dest / "工厂C.json").is_file()
    assert not (SESS / "工厂A.json").exists() and not (SESS / "工厂C.json").exists()
    print("  ✓ 并入已存在的归档目录：同名覆盖、既有文件保留")


def test_dangerous_thread_id() -> None:
    """危险批次号过滤：归档目录始终落在 SESSIONS_DIR/_archive 内。"""
    print("\n===== 5. 危险批次号过滤 =====")
    evil = "../../恶\\劣:批次*?"
    _mk_session("工厂D.json")
    moved = service._archive_sessions_for_new_batch(evil)
    assert moved == 1, f"归档数应为 1，实际 {moved}"
    archive_root = (SESS / "_archive").resolve()
    dests = [p for p in archive_root.iterdir() if p.is_dir()
             and (p / "工厂D.json").is_file()]
    assert len(dests) == 1, f"应恰好一个归档目录含该文件: {dests}"
    dest = dests[0]
    assert dest.parent == archive_root, f"归档目录逃逸出 _archive: {dest}"
    for ch in ("/", "\\", ":", "*", "?"):
        assert ch not in dest.name, f"目录名含危险字符 {ch!r}: {dest.name}"
    assert ".." not in dest.name, dest.name
    # 与进度文件共用同一套过滤：同一批次号应得到同一安全 tag
    assert dest.name == service._safe_path_tag(evil), dest.name
    print(f"  ✓ 危险批次号过滤为安全目录名: {dest.name}")


class _FakeSnap:
    """最小 StateSnapshot 替身：run_until_interrupt 守卫只看 .values。"""

    def __init__(self, values: dict) -> None:
        self.values = values


class _FakeGraph:
    """最小 graph 替身：get_state 返回固定 values，stream 立即跑完。"""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get_state(self, _cfg: dict) -> _FakeSnap:
        return _FakeSnap(self._values)

    def stream(self, *_args, **_kwargs):
        return iter([])


def test_run_until_interrupt_guard() -> None:
    """守卫：已有 checkpoint state → 跳过归档；全新 thread → 触发归档。"""
    print("\n===== 6. run_until_interrupt 归档守卫 =====")
    calls: list[str] = []
    orig_graph = service.get_graph
    orig_archive = service._archive_sessions_for_new_batch
    service._archive_sessions_for_new_batch = (
        lambda tid: calls.append(tid) or 0)
    try:
        # 已有 state（重跑/异常重启续跑）：跳过归档
        service.get_graph = lambda: _FakeGraph({"pending_factories": ["甲"]})
        r = service.run_until_interrupt("已有状态批次")
        assert r["status"] == "completed", r
        assert not calls, f"已有 state 不应触发归档: {calls}"
        print("  ✓ 已有 checkpoint state：跳过归档")

        # 全新 thread（state 为空）：触发归档
        service.get_graph = lambda: _FakeGraph({})
        r = service.run_until_interrupt("全新批次")
        assert r["status"] == "completed", r
        assert calls == ["全新批次"], f"全新批次应归档一次: {calls}"
        print("  ✓ 全新批次：归档函数被调用一次")
    finally:
        service.get_graph = orig_graph
        service._archive_sessions_for_new_batch = orig_archive


def test_in_flight_warning() -> None:
    """有其他在途批次时打醒目 warning；检查失败也不影响归档结果。"""
    print("\n===== 7. 在途批次 warning =====")

    class _Collector(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    collector = _Collector()
    service_logger = logging.getLogger("app.api.service")
    orig_list = service.list_batches
    service_logger.addHandler(collector)
    try:
        # 7a. 存在其他 pending_review/running 批次 → 醒目 warning
        service.list_batches = lambda: {"batches": [
            {"thread_id": "当前批次", "status": "running"},       # 自己，不计入
            {"thread_id": "旧批次-甲", "status": "pending_review"},
            {"thread_id": "旧批次-乙", "status": "running"},
            {"thread_id": "旧批次-丙", "status": "completed"},    # 已完成，不计入
        ]}
        service._warn_if_other_batches_in_flight("当前批次")
        warnings = [r for r in collector.records
                    if r.levelno >= logging.WARNING]
        assert warnings, "在途批次存在时应打 warning"
        msg = warnings[0].getMessage()
        assert "旧批次-甲" in msg and "旧批次-乙" in msg, msg
        assert "2 个在途批次" in msg, msg  # 当前批次自己不计入
        assert "旧批次-丙" not in msg, msg  # completed 不计入
        print(f"  ✓ 在途批次 warning：{msg}")

        # 7b. 无在途批次 → 无 warning
        collector.records.clear()
        service.list_batches = lambda: {"batches": [
            {"thread_id": "当前批次", "status": "running"},
            {"thread_id": "旧批次-丙", "status": "completed"},
        ]}
        service._warn_if_other_batches_in_flight("当前批次")
        assert not [r for r in collector.records
                    if r.levelno >= logging.WARNING], "无在途批次不应打 warning"
        print("  ✓ 无在途批次：无 warning")

        # 7c. 枚举本身失败 → 只记 warning，不抛异常
        collector.records.clear()

        def _boom():
            raise RuntimeError("db 打不开")

        service.list_batches = _boom
        service._warn_if_other_batches_in_flight("当前批次")  # 不得抛出
        warnings = [r for r in collector.records
                    if r.levelno >= logging.WARNING]
        assert warnings, "枚举失败也应留 warning 痕迹"
        print("  ✓ 枚举失败：仅记 warning，不抛异常")
    finally:
        service.list_batches = orig_list
        service_logger.removeHandler(collector)


def main() -> int:
    test_basic_archive()
    test_missing_sessions_dir()
    test_empty_sessions_dir()
    test_merge_into_existing_dest()
    test_dangerous_thread_id()
    test_run_until_interrupt_guard()
    test_in_flight_warning()
    print("\nsession_archive_test: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nsession_archive_test: FAIL — {e}")
        sys.exit(1)
