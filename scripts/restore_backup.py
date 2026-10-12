#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""备份快照交互式恢复脚本（2026-10-12 数据备份机制，实施计划阶段三 §3.1）。

【破坏性操作】本脚本会覆盖 master.db / checkpoints.db / kb_extension.json /
output/。**必须先停止 uvicorn**（关闭 start.bat 窗口）再运行；交互流程内置
「输 yes 才继续」确认门，非 yes 不动任何东西。

【自包含红线】纯标准库 + pathlib，**不 import 任何 app 包**——灾难恢复场景
下 app 包本身可能已损坏。路径自解析（BACKUP_DIR / MASTER_DB_PATH /
CHECKPOINT_DB_PATH / OUTPUT_DIR 四键：先环境变量、后 .env 极简正则解析
去引号、缺省走默认相对路径），不依赖 app.config。

【恢复口径】
- 恢复前自动做一次「兜底快照」（_mini_snapshot，与
  app/orchestrator/backup.py 的 create_snapshot 有约 30 行**有意重复**——
  灾难场景 app 包可能不可 import，不接受 import 复用的简化）；
- 覆盖目标 DB 前先删其 -wal/-shm 伴生文件（否则旧 WAL 会污染还原的库）；
- output/ 追加式还原（copytree(dirs_exist_ok=True)，不删现有多余文件，
  防误杀恢复后新产出）；
- 兼容 6 份旧手工快照（无 manifest.json → rglob 现算清单；含 alias_map.json
  → 一并还原到 app/alias_map.json）；
- **永不自动还原 .env**（防泄露设计）：快照 manifest 有 env_keys 时打印
  「请参照 .env.example 重建 .env」及快照时生效的键名清单；旧格式快照内
  含明文 .env 时只提示手动查看路径，不自动还原；
- 每步失败打印错误并中止（已完成的步骤不回滚），提示兜底快照位置。

用法（在 app/ 目录下）：
  python3 scripts/restore_backup.py

自动化测试只覆盖本文件的纯函数（tests/test_restore_backup.py），
破坏性执行路径不进自动化测试（红线）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 快照目录名白名单：仅 ^\d{8}_\d{6}$ 参与列表/选择（与 orchestrator.backup 同口径）
SNAPSHOT_NAME_RE = re.compile(r"^\d{8}_\d{6}$")

# .env 极简解析正则：KEY=VALUE 行（键全大写/数字/下划线），注释/空行跳过
_ENV_LINE_RE = re.compile(r"^([A-Z0-9_]+)\s*=\s*(.*)$")
# .env 键名解析正则（只取键名，兜底快照 manifest.env_keys 用，绝不记值）
_ENV_KEY_RE = re.compile(r"^([A-Z0-9_]+)=")

# 四个可覆盖路径键及其默认相对路径（基于 PROJECT_ROOT 解析）
DEFAULT_PATHS = {
    "BACKUP_DIR": "app/data/backups",
    "MASTER_DB_PATH": "app/data/master.db",
    "CHECKPOINT_DB_PATH": "app/data/checkpoints.db",
    "OUTPUT_DIR": "app/output",
}

# kb_extension.json / alias_map.json 目标路径（与 app 内硬编码口径一致，
# 无配置项，灾难场景也按默认位置还原）
KB_EXTENSION_REL = "app/data/kb_extension.json"
ALIAS_MAP_REL = "app/alias_map.json"


# ---------------------------------------------------------------------------
# 纯函数（模块级可 import，自动化测试只测这部分）
# ---------------------------------------------------------------------------

def parse_env_file(env_path: Path) -> dict[str, str]:
    """极简解析 .env：返回 {键: 值}（值去首尾空白、去成对单/双引号）。

    跳过注释行（# 开头）与空行；文件不存在返回 {}。
    不展开变量、不处理转义——只够读四个路径覆盖键。
    """
    env_path = Path(env_path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        values[key] = raw
    return values


def parse_env_keys(env_path: Path) -> list[str] | None:
    """从 .env 解析键名清单（只记键名绝不记值）；文件不存在返回 None。"""
    env_path = Path(env_path)
    if not env_path.exists():
        return None
    keys: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_KEY_RE.match(line)
        if m:
            keys.append(m.group(1))
    return keys


def resolve_path_setting(
    key: str,
    env_values: dict[str, str],
    environ: dict[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    """单个路径键解析：环境变量 > .env 值 > 默认相对路径。

    相对路径基于 project_root 解析；绝对路径（含 Windows 盘符/UNC）原样用。
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(key) or env_values.get(key) or DEFAULT_PATHS[key]
    p = Path(raw)
    return p if p.is_absolute() else Path(project_root) / p


def resolve_paths(
    env_path: Path | None = None,
    environ: dict[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Path]:
    """四键一次解析：返回 {键: 绝对路径}。env_path 缺省为项目根 .env。"""
    if env_path is None:
        env_path = Path(project_root) / ".env"
    env_values = parse_env_file(env_path)
    return {
        key: resolve_path_setting(key, env_values, environ, project_root)
        for key in DEFAULT_PATHS
    }


def snapshot_info(snap_dir: Path) -> dict:
    """单份快照的展示信息：name/path/created/size_bytes/files。

    优先读 manifest.json（新格式）；缺失则 rglob 现算（兼容旧手工快照）。
    """
    snap_dir = Path(snap_dir)
    manifest_path = snap_dir / "manifest.json"
    files: list[str] = []
    size_bytes = 0
    has_manifest = False
    if manifest_path.exists():
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8", errors="replace"))
            has_manifest = True
            size_bytes = int(manifest.get("size_bytes") or 0)
            file_sizes = manifest.get("file_sizes") or {}
            if file_sizes:
                files = sorted(file_sizes.keys())
            else:
                files = list(manifest.get("files") or [])
        except (ValueError, OSError):
            has_manifest = False  # manifest 损坏则回退现算
    if not has_manifest:
        for p in sorted(snap_dir.rglob("*")):
            if p.is_file():
                size_bytes += p.stat().st_size
                files.append(p.relative_to(snap_dir).as_posix())
    try:
        created = datetime.strptime(snap_dir.name, "%Y%m%d_%H%M%S")
    except ValueError:
        created = None
    return {
        "name": snap_dir.name,
        "path": snap_dir,
        "created": created,
        "size_bytes": size_bytes,
        "files": files,
        "has_manifest": has_manifest,
    }


def scan_snapshots(backup_root: Path) -> list[dict]:
    """扫描备份根，返回白名单时间戳快照信息列表（按目录名升序）。

    其他目录/文件一律不列入；备份根不存在返回 []。
    """
    backup_root = Path(backup_root)
    if not backup_root.exists():
        return []
    return [
        snapshot_info(p)
        for p in sorted(backup_root.iterdir(), key=lambda p: p.name)
        if p.is_dir() and SNAPSHOT_NAME_RE.match(p.name)
    ]


def parse_choice(raw: str, count: int) -> int | None:
    """序号输入校验：返回 0 基于索引；'q'/'Q' 返回 -1；非法返回 None。

    count 为快照总数（合法序号 1..count）。
    """
    raw = raw.strip()
    if raw.lower() == "q":
        return -1
    if not raw.isdigit():
        return None
    idx = int(raw)
    if 1 <= idx <= count:
        return idx - 1
    return None


# ---------------------------------------------------------------------------
# 以下为执行路径（破坏性操作，不进自动化测试）
# ---------------------------------------------------------------------------

def _mini_snapshot(paths: dict[str, Path]) -> Path | None:
    """恢复前兜底快照：对当前现场做一份迷你快照，返回快照目录。

    【有意重复】与 app/orchestrator/backup.py 的 create_snapshot 重复约 30 行
    快照逻辑是有意为之——灾难恢复时 app 包可能已损坏不可 import，本脚本
    必须纯标准库自包含（实施计划决策 9 / 风险 7）。

    口径：两 DB 走 sqlite3 在线备份 API（WAL 安全，产出已合并 wal）；
    kb_extension/output 直接 copy2/copytree；.env 同样不拷，只把键名清单
    写进迷你 manifest（env_keys，只记键名绝不记值）。源不存在则跳过。
    目录名同时间戳格式，落同一备份根。
    """
    backup_root = paths["BACKUP_DIR"]
    backup_root.mkdir(parents=True, exist_ok=True)
    snap_dir: Path | None = None
    for _ in range(3):
        candidate = backup_root / datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            candidate.mkdir(exist_ok=False)
            snap_dir = candidate
            break
        except FileExistsError:
            time.sleep(1)
    if snap_dir is None:
        print("兜底快照失败：快照目录撞名重试 3 次仍失败")
        return None

    files: list[str] = []
    skipped: list[str] = []
    try:
        for key in ("MASTER_DB_PATH", "CHECKPOINT_DB_PATH"):
            src = paths[key]
            if not src.exists():
                skipped.append(src.name)
                continue
            dst = snap_dir / src.name
            src_conn = sqlite3.connect(str(src))
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            files.append(src.name)

        kb = PROJECT_ROOT / KB_EXTENSION_REL
        if kb.exists():
            shutil.copy2(kb, snap_dir / kb.name)
            files.append(kb.name)
        else:
            skipped.append(kb.name)

        out_dir = paths["OUTPUT_DIR"]
        if out_dir.exists():
            shutil.copytree(out_dir, snap_dir / "output")
            files.append("output/")
        else:
            skipped.append("output/")

        total = 0
        file_sizes: dict[str, int] = {}
        for p in sorted(snap_dir.rglob("*")):
            if p.is_file():
                size = p.stat().st_size
                file_sizes[p.relative_to(snap_dir).as_posix()] = size
                total += size
        manifest = {
            "reason": "pre_restore_fallback",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "size_bytes": total,
            "files": files,
            "skipped": skipped,
            "file_sizes": file_sizes,
            "env_keys": parse_env_keys(PROJECT_ROOT / ".env"),
        }
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(snap_dir, ignore_errors=True)
        print(f"兜底快照失败：{e}")
        return None
    return snap_dir


def _fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f}MB"
    return f"{size_bytes / 1024:.1f}KB"


def _print_snapshot_list(snaps: list[dict]) -> None:
    print("\n可用快照：")
    for i, info in enumerate(snaps, 1):
        created = info["created"].strftime("%Y-%m-%d %H:%M:%S") \
            if info["created"] else info["name"]
        print(f"  [{i}] {created}  大小 {_fmt_size(info['size_bytes'])}"
              f"{'  (旧格式，无 manifest)' if not info['has_manifest'] else ''}")
        for f in info["files"]:
            print(f"        - {f}")


def _confirm_gate(snap: dict, paths: dict[str, Path]) -> bool:
    """确认门：打印将覆盖的目标清单 + 停 uvicorn 警告，输 yes 才返回 True。"""
    snap_dir: Path = snap["path"]
    print("\n即将从快照恢复，以下目标将被覆盖/追加：")
    targets = []
    if (snap_dir / paths["MASTER_DB_PATH"].name).exists() or \
            (snap_dir / "master.db").exists():
        targets.append(f"覆盖数据库: {paths['MASTER_DB_PATH']}")
    if (snap_dir / "checkpoints.db").exists():
        targets.append(f"覆盖数据库: {paths['CHECKPOINT_DB_PATH']}")
    if (snap_dir / "kb_extension.json").exists():
        targets.append(f"覆盖文件: {PROJECT_ROOT / KB_EXTENSION_REL}")
    if (snap_dir / "alias_map.json").exists():
        targets.append(f"覆盖文件: {PROJECT_ROOT / ALIAS_MAP_REL}（旧快照兼容）")
    if (snap_dir / "output").exists():
        targets.append(f"追加目录: {paths['OUTPUT_DIR']}（不删现有多余文件）")
    for t in targets:
        print(f"  - {t}")
    print("\n!!! 请先停止 uvicorn（关闭 start.bat 窗口）再继续 !!!")
    answer = input("确认恢复？输入 yes 继续，其他任意输入取消: ").strip()
    return answer == "yes"


def _restore(snap: dict, paths: dict[str, Path]) -> Path | None:
    """执行恢复。返回兜底快照目录（失败时用于提示）；任一步失败打印并中止。"""
    snap_dir: Path = snap["path"]

    print("\n[1/4] 恢复前兜底快照（当前现场）...")
    fallback = _mini_snapshot(paths)
    if fallback is None:
        print("中止：兜底快照未成功，为避免风险不继续恢复。")
        return None
    print(f"      兜底快照已落盘: {fallback}")

    try:
        print("[2/4] 还原数据库（先删 -wal/-shm 伴生文件）...")
        restored_db = False
        for key in ("MASTER_DB_PATH", "CHECKPOINT_DB_PATH"):
            dst = paths[key]
            src = snap_dir / dst.name
            if not src.exists():
                print(f"      跳过（快照内无 {dst.name}）")
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm"):
                sidecar = dst.parent / (dst.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
                    print(f"      已删除伴生文件: {sidecar}")
            shutil.copy2(src, dst)
            print(f"      已还原: {dst}")
            restored_db = True
        if not restored_db:
            print("      警告：快照内没有任何数据库文件")

        print("[3/4] 还原配置/映射文件...")
        kb_src = snap_dir / "kb_extension.json"
        if kb_src.exists():
            kb_dst = PROJECT_ROOT / KB_EXTENSION_REL
            kb_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kb_src, kb_dst)
            print(f"      已还原: {kb_dst}")
        else:
            print("      跳过（快照内无 kb_extension.json）")
        alias_src = snap_dir / "alias_map.json"
        if alias_src.exists():
            # 旧手工快照兼容：alias_map.json 一并还原（新快照不含此文件）
            alias_dst = PROJECT_ROOT / ALIAS_MAP_REL
            alias_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(alias_src, alias_dst)
            print(f"      已还原（旧快照兼容）: {alias_dst}")

        print("[4/4] 还原输出目录（追加式，不删现有多余文件）...")
        out_src = snap_dir / "output"
        if out_src.exists():
            out_dst = paths["OUTPUT_DIR"]
            out_dst.mkdir(parents=True, exist_ok=True)
            shutil.copytree(out_src, out_dst, dirs_exist_ok=True)
            print(f"      已追加还原: {out_dst}")
        else:
            print("      跳过（快照内无 output/）")
    except Exception as e:  # noqa: BLE001
        print(f"\n恢复失败：{e}")
        print("已完成的步骤不回滚。如需回退，请用兜底快照重新恢复：")
        print(f"  {fallback}")
        return fallback

    # .env 永不自动还原（防泄露设计）
    manifest_path = snap_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8", errors="replace"))
            env_keys = manifest.get("env_keys")
            if env_keys:
                print("\n请参照 .env.example 重建 .env，快照时生效的键："
                      + ", ".join(env_keys))
        except (ValueError, OSError):
            pass
    old_env = snap_dir / ".env"
    if old_env.exists():
        print(f"\n快照内含旧格式 .env，如需找回密钥请手动查看 {old_env}，"
              "本脚本不自动还原")

    print("\n恢复完成，请重新启动服务。")
    print(f"兜底快照（恢复前现场）: {fallback}")
    return fallback


def main() -> int:
    print("=== 雅玛多备份恢复 ===")
    print("【警告】本操作会覆盖数据库与输出文件，请先停止 uvicorn。\n")

    paths = resolve_paths()
    backup_root = paths["BACKUP_DIR"]
    print(f"备份目录: {backup_root}")

    snaps = scan_snapshots(backup_root)
    if not snaps:
        print("备份目录为空（无可用快照），退出。")
        return 0

    _print_snapshot_list(snaps)

    # 选择序号：q 退出，非法重问
    while True:
        raw = input(f"\n输入序号 (1-{len(snaps)}，q 退出): ")
        choice = parse_choice(raw, len(snaps))
        if choice == -1:
            print("已取消，未做任何修改。")
            return 0
        if choice is None:
            print("输入无效，请重新输入。")
            continue
        break
    snap = snaps[choice]
    print(f"\n已选择快照: {snap['name']}")

    if not _confirm_gate(snap, paths):
        print("已取消，未做任何修改。")
        return 0

    result = _restore(snap, paths)
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
