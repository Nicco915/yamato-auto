# -*- coding: utf-8 -*-
"""恢复脚本纯函数测试（scripts/restore_backup.py）。

对应实施计划《2026-10-12-数据备份机制-实施计划.md》阶段三 §3.3：
1. 快照列表扫描：tmp 造 3 份快照（含一份无 manifest 的旧格式），断言列表
   解析与排序（白名单时间戳正则、旧快照 rglob 现算大小/清单）；
2. .env 极简解析：四键覆盖 / 缺省默认 / 引号去除 / 注释与空行跳过；
3. 序号输入校验：合法 / q 退出 / 非法重问。

破坏性执行路径（_restore / _mini_snapshot / 交互流程）不进自动化测试
（红线），仅人工在数据副本上验证。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_restore_backup.py -v

隔离红线（2026-08-11 事故教训）：本文件不 import 任何 app 模块（恢复脚本
本身纯标准库、与 app 包解耦），隔离门② isolate_to_tmp 不适用；仍按惯例
在 import 前设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env 双门
兜底，防未来有人在本文件误加 app import 时打回真实路径。
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
# sys.path 挂 scripts/ 后 import 被测模块（纯标准库，不碰 app 包）
sys.path.insert(0, str(APP_ROOT / "scripts"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线，防御性兜底）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_restore_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")

import restore_backup  # noqa: E402


@pytest.fixture()
def backup_root(tmp_path: Path) -> Path:
    """tmp 下造 3 份快照：2 份新格式（含 manifest）+ 1 份无 manifest 旧格式，
    外加 1 个非法名目录与 1 个游离文件（不应列入）。"""
    root = tmp_path / "backups"
    root.mkdir()

    # 新格式快照 1（较早）
    s1 = root / "20261001_090000"
    s1.mkdir()
    (s1 / "master.db").write_bytes(b"db1")
    (s1 / "manifest.json").write_text(json.dumps({
        "reason": "startup",
        "created_at": "2026-10-01T09:00:00",
        "size_bytes": 3,
        "files": ["master.db"],
        "file_sizes": {"master.db": 3},
        "env_keys": ["DASHSCOPE_API_KEY", "MASTER_DB_PATH"],
    }), encoding="utf-8")

    # 旧格式快照（无 manifest，含 alias_map.json 与明文 .env，兼容 6 份旧快照）
    s2 = root / "20260824_094841"
    s2.mkdir()
    (s2 / "master.db").write_bytes(b"db-old")
    (s2 / "checkpoints.db").write_bytes(b"ckpt-old")
    (s2 / "alias_map.json").write_text("{}", encoding="utf-8")
    (s2 / ".env").write_text("SECRET=1\n", encoding="utf-8")

    # 新格式快照 2（最新，含 output/）
    s3 = root / "20261011_220000"
    s3.mkdir()
    (s3 / "output").mkdir()
    (s3 / "output" / "a.xlsx").write_bytes(b"xlsx")
    (s3 / "manifest.json").write_text(json.dumps({
        "reason": "manual",
        "size_bytes": 4,
        "files": ["output/"],
        "file_sizes": {"output/a.xlsx": 4},
        "env_keys": None,
    }), encoding="utf-8")

    # 非法名目录 + 游离文件：一律不列入
    (root / "not-a-snapshot").mkdir()
    (root / "stray.txt").write_text("x", encoding="utf-8")
    return root


class TestScanSnapshots:
    def test_whitelist_and_sort(self, backup_root: Path):
        snaps = restore_backup.scan_snapshots(backup_root)
        assert [s["name"] for s in snaps] == [
            "20260824_094841", "20261001_090000", "20261011_220000",
        ]

    def test_manifest_preferred(self, backup_root: Path):
        snaps = restore_backup.scan_snapshots(backup_root)
        s1 = snaps[1]
        assert s1["has_manifest"] is True
        assert s1["size_bytes"] == 3
        assert s1["files"] == ["master.db"]
        assert s1["created"].strftime("%Y-%m-%d %H:%M:%S") == "2026-10-01 09:00:00"

    def test_old_format_rglob_fallback(self, backup_root: Path):
        snaps = restore_backup.scan_snapshots(backup_root)
        old = snaps[0]
        assert old["has_manifest"] is False
        # rglob 现算：4 个文件（含隐藏 .env），大小为字节合计
        assert set(old["files"]) == {
            ".env", "alias_map.json", "checkpoints.db", "master.db"}
        assert old["size_bytes"] == len(b"db-old") + len(b"ckpt-old") \
            + len("{}") + len("SECRET=1\n")

    def test_empty_dir(self, tmp_path: Path):
        assert restore_backup.scan_snapshots(tmp_path / "nonexistent") == []
        empty = tmp_path / "empty"
        empty.mkdir()
        assert restore_backup.scan_snapshots(empty) == []


class TestParseEnvFile:
    def test_four_keys_quotes_comments(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            "# 注释行跳过\n"
            "\n"
            "MASTER_DB_PATH=app/data/m.db\n"
            'CHECKPOINT_DB_PATH="D:\\yamato\\ckpt.db"\n'
            "OUTPUT_DIR='app/output'\n"
            "BACKUP_DIR=  /tmp/ym-bak  \n"
            "lowercase_key=ignored\n"  # 键非全大写，不匹配正则
            "DASHSCOPE_API_KEY=sk-secret\n",
            encoding="utf-8",
        )
        values = restore_backup.parse_env_file(env)
        assert values["MASTER_DB_PATH"] == "app/data/m.db"
        assert values["CHECKPOINT_DB_PATH"] == "D:\\yamato\\ckpt.db"
        assert values["OUTPUT_DIR"] == "app/output"
        assert values["BACKUP_DIR"] == "/tmp/ym-bak"
        assert "lowercase_key" not in values
        # 解析取值不影响：键名清单仍应含密钥键（只记名不记值由调用方保证）
        assert values["DASHSCOPE_API_KEY"] == "sk-secret"

    def test_missing_file(self, tmp_path: Path):
        assert restore_backup.parse_env_file(tmp_path / "nope.env") == {}

    def test_parse_env_keys(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("# c\n\nFOO=1\nBAR_BAZ=x\n", encoding="utf-8")
        assert restore_backup.parse_env_keys(env) == ["FOO", "BAR_BAZ"]
        assert restore_backup.parse_env_keys(tmp_path / "nope.env") is None


class TestResolvePaths:
    def test_defaults(self, tmp_path: Path):
        paths = restore_backup.resolve_paths(
            env_path=tmp_path / "nope.env", environ={}, project_root=tmp_path)
        assert paths["BACKUP_DIR"] == tmp_path / "app/data/backups"
        assert paths["MASTER_DB_PATH"] == tmp_path / "app/data/master.db"
        assert paths["CHECKPOINT_DB_PATH"] == tmp_path / "app/data/checkpoints.db"
        assert paths["OUTPUT_DIR"] == tmp_path / "app/output"

    def test_env_overrides_dotenv(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("BACKUP_DIR=/from/dotenv\n", encoding="utf-8")
        paths = restore_backup.resolve_paths(
            env_path=env_file,
            environ={"BACKUP_DIR": "/from/environ"},
            project_root=tmp_path,
        )
        assert paths["BACKUP_DIR"] == Path("/from/environ")

    def test_dotenv_relative_resolved_against_root(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("OUTPUT_DIR=custom/out\n", encoding="utf-8")
        paths = restore_backup.resolve_paths(
            env_path=env_file, environ={}, project_root=tmp_path)
        assert paths["OUTPUT_DIR"] == tmp_path / "custom/out"


class TestParseChoice:
    def test_valid(self):
        assert restore_backup.parse_choice("1", 3) == 0
        assert restore_backup.parse_choice(" 3 ", 3) == 2

    def test_quit(self):
        assert restore_backup.parse_choice("q", 3) == -1
        assert restore_backup.parse_choice("Q", 3) == -1

    def test_invalid(self):
        assert restore_backup.parse_choice("0", 3) is None
        assert restore_backup.parse_choice("4", 3) is None
        assert restore_backup.parse_choice("abc", 3) is None
        assert restore_backup.parse_choice("", 3) is None
        assert restore_backup.parse_choice("-1", 3) is None
