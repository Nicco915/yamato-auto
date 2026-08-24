#!/usr/bin/env python3
"""正向同步：把 product_mappings 中 sku_code 非空的行回填到 factory_skus。

用法（在 app/ 目录下）：
  YAMATO_ALLOW_DESTRUCTIVE=1 python3 scripts/sync_mappings_to_skus.py

说明：
- 只同步 sku_code 非空的映射行。
- 对每条映射行，按 sku_code 查找 factory_skus 并回填 name_cn/hs_code/inspection_required。
- 写入前先创建快照。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ProductMapping
from app.db.session import get_session
from app.db.sync import sync_mapping_to_sku
from backup_master import snapshot


def _require_confirm() -> None:
    if os.environ.get("YAMATO_ALLOW_DESTRUCTIVE") == "1":
        return
    print("⚠️  本脚本会把 product_mappings 的 SKU 级数据正向回填到 factory_skus。")
    print("   继续请输入 yes：", end="", flush=True)
    if input().strip().lower() != "yes":
        print("已取消")
        sys.exit(0)


def main() -> None:
    _require_confirm()

    print("[1/2] 创建快照...")
    backup_dir = snapshot()

    session = get_session()
    try:
        print("[2/2] 正向同步 product_mappings -> factory_skus...")
        rows = (
            session.query(ProductMapping)
            .filter(ProductMapping.sku_code.isnot(None))
            .all()
        )
        total_updated = 0
        for m in rows:
            total_updated += sync_mapping_to_sku(session, m)
        session.commit()
        print(f"\n同步完成：")
        print(f"  映射行数: {len(rows)}")
        print(f"  更新 SKU 主数据行数: {total_updated}")
        print(f"\n快照目录: {backup_dir}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
