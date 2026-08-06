#!/usr/bin/env python3
"""工厂主数据迁移：alias_map.json + FACTORY_NORMALIZE_MAP + INSPECTION_FACTORIES 入库。

- factories 扩充：json key（日文名）= factory_name，存在且 short_name 为空则回填，不存在则新建
- alias_map.json → factory_aliases：alias=日文名，目标 factory 按 short_name=中文短名 找，
  use_folder_match=True（同一 alias 已有 folder 行则跳过，不覆盖人工改动）
- FACTORY_NORMALIZE_MAP → factory_aliases：alias=变体，目标 factory 按 factory_name=规范名 找，
  use_excel_normalize=True；若同一 alias 已有 folder 行则合并为一行（置 excel 标记），
  两边目标 factory 不同但 short_name 一致时重指向 normalize 目标（保证归一化结果不变）
- INSPECTION_FACTORIES → factories.is_inspection_factory=True

幂等可重跑。结尾打印迁移条数 + 未匹配项清单 + factories 全量。

用法（在 app/ 目录下）：
  python3 scripts/migrate_factory_aliases.py
"""
import json
import sys
from pathlib import Path

# 保证能 import app 包（脚本位于 app/scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Base, Factory, FactoryAlias  # noqa: E402
from app.db.session import get_engine, get_session  # noqa: E402


def _ensure_columns(engine) -> None:
    """create_all 不会给既有表加列：检测 factories 新列，缺失则 ALTER TABLE（SQLite）。"""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(factories)"))}
        if "short_name" not in cols:
            conn.execute(text("ALTER TABLE factories ADD COLUMN short_name VARCHAR(100)"))
            print("  ALTER TABLE factories ADD COLUMN short_name")
        if "is_inspection_factory" not in cols:
            conn.execute(text(
                "ALTER TABLE factories ADD COLUMN is_inspection_factory BOOLEAN NOT NULL DEFAULT 0"
            ))
            print("  ALTER TABLE factories ADD COLUMN is_inspection_factory")
    Base.metadata.create_all(engine)  # factory_aliases 等新表


def _load_alias_map() -> dict:
    p = get_settings().alias_map_abs
    if not p.exists():
        print(f"警告: alias_map 不存在，按空表处理: {p}")
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _find_by_short_name(session, short_name: str, alias: str | None = None) -> Factory | None:
    """按 short_name 找目标 factory；多个候选时优先 factory_name==alias，其次 factory_id 最小。"""
    candidates = (
        session.query(Factory)
        .filter(Factory.short_name == short_name)
        .order_by(Factory.factory_id)
        .all()
    )
    if not candidates:
        return None
    if alias:
        for c in candidates:
            if c.factory_name == alias:
                return c
    return candidates[0]


def migrate(session) -> dict:
    settings = get_settings()
    alias_map = _load_alias_map()
    normalize_map = settings.FACTORY_NORMALIZE_MAP
    inspection_factories = settings.INSPECTION_FACTORIES

    stats = {
        "factory_created": 0,
        "short_backfilled": 0,
        "alias_folder_new": 0,
        "alias_excel_new": 0,
        "alias_merged": 0,
        "inspection_marked": 0,
    }
    unmatched = []   # (来源, alias, 原因)
    conflicts = []   # 同一 alias 双用途但目标 factory 的 short_name 不一致，需人工

    # 1. factories 扩充：json key=日文名=factory_name，value=中文短名
    for jp_name, short in alias_map.items():
        f = session.query(Factory).filter(Factory.factory_name == jp_name).first()
        if f is None:
            session.add(Factory(factory_name=jp_name, short_name=short))
            stats["factory_created"] += 1
        elif not f.short_name:
            f.short_name = short
            stats["short_backfilled"] += 1
    session.flush()

    # 2. alias_map.json → factory_aliases（文件夹匹配套用）
    for jp_name, short in alias_map.items():
        existing = (
            session.query(FactoryAlias)
            .filter(FactoryAlias.alias == jp_name, FactoryAlias.use_folder_match.is_(True))
            .first()
        )
        if existing is not None:
            continue  # 幂等：已有 folder 行不覆盖（保留人工重关联）
        target = _find_by_short_name(session, short, alias=jp_name)
        if target is None:
            unmatched.append(("alias_map", jp_name, f"找不到 short_name={short} 的工厂"))
            continue
        session.add(FactoryAlias(
            factory_id=target.factory_id,
            alias=jp_name,
            use_folder_match=True,
            use_excel_normalize=False,
        ))
        stats["alias_folder_new"] += 1
    session.flush()

    # 3. FACTORY_NORMALIZE_MAP → factory_aliases（Excel 归一化用途，同 alias 合并一行）
    for variant, canonical in normalize_map.items():
        target = session.query(Factory).filter(Factory.factory_name == canonical).first()
        if target is None:
            unmatched.append(("normalize_map", variant, f"找不到 factory_name={canonical} 的工厂"))
            continue
        already = (
            session.query(FactoryAlias)
            .filter(FactoryAlias.alias == variant, FactoryAlias.use_excel_normalize.is_(True))
            .first()
        )
        if already is not None:
            continue  # 幂等
        folder_row = (
            session.query(FactoryAlias)
            .filter(FactoryAlias.alias == variant, FactoryAlias.use_folder_match.is_(True))
            .first()
        )
        if folder_row is not None:
            # 同一 alias 两种用途：合并为一行
            if folder_row.factory_id != target.factory_id:
                current = session.get(Factory, folder_row.factory_id)
                if current is not None and current.short_name == target.short_name:
                    # 两目标 short_name 一致：重指向 normalize 目标，两种用途结果都不变
                    folder_row.factory_id = target.factory_id
                else:
                    conflicts.append(
                        f"{variant}: folder 目标={current.factory_name if current else '?'} "
                        f"与 normalize 目标={canonical} 的 short_name 不一致，保留 folder 目标"
                    )
            folder_row.use_excel_normalize = True
            stats["alias_merged"] += 1
        else:
            session.add(FactoryAlias(
                factory_id=target.factory_id,
                alias=variant,
                use_folder_match=False,
                use_excel_normalize=True,
            ))
            stats["alias_excel_new"] += 1
    session.flush()

    # 4. INSPECTION_FACTORIES → is_inspection_factory=True
    for name in inspection_factories:
        f = session.query(Factory).filter(Factory.factory_name == name).first()
        if f is None:
            unmatched.append(("inspection_factories", name, "factories 表无此行"))
            continue
        if not f.is_inspection_factory:
            f.is_inspection_factory = True
            stats["inspection_marked"] += 1
    session.flush()

    return {"stats": stats, "unmatched": unmatched, "conflicts": conflicts}


def print_report(result: dict, session) -> None:
    stats = result["stats"]
    print("\n===== 迁移报告 =====")
    print(f"  新建工厂: {stats['factory_created']}，回填短名: {stats['short_backfilled']}")
    print(f"  别名(文件夹匹配)新增: {stats['alias_folder_new']}")
    print(f"  别名(Excel归一)新增: {stats['alias_excel_new']}，双用途合并: {stats['alias_merged']}")
    print(f"  商检工厂标记: {stats['inspection_marked']}")

    if result["unmatched"]:
        print("\n  未匹配项（请到 /mappings 页人工关联）:")
        for source, alias, reason in result["unmatched"]:
            print(f"    [{source}] {alias}: {reason}")
    else:
        print("\n  未匹配项: 无")

    if result["conflicts"]:
        print("\n  用途冲突（需人工确认目标工厂）:")
        for c in result["conflicts"]:
            print(f"    {c}")

    print("\n  当前 factories 全量:")
    for f in session.query(Factory).order_by(Factory.factory_id).all():
        print(
            f"    {f.factory_id:>3} | {f.factory_name} | short={f.short_name} "
            f"| 商检={f.is_inspection_factory}"
        )
    alias_count = session.query(FactoryAlias).count()
    print(f"\n  factory_aliases 总数: {alias_count}")


def main() -> None:
    engine = get_engine()
    _ensure_columns(engine)
    session = get_session()
    try:
        result = migrate(session)
        session.commit()
        print_report(result, session)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
