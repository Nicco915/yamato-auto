#!/usr/bin/env python3
"""一次性迁移：合并产品映射中同（品名+供应商）的重复行（多 SKU 化后的存量收敛）。

背景：S4 多 SKU 化后，存量数据仍是「一品名 N 行、每行 1 SKU」的旧结构。
本脚本把同（product_name_cn, supplier_name）的重复行合并为一行：
- 保留 id 最小的行，其余行的 SKU 关联全部并入其 sku_links 后删除
- 仅当组内所有行的 税号/商检/英文品名/单位代码 完全一致时自动合并；
  任一字段有冲突则不合并，打印冲突清单交人工在 /mappings 页面定夺
  （零容错：程序不擅自挑选字段值）

用法（在仓库根目录，如 D:\\project\\yamto）：
  python scripts/merge_duplicate_mappings.py            # dry-run：只打印计划，不改库
  python scripts/merge_duplicate_mappings.py --apply    # 实际执行（执行前自动备份）

环境：跟随应用 .env 的 master 库路径；测试用 YAMATO_DOTENV_PATH 隔离。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ProductMapping  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.db.sync import _mapping_sku_codes  # noqa: E402

# 合并时比对一致性的字段（product_name_cn/supplier_name 是分组键，不在其内；
# is_incomplete 由合并后的 hs_code 重新推导）
_COMPARE_FIELDS = ("hs_code", "inspection_required", "name_en", "unit_code")


def _norm(v) -> str:
    """比对口径：None 与空串等价、去前后空白。"""
    return str(v).strip() if v is not None else ""


def plan_merges() -> tuple[list[dict], list[dict]]:
    """扫描重复组，返回 (可自动合并计划, 冲突组清单)。纯读不写。

    计划项：{"key": (品名, 供应商), "keep_id", "drop_ids", "skus": [...]}
    冲突项：{"key": (品名, 供应商), "rows": [{id, 各字段值...}], "fields": [冲突字段]}
    """
    with get_session() as s:
        rows = s.query(ProductMapping).order_by(ProductMapping.id).all()
        groups: dict[tuple[str, str], list[ProductMapping]] = {}
        for m in rows:
            groups.setdefault((_norm(m.product_name_cn), _norm(m.supplier_name)),
                              []).append(m)
        plans, conflicts = [], []
        for key, ms in groups.items():
            if len(ms) < 2:
                continue
            bad_fields = [
                f for f in _COMPARE_FIELDS
                if len({_norm(getattr(m, f)) for m in ms}) > 1
            ]
            if bad_fields:
                conflicts.append({
                    "key": key,
                    "fields": bad_fields,
                    "rows": [
                        {"id": m.id,
                         **{f: getattr(m, f) for f in _COMPARE_FIELDS},
                         "skus": _mapping_sku_codes(m)}
                        for m in ms
                    ],
                })
                continue
            skus: list[str] = []
            for m in ms:
                for code in _mapping_sku_codes(m):
                    if code not in skus:
                        skus.append(code)
            plans.append({
                "key": key,
                "keep_id": ms[0].id,
                "drop_ids": [m.id for m in ms[1:]],
                "skus": skus,
            })
    return plans, conflicts


def apply_plans(plans: list[dict]) -> dict:
    """执行合并：SKU 并入保留行、删除多余行、is_incomplete 按 hs_code 重推导。"""
    from app.db.sync import _is_blank  # 与建行/编辑同口径的空值判定

    merged_groups = 0
    moved_skus = 0
    with get_session() as s:
        for p in plans:
            keep = s.get(ProductMapping, p["keep_id"])
            if keep is None:
                continue
            # existing 只看子表实际行：不能用 _mapping_sku_codes（带旧列兜底），
            # 否则子表未迁移的保留行会把旧列里的自身 SKU 误判为「已存在」，
            # 导致它永远不会进子表（本机实测丢行，靠旧列兜底才没显示出来）
            existing = {link.sku_code for link in keep.sku_links}
            for code in p["skus"]:
                if code not in existing:
                    keep.sku_links.append(_new_link(code))
                    existing.add(code)
                    moved_skus += 1
            # 旧列同步为列表第一个（与 mappings_api._replace_sku_links 同语义，
            # 防启动迁移把已删 SKU 幽灵搬回）
            keep.sku_code = p["skus"][0] if p["skus"] else None
            keep.is_incomplete = _is_blank(keep.hs_code)
            for drop_id in p["drop_ids"]:
                drop = s.get(ProductMapping, drop_id)
                if drop is not None:
                    s.delete(drop)  # relationship 级联清子表旧关联
            merged_groups += 1
        s.commit()
    return {"merged_groups": merged_groups, "moved_skus": moved_skus}


def _new_link(sku_code: str):
    """构造子表行（抽出来便于测试打桩）。"""
    from app.db.models import ProductMappingSku
    return ProductMappingSku(sku_code=sku_code)


def _print_plan(plans: list[dict], conflicts: list[dict]) -> None:
    if plans:
        print(f"\n可自动合并 {len(plans)} 组：")
        for p in plans:
            name, supplier = p["key"]
            print(f"  「{name}」（供应商：{supplier or '空'}）"
                  f" 保留 id={p['keep_id']}，删除 {p['drop_ids']}，"
                  f"SKU 收拢 {len(p['skus'])} 个")
    else:
        print("\n没有可自动合并的重复组。")
    if conflicts:
        print(f"\n字段冲突、需人工处理的 {len(conflicts)} 组：")
        for c in conflicts:
            name, supplier = c["key"]
            print(f"  「{name}」（供应商：{supplier or '空'}）冲突字段: "
                  f"{', '.join(c['fields'])}")
            for r in c["rows"]:
                vals = ", ".join(f"{f}={r[f]!r}" for f in c["fields"])
                print(f"    id={r['id']}: {vals} | SKU: {r['skus']}")
        print("  → 请在 /mappings 页面人工确认正确值后手动合并")


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    plans, conflicts = plan_merges()
    _print_plan(plans, conflicts)
    if not apply:
        print("\n[dry-run] 未改动数据库。确认无误后加 --apply 实际执行。")
        return 1 if conflicts else 0
    if not plans:
        return 1 if conflicts else 0

    # 写操作确认门 + 执行前备份（CLAUDE.md：一次一确认）
    import backup_master
    ans = input(f"\n即将合并 {len(plans)} 组（执行前自动备份 master.db）。"
                f"确认执行？[y/N] ").strip().lower()
    if ans != "y":
        print("已取消。")
        return 2
    backup_dir = backup_master.snapshot()
    print(f"已备份到 {backup_dir}")
    result = apply_plans(plans)
    print(f"完成：合并 {result['merged_groups']} 组，"
          f"移动 SKU 关联 {result['moved_skus']} 条。")
    if conflicts:
        print(f"另有 {len(conflicts)} 组字段冲突未合并（见上方清单）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
