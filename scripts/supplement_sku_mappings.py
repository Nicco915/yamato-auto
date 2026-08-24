#!/usr/bin/env python3
"""按用户提供的清单补充 product_mappings 的 SKU 级映射。

用法（在 app/ 目录下）：
  YAMATO_ALLOW_DESTRUCTIVE=1 python3 scripts/supplement_sku_mappings.py

逻辑：
1. 解析脚本内硬编码的 SUPPLEMENT 字典（品名 -> SKU 列表）。
2. 只处理当前 product_mappings 中已存在的品名；不存在的跳过。
3. 对每个已存在品名：
   - 以现有映射行为模板复制 hs_code/supplier_name/inspection_required/name_en/unit_code/factory_id。
   - 删除该品名下行 sku_code 为 NULL 的原品名级兜底行。
   - 为每个 SKU 插入一行 SKU 级映射；已存在（任意品名下）的 SKU 跳过并报告。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.db.models import ProductMapping
from app.db.session import get_session
from app.db.sync import sync_mapping_to_sku
from backup_master import snapshot

# 用户 2026-08-24 提供的补充清单
SUPPLEMENT: dict[str, list[str]] = {
    "木橱": ["4549509280125"],
    "木箱": [
        "4549509315285", "4549509315292", "4549509315308",
        "4549509315315", "4549509315322", "4549509315339",
    ],
    "木盖": [
        "4550596092836", "4550596092843",
        "4550596184838", "4550596197395",
    ],
    "44木架": [
        "4550596018607", "4550596018614", "4550596018621",
        "4550596018638", "4550596018645", "4550596018652",
        "4550596018669", "4550596018676", "4550596018683",
        "4550596021126", "4550596018690", "4550596018713",
        "4550596202044", "4550596202051", "4550596018706",
    ],
    "木制抓挠盒": ["4550596145907"],
    "写字板": [
        "4549509341550", "4549509341567", "4549509845539",
    ],
    "坐垫": [
        "4549509518860", "4549509518877", "4549509518884",
        "4549509518891", "4550596018201", "4550596018218",
        "4550596018225", "4550596018232", "4550596018249",
        "4550596018256", "4550596109466",
    ],
    "枕头": [
        "4549509610960", "4549509610977", "4549509610984",
    ],
}


def _require_confirm() -> None:
    if os.environ.get("YAMATO_ALLOW_DESTRUCTIVE") == "1":
        return
    print("⚠️  本脚本会修改 product_mappings（删除 NULL sku 行并插入 SKU 级行）。")
    print("   继续请输入 yes：", end="", flush=True)
    if input().strip().lower() != "yes":
        print("已取消")
        sys.exit(0)


def main() -> None:
    _require_confirm()

    print("[1/3] 创建快照...")
    backup_dir = snapshot()

    session = get_session()
    try:
        print("[2/3] 处理 SKU 补充...")
        # 当前所有映射行
        all_rows = session.query(ProductMapping).all()
        existing_names = {r.product_name_cn for r in all_rows}
        existing_skus = {r.sku_code for r in all_rows if r.sku_code}

        skipped_names = []
        processed = []
        skipped_skus = []

        for name, skus in SUPPLEMENT.items():
            if name not in existing_names:
                skipped_names.append(name)
                continue

            template_rows = [r for r in all_rows if r.product_name_cn == name]
            if not template_rows:
                skipped_names.append(name)
                continue
            template = template_rows[0]

            # 删除该品名下行 sku_code 为 NULL 的原品名级兜底行
            null_rows = [
                r for r in template_rows if r.sku_code is None or r.sku_code == ""
            ]
            for r in null_rows:
                session.delete(r)

            inserted = 0
            for sku in skus:
                if sku in existing_skus:
                    skipped_skus.append((name, sku))
                    continue
                new_mapping = ProductMapping(
                    product_name_cn=name,
                    sku_code=sku,
                    hs_code=template.hs_code,
                    supplier_name=template.supplier_name,
                    inspection_required=template.inspection_required,
                    name_en=template.name_en,
                    unit_code=template.unit_code,
                    factory_id=template.factory_id,
                    is_incomplete=template.is_incomplete,
                )
                session.add(new_mapping)
                sync_mapping_to_sku(session, new_mapping)
                existing_skus.add(sku)
                inserted += 1

            processed.append((name, len(null_rows), inserted))

        session.commit()

        print("\n[3/3] 处理结果：")
        print(f"  已处理品名: {len(processed)}")
        for name, deleted, inserted in processed:
            print(f"    {name}: 删除 NULL 行 {deleted}，新增 SKU 行 {inserted}")
        if skipped_names:
            print(f"\n  已跳过（当前映射中无此品名）: {len(skipped_names)}")
            for n in skipped_names:
                print(f"    - {n}")
        if skipped_skus:
            print(f"\n  已跳过（SKU 已存在于其他品名）: {len(skipped_skus)}")
            for n, s in skipped_skus:
                print(f"    - {n} / {s}")
        print(f"\n快照目录: {backup_dir}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
