#!/usr/bin/env python3
"""master/checkpoint 数据库快照 + 完整性校验。

用法（在 app/ 目录下）：
  python3 scripts/backup_master.py

环境变量：
  YAMATO_BACKUP_DIR  覆盖默认备份目录（默认 master.db 同级 backups/）
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings


def _integrity_check(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ok = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"{db_path.name} 完整性校验失败: {ok}")
    finally:
        conn.close()


def snapshot() -> Path:
    """为当前 master.db / checkpoints.db / alias_map.json / .env 创建时间戳快照。"""
    settings = get_settings()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = Path(os.environ.get("YAMATO_BACKUP_DIR") or settings.master_db_abs.parent)
    backup_dir = base_dir / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    files = [
        settings.master_db_abs,
        settings.checkpoint_db_abs,
        settings.alias_map_abs,
        settings.resolve(".env"),
    ]
    copied = []
    for src in files:
        if src.exists():
            dst = backup_dir / src.name
            shutil.copy2(src, dst)
            copied.append(src.name)
            if src.suffix == ".db":
                _integrity_check(dst)

    print(f"备份完成: {backup_dir}")
    print(f"  包含: {', '.join(copied)}")
    return backup_dir


def main() -> None:
    snapshot()


if __name__ == "__main__":
    main()
