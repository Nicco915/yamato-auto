"""product_mappings ↔ factory_skus 单向同步工具（供 UI 与脚本复用）。"""
from sqlalchemy.orm import Session

from app.db.models import FactorySKU, ProductMapping


def sync_mapping_to_sku(session: Session, mapping: ProductMapping) -> int:
    """product_mappings → factory_skus 单向回填。

    若 mapping.sku_code 非空：回填该行 factory_skus 的 name_cn/hs_code/inspection_required。
    返回更新的行数。仅回填，不删不改其他字段（unit_net_weight/unit_gross_weight 不动）。
    """
    if not mapping.sku_code:
        return 0
    rows = (
        session.query(FactorySKU)
        .filter(FactorySKU.sku_code == mapping.sku_code)
        .all()
    )
    for sku in rows:
        sku.name_cn = mapping.product_name_cn
        sku.hs_code = mapping.hs_code
        sku.inspection_required = mapping.inspection_required
    session.flush()
    return len(rows)
