# -*- coding: utf-8 -*-
"""Pinned scope 检查 + pinned 上下文注入测试。

覆盖：
1. _pinned_scope_warning 单元：
   - session_id=None → 沉默（向后兼容）
   - args 无 thread_id → 沉默
   - DB 无 pinned → 沉默
   - thread_id == pinned → 沉默
   - thread_id != pinned → 返回警告
   - DB 异常 → 降级沉默（不抛）
2. preview 集成：写工具预览含 pinned 不一致警告（验证 session_id 透传
   进 11 个 preview 函数 + 警告进 warnings）
3. pinned 上下文注入：prompts.system_prompt / executor_prompt / react_prompt
   在 session_id 有 pinned 时追加「会话上下文」段；无 pinned 时不追加。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 python3 validation/dispatcher_pinned_scope_test.py

隔离（血泪红线）：同 dispatcher_read_test.py 套路——临时目录、临时 master.db。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后）----
isolate_to_tmp("yamato_pinned_test_")


# ---------------------------------------------------------------------------
# 1. _pinned_scope_warning 单元
# ---------------------------------------------------------------------------

def unit_no_session_id():
    """session_id=None 时沉默（向后兼容）。"""
    from app.dispatcher.tools import _pinned_scope_warning
    assert _pinned_scope_warning({"thread_id": "TGT"}, None) is None
    print("  ✓ session_id=None 沉默")


def unit_no_thread_id_in_args():
    """args 无 thread_id 时沉默（set_paths 等场景）。"""
    from app.dispatcher.tools import _pinned_scope_warning
    assert _pinned_scope_warning({"paths": {}}, "any-sid") is None
    print("  ✓ 无 thread_id 沉默")


def unit_db_no_pinned():
    """DB 中该 session 未 pin 时沉默。"""
    from app.dispatcher.tools import _pinned_scope_warning
    # 全新 session：DB 行由 get_session() 建，但 pinned 默认 None
    assert _pinned_scope_warning({"thread_id": "TGT"}, "PINNED-UNIT-001") is None
    print("  ✓ 未 pin 沉默")


def unit_thread_id_matches():
    """thread_id == pinned 时沉默。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher.tools import _pinned_scope_warning

    sid = "PINNED-UNIT-MATCH"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = "BATCH-A"
        db.commit()
    try:
        assert _pinned_scope_warning({"thread_id": "BATCH-A"}, sid) is None
        print("  ✓ 一致 thread_id 沉默")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def unit_thread_id_mismatch():
    """thread_id != pinned 时返回警告。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher.tools import _pinned_scope_warning

    sid = "PINNED-UNIT-MISMATCH"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = "BATCH-A"
        db.commit()
    try:
        msg = _pinned_scope_warning({"thread_id": "BATCH-B"}, sid)
        assert msg is not None, "不一致应返回警告"
        assert "BATCH-A" in msg and "BATCH-B" in msg, f"警告应含两个批次号: {msg}"
        print(f"  ✓ 不一致 thread_id 返回警告: {msg}")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def unit_db_failure_degrades():
    """DB 异常时降级沉默（不抛）。"""
    from app.dispatcher import tools
    # 用一个 object() 让 db.get 直接抛错：模拟 DB 异常
    class _Boom:
        def __enter__(self):
            raise RuntimeError("simulated db boom")
        def __exit__(self, *a):
            return False

    # monkey-patch db.get 抛错
    from app.db.session import get_session as _get_db_session
    from contextlib import contextmanager

    @contextmanager
    def boom():
        raise RuntimeError("simulated db boom")
        yield  # unreachable

    # 保存并替换
    orig = getattr(_get_db_session, "__wrapped__", None)
    # 直接 patch tools._pinned_scope_warning 内部依赖的 db.session.get_session
    import app.db.session as _db_sess
    saved_get = _db_sess.get_session
    _db_sess.get_session = boom
    try:
        msg = tools._pinned_scope_warning({"thread_id": "BATCH-X"}, "PINNED-UNIT-BOOM")
        assert msg is None, f"DB 异常应降级沉默，实际: {msg}"
        print("  ✓ DB 异常降级沉默")
    finally:
        _db_sess.get_session = saved_get


# ---------------------------------------------------------------------------
# 2. preview 集成：写工具警告进 warnings
# ---------------------------------------------------------------------------

def integration_create_batch_preview_with_pinned():
    """create_batch 预览：args.thread_id != pinned → warnings 含警告。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher.tools import _preview_create_batch

    sid = "PINNED-INT-CREATE"
    pinned, target = "BATCH-A", "BATCH-Z"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = pinned
        db.commit()
    try:
        prev = _preview_create_batch({"thread_id": target}, sid)
        warnings = prev.get("warnings") or []
        scope_warn = [w for w in warnings
                      if "已 pin" in w or "已绑定" in w]
        assert scope_warn, f"预览应含 pinned scope 警告: {warnings}"
        assert pinned in scope_warn[0] and target in scope_warn[0], \
            f"警告应含两个批次号: {scope_warn}"
        print(f"  ✓ create_batch 预览含 pinned 警告: {scope_warn[0]}")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def integration_set_paths_no_thread_id_no_warn():
    """set_paths 无 thread_id → pinned scope 沉默。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher.tools import _preview_set_paths

    sid = "PINNED-INT-SETPATHS"
    pinned = "BATCH-A"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = pinned
        db.commit()
    try:
        prev = _preview_set_paths({"paths": {"upstream_root": "/tmp/x"}}, sid)
        warnings = prev.get("warnings") or []
        scope_warn = [w for w in warnings
                      if "已 pin" in w or "已绑定" in w]
        assert not scope_warn, \
            f"无 thread_id 应沉默，实际警告: {scope_warn}"
        print("  ✓ set_paths 无 thread_id → 沉默（无 pinned 警告）")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


# ---------------------------------------------------------------------------
# 3. pinned 上下文注入：prompts.* 三函数
# ---------------------------------------------------------------------------

def injection_system_prompt_with_pinned():
    """system_prompt(phase, session_id) 有 pinned → 含「会话上下文」段。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher import prompts

    sid = "PINNED-INJ-SYSPROMPT"
    pinned = "BATCH-CTX-001"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = pinned
        db.commit()
    try:
        sp = prompts.system_prompt(2, sid)
        assert "会话上下文" in sp, f"system_prompt 应含「会话上下文」段: head={sp[:300]}"
        assert pinned in sp, f"system_prompt 应含 pinned 批次号: head={sp[:300]}"
        # 无 session_id 时应不含该段
        sp_none = prompts.system_prompt(2)
        assert "会话上下文" not in sp_none, "session_id=None 时应不含上下文段"
        print(f"  ✓ system_prompt(2, sid) 注入 pinned 上下文段（含 {pinned}）")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def injection_executor_prompt_with_pinned():
    """executor_prompt(phase, session_id) 有 pinned → 含上下文段。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher import prompts

    sid = "PINNED-INJ-EXEC"
    pinned = "BATCH-CTX-002"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = pinned
        db.commit()
    try:
        sp = prompts.executor_prompt(2, sid)
        assert "会话上下文" in sp, "executor_prompt 应含「会话上下文」段"
        assert pinned in sp, "executor_prompt 应含 pinned 批次号"
        print(f"  ✓ executor_prompt(2, sid) 注入 pinned 上下文段")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def injection_react_prompt_with_pinned():
    """react_prompt(phase, session_id) 有 pinned → 含上下文段。"""
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session
    from app.dispatcher import prompts

    sid = "PINNED-INJ-REACT"
    pinned = "BATCH-CTX-003"
    with _get_db_session() as db:
        row = db.get(_ChatSessionOrm, sid)
        if row is None:
            db.add(_ChatSessionOrm(session_id=sid))
            db.commit()
        row = db.get(_ChatSessionOrm, sid)
        row.pinned_thread_id = pinned
        db.commit()
    try:
        sp = prompts.react_prompt(2, sid)
        assert "会话上下文" in sp, "react_prompt 应含「会话上下文」段"
        assert pinned in sp, "react_prompt 应含 pinned 批次号"
        print(f"  ✓ react_prompt(2, sid) 注入 pinned 上下文段")
    finally:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, sid)
            if row:
                row.pinned_thread_id = None
                db.commit()


def injection_no_prompt_no_segment():
    """session_id=None 时三个 prompt 都不应含「会话上下文」段。"""
    from app.dispatcher import prompts

    for fn_name in ("system_prompt", "executor_prompt", "react_prompt"):
        sp = getattr(prompts, fn_name)(2)
        assert "会话上下文" not in sp, \
            f"{fn_name}(2) session_id=None 时应不含上下文段"
    print("  ✓ session_id=None → 三 prompt 不含上下文段")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

UNIT_CASES = [
    ("1.1 session_id=None 沉默", unit_no_session_id),
    ("1.2 无 thread_id 沉默", unit_no_thread_id_in_args),
    ("1.3 未 pin 沉默", unit_db_no_pinned),
    ("1.4 thread_id 一致沉默", unit_thread_id_matches),
    ("1.5 thread_id 不一致警告", unit_thread_id_mismatch),
    ("1.6 DB 异常降级沉默", unit_db_failure_degrades),
]

INTEG_CASES = [
    ("2.1 create_batch 预览含 pinned 警告", integration_create_batch_preview_with_pinned),
    ("2.2 set_paths 无 thread_id 沉默", integration_set_paths_no_thread_id_no_warn),
]

INJ_CASES = [
    ("3.1 system_prompt 注入 pinned", injection_system_prompt_with_pinned),
    ("3.2 executor_prompt 注入 pinned", injection_executor_prompt_with_pinned),
    ("3.3 react_prompt 注入 pinned", injection_react_prompt_with_pinned),
    ("3.4 session_id=None 三 prompt 不含段", injection_no_prompt_no_segment),
]


def main() -> int:
    print("===== 1. _pinned_scope_warning 单元 =====")
    results = []
    for name, fn in UNIT_CASES:
        try:
            fn()
            results.append((name, True, ""))
            print(f"[PASS] {name}\n")
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}\n")

    print("===== 2. preview 集成（写工具警告） =====")
    for name, fn in INTEG_CASES:
        try:
            fn()
            results.append((name, True, ""))
            print(f"[PASS] {name}\n")
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}\n")

    print("===== 3. prompts 上下文注入 =====")
    for name, fn in INJ_CASES:
        try:
            fn()
            results.append((name, True, ""))
            print(f"[PASS] {name}\n")
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}\n")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    if passed == len(results):
        print("🎉 pinned scope + pinned 上下文注入测试全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())