"""product_mappings ↔ factory_skus 双向同步工具（供 UI 与脚本复用）。

正向 sync_mapping_to_sku：品名映射 Tab 编辑 → 回填 SKU 主数据；
反向 sync_sku_to_mapping：SKU 主数据 Tab 编辑 → 回填品名映射（仅 SKU 级行）。
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import FactorySKU, ProductMapping

logger = logging.getLogger(__name__)


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


def sync_sku_to_mapping(
    session: Session,
    sku: FactorySKU,
    *,
    sync_name: bool = False,
    sync_hs: bool = False,
    sync_inspection: bool = False,
) -> int:
    """factory_skus → product_mappings 反向回填（SKU 主数据 Tab 编辑后调用）。

    只更新 sku_code 精确匹配的映射行（SKU 级映射）；品名级行（sku_code 为空）
    可能被多个 SKU 共享兜底，绝不触碰。

    风险边界（2026-08-12 与用户确认的设计决策）：
    - product_name_cn 是报关匹配主键，改名会让依赖旧品名兜底的其他 SKU 失配，
      所以仅在 name_cn 非空时回写（清空品名不会毁掉匹配键）；
    - 同 sku_code 的映射行可能有多条且不限工厂，全部更新（与正向 sync 同 scope）；
    - product_mappings 无审计表，此处改动静默——调用方需在响应里返回行数告知用户。

    参数 sync_*：只回写发生变化的字段（调用方按 audited_fields 传入）。
    返回更新的映射行数。
    """
    if not (sync_name or sync_hs or sync_inspection):
        return 0
    rows = (
        session.query(ProductMapping)
        .filter(ProductMapping.sku_code == sku.sku_code)
        .all()
    )
    for m in rows:
        if sync_name and sku.name_cn:
            m.product_name_cn = sku.name_cn
        if sync_hs:
            m.hs_code = sku.hs_code
            m.is_incomplete = not (sku.hs_code or "").strip()
        if sync_inspection:
            m.inspection_required = sku.inspection_required
    session.flush()
    if rows:
        logger.info(
            "[sync] SKU %s → 品名映射反向回填 %d 行（name=%s hs=%s inspection=%s）",
            sku.sku_code, len(rows), sync_name, sync_hs, sync_inspection,
        )
    return len(rows)
