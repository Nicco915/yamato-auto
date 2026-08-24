#!/usr/bin/env python3
"""master 主数据修复脚本：恢复产品映射 + 品名组，并清理孤儿工厂。

用法（在 app/ 目录下）：
  python3 scripts/remediate_master_data.py

若用于 CI/自动化，需先设置环境变量跳过确认：
  YAMATO_ALLOW_DESTRUCTIVE=1 python3 scripts/remediate_master_data.py

脚本会自动：
1. 先调用 backup_master.snapshot() 创建时间戳快照
2. 从 96/报关匹配东京.xlsx 导入 product_mappings
3. 从 scripts/import_product_mappings.py 的 GROUPS 恢复 product_groups / members
4. 删除没有任何别名/SKU/映射关联的孤儿工厂
5. 打印修复后各表记录数
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.db.models import Factory, FactoryAlias, FactorySKU, ProductMapping
from app.db.session import get_session
import backup_master
from import_product_mappings import import_groups, import_mappings


def _require_confirm() -> None:
    if os.environ.get("YAMATO_ALLOW_DESTRUCTIVE") == "1":
        return
    print("⚠️  本脚本会修改 master.db（导入映射/品名组并清理孤儿工厂）。")
    print("   继续请输入 yes：", end="", flush=True)
    if input().strip().lower() != "yes":
        print("已取消")
        sys.exit(0)


def _clean_orphan_factories(session) -> int:
    """删除没有别名、SKU、产品映射关联的工厂，返回删除数。"""
    orphan_ids = [
        r[0]
        for r in session.execute(
            text(
                """
                SELECT f.factory_id
                FROM factories f
                LEFT JOIN factory_aliases a ON a.factory_id = f.factory_id
                LEFT JOIN factory_skus s ON s.factory_id = f.factory_id
                LEFT JOIN product_mappings pm ON pm.factory_id = f.factory_id
                WHERE a.id IS NULL AND s.sku_id IS NULL AND pm.id IS NULL
                """
            )
        )
    ]
    if not orphan_ids:
        return 0
    for fid in orphan_ids:
        session.delete(session.get(Factory, fid))
    session.flush()
    return len(orphan_ids)


def _print_counts(session) -> None:
    print("\n修复后各表记录数：")
    for label, sql in (
        ("factories", "SELECT COUNT(*) FROM factories"),
        ("factory_aliases", "SELECT COUNT(*) FROM factory_aliases"),
        ("factory_skus", "SELECT COUNT(*) FROM factory_skus"),
        ("product_mappings", "SELECT COUNT(*) FROM product_mappings"),
        ("product_groups", "SELECT COUNT(*) FROM product_groups"),
        ("product_group_members", "SELECT COUNT(*) FROM product_group_members"),
    ):
        count = session.execute(text(sql)).scalar()
        print(f"  {label:24s}: {count}")


def main() -> None:
    _require_confirm()

    print("\n[1/4] 创建快照...")
    backup_dir = backup_master.snapshot()

    session = get_session()
    try:
        print("\n[2/4] 导入产品映射...")
        n_mappings = import_mappings(session)

        print("\n[3/4] 恢复品名组...")
        n_groups = import_groups(session)

        print("\n[4/4] 清理孤儿工厂...")
        n_orphans = _clean_orphan_factories(session)

        session.commit()

        print(f"\n修复结果：")
        print(f"  产品映射: {n_mappings} 条")
        print(f"  品名组:   {n_groups} 组")
        print(f"  清理孤儿工厂: {n_orphans} 个")
        _print_counts(session)
        print(f"\n快照目录: {backup_dir}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
