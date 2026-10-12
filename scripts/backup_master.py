#!/usr/bin/env python3
"""数据库 + 配置 + 输出 全量快照 CLI（委托 orchestrator.backup）。

2026-10-12 起委托 app.orchestrator.backup.create_snapshot（WAL 安全在线备份
+ 快照侧完整性校验 + 保留最近 20 份 + external_paths/manifest 两份清单），
本脚本仅保留 CLI 入口与打印格式，避免两套备份逻辑并存漂移。

用法（在 app/ 目录下）：
  python3 scripts/backup_master.py

环境变量：
  BACKUP_DIR  覆盖默认备份目录（默认 master.db 同级 backups/）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.backup import create_snapshot  # noqa: E402


def snapshot() -> Path:
    """创建时间戳快照并打印结果，返回快照目录。"""
    result = create_snapshot("cli")
    print(f"备份完成: {result['snapshot']}")
    print(f"  包含: {', '.join(result['files'])}")
    return Path(result["snapshot"])


def main() -> None:
    snapshot()


if __name__ == "__main__":
    main()
