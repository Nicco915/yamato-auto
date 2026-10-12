# -*- coding: utf-8 -*-
"""数据备份核心模块测试（app/orchestrator/backup.py + app/config.py）。

对应《2026-10-12-数据备份机制-实施计划.md》阶段一 §1.4（含 2026-10-12
二次确认修订：.env 排除备份范围，manifest 增 env_keys）：
1. create_snapshot 全量：四类内容（两 DB + kb_extension.json + output/）
   + 两份清单齐全，无 .env，env_keys 键名正确，WAL 运行中数据已合并；
2. 缺文件宽容：无 .env（env_keys=null）/无 kb/无 output/无 DB → 不抛，
   manifest 记 skipped；
3. 保留策略：22 个合法时间戳目录 + 非法名目录 + 游离文件 → 剩 20 个最新，
   其余不动；
4. 半成品清理：_backup_db 抛异常 → 快照目录删除、异常外抛；
5. trigger_async：进行中重入 False、60 秒节流、manual 绕过节流、
   线程异常不外抛；
6. maybe_backup_on_startup：无快照触发 / 13h 前快照触发 / 1h 前不触发；
7. settings：backup_dir 默认/覆盖两态。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_backup.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
  备份默认目录挂在 master.db 同级 backups/ 下，随隔离自动落 tmp；
  本文件绝不显式把 BACKUP_DIR 指向真实目录。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_backup_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.config import Settings, get_settings  # noqa: E402
from app.orchestrator import backup  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_backup_test_")


@pytest.fixture(autouse=True)
def _backup_tmp_root(tmp_path):
    """每个用例独立备份根目录（tmp_path 下）；结束后恢复 settings 单例字段
    并清 backup 模块级状态（红线 2：settings 单例钉根），防全量混跑串味。
    """
    s = get_settings()
    original = s.backup_dir
    root = tmp_path / "backups"
    root.mkdir()
    s.backup_dir = str(root)
    with backup._state_lock:
        backup._running = False
        backup._last_success_ts = 0.0
    try:
        yield root
    finally:
        s.backup_dir = original
        with backup._state_lock:
            backup._running = False
            backup._last_success_ts = 0.0


def _make_db(path: Path, rows: int, *, wal: bool = False) -> sqlite3.Connection:
    """造一个带 t 表 + rows 行的 sqlite 库；wal=True 时保持写入连接不关闭
    （返回该连接，调用方负责关闭），模拟服务运行中的 WAL 库。"""
    conn = sqlite3.connect(str(path))
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO t (v) VALUES (?)", (f"row{i}",))
    conn.commit()
    if wal:
        return conn
    conn.close()
    return None


def _count_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def _join_backup_worker(timeout: float = 10.0) -> None:
    """等 backup-worker 线程结束（超时保护，宁 FAIL 不挂死）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = next((x for x in threading.enumerate()
                  if x.name == "backup-worker"), None)
        if t is None:
            return
        t.join(timeout=0.2)
    raise AssertionError("后台线程 backup-worker 超时未结束")


# ---- 1. create_snapshot 全量 ----

def test_create_snapshot_full(_backup_tmp_root, tmp_path, monkeypatch):
    s = get_settings()
    _make_db(s.master_db_abs, 3)
    # checkpoints 显式 WAL 且保持写入连接不关闭（模拟服务运行中）
    wal_conn = _make_db(s.checkpoint_db_abs, 5, wal=True)
    # 假 kb_extension.json（真实路径不隔离，monkeypatch 到 tmp）
    kb = tmp_path / "kb_extension.json"
    kb.write_text('{"kb": 1}\n', encoding="utf-8")
    monkeypatch.setattr(backup, "_kb_extension_path", lambda: kb)
    # 假 .env（含键/注释/空行）：YAMATO_DOTENV_PATH 指向之，绝不拷进快照
    fake_env = tmp_path / ".env"
    fake_env.write_text(
        "# 注释行\n\nAAA_KEY=secret-1\nBBB_KEY=\"secret-2\"\n", encoding="utf-8")
    monkeypatch.setenv("YAMATO_DOTENV_PATH", str(fake_env))
    # 假 output/（两个文件；隔离后 output_dir_abs 已落 tmp）
    (s.output_dir_abs / "a.txt").write_text("a", encoding="utf-8")
    sub = s.output_dir_abs / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("bb", encoding="utf-8")

    try:
        result = backup.create_snapshot("test")
    finally:
        wal_conn.close()

    assert result["ok"] is True
    snap = Path(result["snapshot"])
    assert snap.parent == _backup_tmp_root
    assert backup.SNAPSHOT_NAME_RE.match(snap.name)

    # 四类内容 + 两份清单齐全，且无 .env
    names = {p.name for p in snap.iterdir()}
    assert {"master.db", "checkpoints.db", "kb_extension.json",
            "output", "external_paths.json", "manifest.json"} <= names
    assert ".env" not in names
    assert (snap / "output" / "a.txt").read_text() == "a"
    assert (snap / "output" / "sub" / "b.txt").read_text() == "bb"
    # 不拷 WAL 伴生文件
    assert not (snap / "checkpoints.db-wal").exists()

    # 快照 DB 可独立打开且行数一致（WAL 内未 checkpoint 数据已合并）
    assert _count_rows(snap / "master.db") == 3
    assert _count_rows(snap / "checkpoints.db") == 5

    # manifest 字段齐全，env_keys 只记键名
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reason"] == "test"
    assert manifest["created_at"]
    assert manifest["elapsed_s"] >= 0
    assert manifest["size_bytes"] > 0
    assert manifest["files"] == [
        "master.db", "checkpoints.db", "kb_extension.json", "output/"]
    assert manifest["skipped"] == []
    assert manifest["env_keys"] == ["AAA_KEY", "BBB_KEY"]
    assert "master.db" in manifest["file_sizes"]
    assert "output/a.txt" in manifest["file_sizes"]

    # external_paths 字段齐全
    ext = json.loads((snap / "external_paths.json").read_text(encoding="utf-8"))
    for key in ("master_db_path", "checkpoint_db_path", "output_dir",
                "upstream_root", "downstream_file_path", "watch_dir",
                "alias_map_path", "backup_dir"):
        assert key in ext
    assert ext["master_db_path"]["exists"] is True

    assert result["size_bytes"] == manifest["size_bytes"]


# ---- 2. 缺文件宽容 ----

def test_create_snapshot_missing_files(_backup_tmp_root, tmp_path, monkeypatch):
    s = get_settings()
    # 连 DB 都不存在；kb / output / .env 也全部不存在
    monkeypatch.setattr(s, "master_db_path", str(tmp_path / "no_master.db"))
    monkeypatch.setattr(s, "checkpoint_db_path", str(tmp_path / "no_ckpt.db"))
    monkeypatch.setattr(s, "output_dir", str(tmp_path / "no_output"))
    monkeypatch.setattr(
        backup, "_kb_extension_path", lambda: tmp_path / "no_kb.json")
    monkeypatch.setenv("YAMATO_DOTENV_PATH", str(tmp_path / "no_env"))

    result = backup.create_snapshot("test-missing")

    assert result["ok"] is True
    snap = Path(result["snapshot"])
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == []
    assert set(manifest["skipped"]) == {
        "no_master.db", "no_ckpt.db", "no_kb.json", "output/"}
    assert manifest["env_keys"] is None  # .env 不存在
    # 只剩两份清单
    assert {p.name for p in snap.iterdir()} == {
        "external_paths.json", "manifest.json"}


# ---- 3. 保留策略 ----

def test_prune_keep_latest_20(_backup_tmp_root):
    base = datetime(2026, 1, 1)
    names = [(base + timedelta(seconds=i)).strftime("%Y%m%d_%H%M%S")
             for i in range(22)]
    for name in names:
        (_backup_tmp_root / name).mkdir()
    # 非法名目录与游离文件：一律不动
    (_backup_tmp_root / "notes_keep").mkdir()
    stray = _backup_tmp_root / "stray.txt"
    stray.write_text("x", encoding="utf-8")

    backup._prune()

    remaining = {p.name for p in _backup_tmp_root.iterdir() if p.is_dir()
                 and backup.SNAPSHOT_NAME_RE.match(p.name)}
    assert remaining == set(names[2:])  # 最旧 2 份被删，剩最新 20 份
    assert (_backup_tmp_root / "notes_keep").is_dir()
    assert stray.is_file()

    # 不足配额时不动
    backup._prune()
    assert len([p for p in _backup_tmp_root.iterdir()
                if p.is_dir() and backup.SNAPSHOT_NAME_RE.match(p.name)]) == 20


# ---- 4. 半成品清理 ----

def test_create_snapshot_cleanup_on_failure(_backup_tmp_root, monkeypatch):
    def _boom(src, dst):
        raise RuntimeError("模拟备份中途失败")

    monkeypatch.setattr(backup, "_backup_db", _boom)
    with pytest.raises(RuntimeError, match="模拟备份中途失败"):
        backup.create_snapshot("test-fail")
    # 半成品快照目录已删除
    assert [p for p in _backup_tmp_root.iterdir()
            if backup.SNAPSHOT_NAME_RE.match(p.name)] == []


# ---- 5. trigger_async ----

def test_trigger_async_reentry_and_throttle(_backup_tmp_root, monkeypatch):
    # 进行中重入 → False（不起线程）
    monkeypatch.setattr(backup, "_running", True)
    assert backup.trigger_async("test") is False

    monkeypatch.setattr(backup, "_running", False)
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "create_snapshot",
        lambda reason: calls.append(reason) or {"ok": True})

    # 距上次成功不足 60 秒 → 自动触发被节流
    monkeypatch.setattr(backup, "_last_success_ts", time.monotonic())
    assert backup.trigger_async("auto") is False
    # manual=True 不受节流限制
    assert backup.trigger_async("manual", manual=True) is True
    _join_backup_worker()
    assert calls == ["manual"]

    # 无节流时正常触发
    monkeypatch.setattr(backup, "_last_success_ts", 0.0)
    assert backup.trigger_async("auto") is True
    _join_backup_worker()
    assert calls == ["manual", "auto"]
    # 成功后 _last_success_ts 已更新
    assert backup._last_success_ts > 0


def test_trigger_async_worker_exception_not_raised(_backup_tmp_root, monkeypatch):
    def _boom(reason):
        raise RuntimeError("线程内模拟失败")

    monkeypatch.setattr(backup, "create_snapshot", _boom)
    assert backup.trigger_async("test-exc") is True  # 触发动作本身不抛
    _join_backup_worker()  # 线程异常不外抛（只记 error 日志）
    # 失败后 _running 复位，可再次触发
    assert backup._running is False


# ---- 6. maybe_backup_on_startup ----

def test_maybe_backup_on_startup(_backup_tmp_root, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "trigger_async", lambda reason: calls.append(reason) or True)

    # 无快照 → 触发
    assert backup.maybe_backup_on_startup() is True
    assert calls == ["startup"]

    # 伪造 13 小时前快照 → 触发
    old = _backup_tmp_root / (
        datetime.now() - timedelta(hours=13)).strftime("%Y%m%d_%H%M%S")
    old.mkdir()
    assert backup.maybe_backup_on_startup() is True
    assert calls == ["startup", "startup"]

    # 换成 1 小时前快照 → 不触发
    import shutil
    shutil.rmtree(old)
    recent = _backup_tmp_root / (
        datetime.now() - timedelta(hours=1)).strftime("%Y%m%d_%H%M%S")
    recent.mkdir()
    assert backup.maybe_backup_on_startup() is False
    assert calls == ["startup", "startup"]

    # latest_snapshot_time / list_snapshots 口径顺带验证
    assert backup.latest_snapshot_time() == datetime.strptime(
        recent.name, "%Y%m%d_%H%M%S")
    assert [p.name for p in backup.list_snapshots()] == [recent.name]


# ---- 7. settings：backup_dir 默认/覆盖两态 ----

def test_settings_backup_dir_default_and_override(tmp_path):
    # 默认：空串 → master.db 同级 backups/
    # （_env_file=None 防真实 .env 干扰；环境变量已由隔离门②指向 tmp）
    s = Settings(_env_file=None)
    assert s.backup_dir == ""
    assert s.backup_dir_abs == s.master_db_abs.parent / "backups"
    # 覆盖：显式字段（等价于 env BACKUP_DIR 注入）
    custom = tmp_path / "custom-backups"
    s2 = Settings(_env_file=None, backup_dir=str(custom))
    assert s2.backup_dir_abs == custom
