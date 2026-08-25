"""product_mappings ↔ factory_skus 双向同步工具（供 UI 与脚本复用）。

正向 sync_mapping_to_sku：品名映射 Tab 编辑 → 回填 SKU 主数据；
反向 sync_sku_to_mapping：SKU 主数据 Tab 编辑 → 回填品名映射（仅 SKU 级行）；
启动迁移 ensure_mapping_skus_migrated：旧 sku_code 单列只读搬迁到
product_mapping_skus 子表（幂等，失败只记 warning 不阻断启动）。
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import FactorySKU, ProductMapping, ProductMappingSku

logger = logging.getLogger(__name__)


def ensure_mapping_skus_migrated() -> int:
    """启动幂等迁移：product_mappings.sku_code 旧列 → product_mapping_skus 子表。

    对所有 sku_code 非空（NULL 与空串都跳过）的映射行，若子表中没有对应
    (mapping_id, sku_code) 则插入；旧列值不清空（只读搬迁，回滚保险）。
    幂等可重跑；失败只记 warning，绝不阻断启动。返回本次新增的子表行数。
    """
    from app.db.session import get_session

    try:
        with get_session() as session:
            rows = (
                session.query(ProductMapping)
                .filter(ProductMapping.sku_code.isnot(None))
                .filter(ProductMapping.sku_code != "")
                .all()
            )
            existing = {
                (link.mapping_id, link.sku_code)
                for link in session.query(ProductMappingSku).all()
            }
            added = 0
            for m in rows:
                key = (m.id, m.sku_code)
                if key in existing:
                    continue  # 幂等：已搬迁过的跳过
                session.add(ProductMappingSku(mapping_id=m.id, sku_code=m.sku_code))
                existing.add(key)
                added += 1
            session.commit()
        if added:
            logger.info("[迁移] product_mapping_skus 搬迁完成：新增 %d 行 SKU 关联", added)
        else:
            logger.debug("[迁移] product_mapping_skus 无需搬迁（0 行）")
        return added
    except Exception as e:  # noqa: BLE001 迁移失败绝不阻断启动
        logger.warning("[迁移] product_mapping_skus 搬迁失败（不阻断启动）: %s", e)
        return 0


def sync_mapping_to_sku(session: Session, mapping: ProductMapping) -> int:
    """product_mappings → factory_skus 单向回填。

    若 mapping.sku_code 非空：回填该行 factory_skus 的
    name_cn/hs_code/inspection_required，name_en 非空时也回填。
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
        if mapping.name_en is not None:
            sku.name_en = mapping.name_en
    session.flush()
    return len(rows)


def sync_sku_to_mapping(
    session: Session,
    sku: FactorySKU,
    *,
    sync_name: bool = False,
    sync_name_en: bool = False,
    sync_hs: bool = False,
    sync_inspection: bool = False,
) -> int:
    """factory_skus → product_mappings 反向回填（SKU 主数据 Tab 编辑后调用）。

    只更新 sku_code 精确匹配的映射行（SKU 级映射）；品名级行（sku_code 为空）
    可能被多个 SKU 共享兜底，绝不触碰。

    风险边界（2026-08-12 与用户确认的设计决策）：
    - product_name_cn / name_en 是报关匹配主键/辅助字段，清空不会回写，
      仅在原值非空时回写，避免毁掉匹配键；
    - 同 sku_code 的映射行可能有多条且不限工厂，全部更新（与正向 sync 同 scope）；
    - product_mappings 无审计表，此处改动静默——调用方需在响应里返回行数告知用户。

    参数 sync_*：只回写发生变化的字段（调用方按 audited_fields 传入）。
    返回更新的映射行数。
    """
    if not (sync_name or sync_name_en or sync_hs or sync_inspection):
        return 0
    rows = (
        session.query(ProductMapping)
        .filter(ProductMapping.sku_code == sku.sku_code)
        .all()
    )
    for m in rows:
        if sync_name and sku.name_cn:
            m.product_name_cn = sku.name_cn
        if sync_name_en and sku.name_en:
            m.name_en = sku.name_en
        if sync_hs:
            m.hs_code = sku.hs_code
            m.is_incomplete = not (sku.hs_code or "").strip()
        if sync_inspection:
            m.inspection_required = sku.inspection_required
    session.flush()
    if rows:
        logger.info(
            "[sync] SKU %s → 品名映射反向回填 %d 行（name=%s name_en=%s hs=%s inspection=%s）",
            sku.sku_code, len(rows), sync_name, sync_name_en, sync_hs, sync_inspection,
        )
    return len(rows)
