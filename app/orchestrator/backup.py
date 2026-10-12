# -*- coding: utf-8 -*-
"""数据备份核心模块（2026-10-12 数据备份机制，实施计划阶段一 §1.2）。

快照内容（四类内容 + 两份清单）：
- master.db / checkpoints.db：走 sqlite3 在线备份 API（Connection.backup），
  WAL 运行中安全，产出的快照已合并 wal，不含 -wal/-shm 伴生文件；备份后
  对快照内 .db 做 PRAGMA integrity_check，失败删整份快照并抛错；
- kb_extension.json：不存在则跳过并记入 manifest.skipped；
- output/ 全量：目录不存在则跳过；
- external_paths.json：外部路径当前生效值 + 存在性布尔（灾难恢复核对用）；
- manifest.json：reason/created_at/elapsed_s/逐文件字节数/总大小/env_keys。
  env_keys 从生效 .env 解析键名清单（只记键名绝不记值；.env 不存在记
  null）——.env 本身不进快照（2026-10-12 二次确认：防密钥随快照外泄）。

不备份：.env、sessions 缓存、logs、data/cache/（含 excel_pdf）。

快照命名 {YYYYMMDD_HHMMSS}/，默认落 master.db 同级 backups/（BACKUP_DIR
可覆盖）；同一秒撞名休眠 1 秒重取时间戳，最多 3 次；保留最近 20 份
（白名单时间戳正则目录才参与排序/删除，其他目录/文件一律不动；既有
6 份旧手工快照命名同格式，自然计入配额）。

触发点三处：启动超 12h（maybe_backup_on_startup）/ 批次结案
（batch_store.update_status 挂钩，阶段二接线）/ 看板手动端点（阶段二），
全部共用 create_snapshot；自动触发走 trigger_async（全局锁单备份串行 +
60 秒节流 + daemon 线程 backup-worker）。

本模块只 import app.config + 标准库（不碰 app.db / app.api），保证
batch_store 可函数内延迟 import 而无循环依赖。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

# 快照目录名白名单：仅 ^\d{8}_\d{6}$ 参与排序/删除，其他目录/文件一律不动
SNAPSHOT_NAME_RE = re.compile(r"^\d{8}_\d{6}$")
# 保留最近 20 份快照
RETAIN_COUNT = 20
# 自动触发节流：距上次成功备份不足 60 秒跳过（手动端点不受此限）
AUTO_MIN_INTERVAL_SECONDS = 60
# 启动超期判定：最新快照距今超过 12 小时则触发
STARTUP_MAX_AGE_HOURS = 12
# 以上均为常量（不引入同名 env 覆盖），避免配置面膨胀

# .env 键名解析正则（只取键名，绝不取值）；注释/空行跳过
_ENV_KEY_RE = re.compile(r"^([A-Z0-9_]+)=")

# create_snapshot 全局锁：同一时刻只允许一份备份（IO 密集，串行足够）
_snapshot_lock = threading.Lock()
# trigger_async 状态：_running 进行中标尺、_last_success_ts 上次成功 monotonic 时间
_state_lock = threading.Lock()
_running = False
_last_success_ts: float = 0.0


def _env_file_path() -> Path:
    """生效 .env 路径：优先 YAMATO_DOTENV_PATH（测试隔离口径，与 llm_client
    一致），缺省项目根 .env。"""
    override = os.environ.get("YAMATO_DOTENV_PATH")
    return Path(override) if override else get_settings().resolve(".env")


def _parse_env_keys() -> list[str] | None:
    """从生效 .env 解析键名清单（只记键名绝不记值）；.env 不存在返回 None。"""
    env_path = _env_file_path()
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


def _kb_extension_path() -> Path:
    """kb_extension.json 路径（与 dispatcher/rag.py 同一硬编码相对路径口径）。"""
    return get_settings().resolve("app/data/kb_extension.json")


def _backup_db(src: Path, dst: Path) -> bool:
    """sqlite3 在线备份（WAL 安全，运行中库产出的快照已合并 wal）。

    备份后对快照侧做 PRAGMA integrity_check（沿用 backup_master.py 先例），
    非 ok 抛 RuntimeError。src 不存在则跳过（记 info，全新部署允许），
    返回是否实际写入。
    """
    if not src.exists():
        logger.info("备份跳过（源库不存在）: %s", src)
        return False
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    check_conn = sqlite3.connect(str(dst))
    try:
        ok = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
    finally:
        check_conn.close()
    if ok != "ok":
        raise RuntimeError(f"{src.name} 快照完整性校验失败: {ok}")
    return True


def _external_paths_payload() -> dict:
    """external_paths.json 内容：外部路径当前生效值 + 存在性布尔。

    配置值为空串时记 value="" / exists=null（不拿 PROJECT_ROOT 冒充）。
    """
    s = get_settings()

    def entry(raw: str, resolved: Path) -> dict:
        if not raw.strip():
            return {"value": "", "exists": None}
        return {"value": str(resolved), "exists": resolved.exists()}

    return {
        "master_db_path": entry(s.master_db_path, s.master_db_abs),
        "checkpoint_db_path": entry(s.checkpoint_db_path, s.checkpoint_db_abs),
        "output_dir": entry(s.output_dir, s.output_dir_abs),
        "upstream_root": entry(s.upstream_root, s.resolve(s.upstream_root)),
        "downstream_file_path": entry(
            s.downstream_file_path, s.resolve(s.downstream_file_path)),
        "watch_dir": entry(s.watch_dir, s.resolve(s.watch_dir)),
        "alias_map_path": entry(s.alias_map_path, s.alias_map_abs),
        "backup_dir": entry(s.backup_dir, s.backup_dir_abs),
    }


def create_snapshot(reason: str) -> dict:
    """创建一份时间戳快照（所有触发点共用入口）。

    全局锁非阻塞获取，已有备份进行中 → raise RuntimeError；
    任一步异常：删除半成品快照目录、记 error 日志、原样抛出。
    """
    if not _snapshot_lock.acquire(blocking=False):
        raise RuntimeError("已有备份任务进行中")
    try:
        return _create_snapshot_locked(reason)
    finally:
        _snapshot_lock.release()


def _create_snapshot_locked(reason: str) -> dict:
    settings = get_settings()
    backup_root = settings.backup_dir_abs
    backup_root.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    created_at = datetime.now()

    # 快照目录：同一秒撞名则休眠 1 秒重取时间戳，最多 3 次
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
        raise RuntimeError("快照目录撞名重试 3 次仍失败")

    try:
        files: list[str] = []
        skipped: list[str] = []

        # 1) 两个 DB：WAL 安全在线备份 + 快照侧完整性校验
        for db in (settings.master_db_abs, settings.checkpoint_db_abs):
            if _backup_db(db, snap_dir / db.name):
                files.append(db.name)
            else:
                skipped.append(db.name)

        # 2) kb_extension.json 直拷（.env 不进快照——防密钥外泄，只记键名）
        kb = _kb_extension_path()
        if kb.exists():
            shutil.copy2(kb, snap_dir / kb.name)
            files.append(kb.name)
        else:
            logger.warning("备份跳过（文件不存在）: %s", kb)
            skipped.append(kb.name)

        # 3) output/ 全量（目录不存在则跳过）
        out_dir = settings.output_dir_abs
        if out_dir.exists():
            shutil.copytree(out_dir, snap_dir / "output")
            files.append("output/")
        else:
            logger.warning("备份跳过（目录不存在）: %s", out_dir)
            skipped.append("output/")

        # 4) 两份清单：external_paths.json + manifest.json
        (snap_dir / "external_paths.json").write_text(
            json.dumps(_external_paths_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 逐文件字节数（output/ 递归合计；manifest.json 自身尚未写入不计）
        file_sizes: dict[str, int] = {}
        total = 0
        for p in sorted(snap_dir.rglob("*")):
            if p.is_file():
                size = p.stat().st_size
                file_sizes[p.relative_to(snap_dir).as_posix()] = size
                total += size
        manifest = {
            "reason": reason,
            "created_at": created_at.isoformat(timespec="seconds"),
            "elapsed_s": round(time.monotonic() - t0, 3),
            "size_bytes": total,
            "files": files,
            "skipped": skipped,
            "file_sizes": file_sizes,
            "env_keys": _parse_env_keys(),
        }
        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # 5) 保留最近 RETAIN_COUNT 份
        _prune()

        elapsed = time.monotonic() - t0
        logger.info(
            "备份完成 | reason=%s path=%s elapsed=%.1fs size=%.1fMB",
            reason, snap_dir, elapsed, total / 1024 / 1024,
        )
        return {
            "ok": True,
            "snapshot": str(snap_dir),
            "elapsed_s": round(elapsed, 3),
            "size_bytes": total,
            "files": files,
        }
    except Exception:
        # 半成品清理：删整份快照目录，记 error，原样抛出
        shutil.rmtree(snap_dir, ignore_errors=True)
        logger.error("备份失败，半成品快照已清理 | reason=%s", reason, exc_info=True)
        raise


def list_snapshots() -> list[Path]:
    """白名单时间戳快照目录（按名称升序；其他目录/文件不列入）。"""
    root = get_settings().backup_dir_abs
    if not root.exists():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and SNAPSHOT_NAME_RE.match(p.name)
    )


def latest_snapshot_time() -> datetime | None:
    """最新快照时间（目录名解析失败的目录跳过）；无快照返回 None。"""
    for p in reversed(list_snapshots()):
        try:
            return datetime.strptime(p.name, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
    return None


def _prune() -> None:
    """保留最近 RETAIN_COUNT 份；只删白名单时间戳目录，其余一概不动。"""
    snaps = list_snapshots()
    excess = len(snaps) - RETAIN_COUNT
    if excess <= 0:
        return
    for p in snaps[:excess]:
        shutil.rmtree(p)
        logger.info("备份保留策略：删除最旧快照 %s", p)


def _backup_worker(reason: str) -> None:
    """backup-worker 线程体：异常只记 error 绝不外抛；成功更新节流时间戳。"""
    global _running, _last_success_ts
    try:
        create_snapshot(reason)
    except Exception:
        logger.error("异步备份失败 | reason=%s", reason, exc_info=True)
    else:
        with _state_lock:
            _last_success_ts = time.monotonic()
    finally:
        with _state_lock:
            _running = False


def trigger_async(reason: str, *, manual: bool = False) -> bool:
    """异步触发备份：进行中直接返回 False；自动触发距上次成功不足 60 秒
    返回 False（debug 日志）；否则起 daemon 线程 backup-worker 跑
    create_snapshot，返回是否已触发。手动端点（manual=True）不受节流限制
    （但仍受「进行中」限制）。
    """
    global _running
    with _state_lock:
        if _running:
            logger.debug("备份触发跳过（已有备份进行中）| reason=%s", reason)
            return False
        if not manual and _last_success_ts and \
                time.monotonic() - _last_success_ts < AUTO_MIN_INTERVAL_SECONDS:
            logger.debug(
                "备份触发跳过（距上次成功不足 %ds）| reason=%s",
                AUTO_MIN_INTERVAL_SECONDS, reason,
            )
            return False
        _running = True
    t = threading.Thread(
        target=_backup_worker, args=(reason,), name="backup-worker", daemon=True)
    t.start()
    return True


def maybe_backup_on_startup() -> bool:
    """启动超期判定：无快照或最新快照距今 >12h → trigger_async("startup")。

    整个调用 try/except 包死，失败只 warning 返回 False，不阻断启动
    （与 factory_watch 同哲学）。返回是否已触发。
    """
    try:
        latest = latest_snapshot_time()
        if latest is not None and \
                datetime.now() - latest <= timedelta(hours=STARTUP_MAX_AGE_HOURS):
            return False
        return trigger_async("startup")
    except Exception:
        logger.warning("启动时自动备份检查失败", exc_info=True)
        return False
