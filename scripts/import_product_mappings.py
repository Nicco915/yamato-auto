#!/usr/bin/env python3
"""从《报关匹配东京.xlsx》Sheet1 导入产品映射，并写入品名组初始数据。

- Sheet1 结构（表头第 1 行）：产品 | 税号 | 供应商 | 商检 | 产品组一 | 自定义七
- 按 (产品, 供应商) 去重；幂等 upsert（按 product_name_cn + supplier_name）
- 商检列：'商检' → True，空/'(空白)' → False
- 供应商全称与 factories.factory_name 不一致，factory_id 一律留 NULL（后续维护页人工关联）
- sku_code 全部留 NULL（初版品名级）

用法（在 app/ 目录下）：
  python3 scripts/import_product_mappings.py
"""
import sys
from pathlib import Path

# 保证能 import app 包（脚本位于 app/scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import ProductGroup, ProductGroupMember  # noqa: E402
from app.db.session import get_session  # noqa: E402

XLSX_PATH = Path("/Users/nz/Downloads/yamato/96/报关匹配东京.xlsx")

# 品名组初始数据（净重为报关样本总净重/套数推导的单件净重 kg/件）
GROUPS = [
    {
        "name": "6件套",
        "group_type": "set_split",
        "source_name_cn": "6件套",
        # 报关东京A：1941 套对应各组件净重 1177.63/1361.03/2988.64/2564.4/583.05/2350.13
        "members": [
            ("床罩", 1, 4.0, 0.6067),
            ("被褥套", 2, 4.0, 0.7013),
            ("绗缝垫", 3, 5.0, 1.5398),
            ("枕头", 4, 4.0, 1.3212),
            ("枕套", 5, 2.0, 0.3004),
            ("绗缝被", 6, 0.3, 1.2112),
        ],
    },
    {
        "name": "3件套",
        "group_type": "set_split",
        "source_name_cn": "3件套",
        # 报关横滨B：15 套对应 23.09/19.81/53.1，除以 15 得 1.5393/1.3207/3.54
        # （与 6件套组件单重略有出入，以横滨B 3件套样本为准）
        "members": [
            ("绗缝垫", 1, 5.0, 1.5393),
            ("枕头", 2, 4.0, 1.3207),
            ("绗缝被", 3, 13.472, 3.54),
        ],
    },
    {
        "name": "烟灰缸+支架",
        "group_type": "box_share",
        "source_name_cn": "烟灰缸",
        # 报关名古屋A：60 件对应 39.0/72.0，除以 60 得 0.65/1.2；box_share 金额平均分，split_price 留 NULL
        "members": [
            ("烟灰缸", 1, None, 0.65),
            ("烟灰缸支架", 2, None, 1.2),
        ],
    },
]


def import_mappings(session) -> int:
    """读 Sheet1，按 (产品, 供应商) 去重后 upsert product_mappings。返回写入条数。"""
    from app.declare.mapping_import import parse_mapping_rows, upsert_mappings

    rows = parse_mapping_rows(XLSX_PATH)
    created, updated = upsert_mappings(session, rows)
    print(f"  映射: 新增 {created}，更新 {updated}")
    return created + updated


def import_groups(session) -> int:
    """写入品名组初始数据（幂等：同名同源的组先清成员再重建）。"""
    for g in GROUPS:
        group = (
            session.query(ProductGroup)
            .filter(
                ProductGroup.name == g["name"],
                ProductGroup.source_name_cn == g["source_name_cn"],
            )
            .first()
        )
        if group is None:
            group = ProductGroup(
                name=g["name"],
                group_type=g["group_type"],
                source_name_cn=g["source_name_cn"],
            )
            session.add(group)
            session.flush()
        else:
            group.group_type = g["group_type"]
            session.query(ProductGroupMember).filter(
                ProductGroupMember.group_id == group.id
            ).delete()
        for product_name, order, price, net_weight in g["members"]:
            session.add(
                ProductGroupMember(
                    group_id=group.id,
                    product_name_cn=product_name,
                    display_order=order,
                    split_price=price,
                    split_net_weight=net_weight,
                )
            )
    return len(GROUPS)


def main() -> None:
    if not XLSX_PATH.exists():
        print(f"源文件不存在: {XLSX_PATH}")
        sys.exit(1)
    session = get_session()
    try:
        n_mappings = import_mappings(session)
        n_groups = import_groups(session)
        session.commit()
        print(f"导入完成：产品映射 {n_mappings} 条，品名组 {n_groups} 组")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
