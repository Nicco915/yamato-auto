"""product_mappings ↔ factory_skus 双向同步工具（供 UI 与脚本复用）。

正向 sync_mapping_to_sku：品名映射 Tab 编辑 → 回填 SKU 主数据；
反向 sync_sku_to_mapping：SKU 主数据 Tab 编辑 → 回填品名映射（仅 SKU 级行）；
自动挂接 auto_link_new_sku_to_mapping：Node6 新 SKU 落库后按品名挂/建映射行；
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


def _mapping_sku_codes(mapping: ProductMapping) -> list[str]:
    """取映射行的 SKU 列表：子表为准（按 id 排序），旧列 sku_code 兜底。

    旧列兜底存在的意义：supplement_sku_mappings 等老脚本只写旧列、
    以及个别未迁移数据；去重保序，空串剔除。
    """
    # 未 flush 的新行 id 为 None：排在已持久化行之后，稳定排序保持插入顺序
    codes = [
        link.sku_code
        for link in sorted(
            mapping.sku_links, key=lambda l: (l.id is None, l.id or 0))
    ]
    if not codes and mapping.sku_code:
        codes = [mapping.sku_code]
    seen: set[str] = set()
    result: list[str] = []
    for c in codes:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def check_sku_conflicts(
    session: Session,
    sku_codes: list[str],
    *,
    exclude_mapping_id: int | None = None,
) -> list[dict]:
    """检查 SKU 列表是否被**其他**映射行占用。

    返回冲突列表 [{sku_code, mapping_id, product_name_cn}]；空列表 = 无冲突。
    UI 层据此抛 409，dispatcher 据此拼中文错误说明；调用方保证整体不落库。
    """
    codes = [c for c in dict.fromkeys(sku_codes) if c]
    if not codes:
        return []
    query = (
        session.query(ProductMappingSku, ProductMapping)
        .join(ProductMapping, ProductMappingSku.mapping_id == ProductMapping.id)
        .filter(ProductMappingSku.sku_code.in_(codes))
    )
    if exclude_mapping_id is not None:
        query = query.filter(ProductMappingSku.mapping_id != exclude_mapping_id)
    return [
        {
            "sku_code": link.sku_code,
            "mapping_id": m.id,
            "product_name_cn": m.product_name_cn,
        }
        for link, m in query.all()
    ]


def sync_mapping_to_sku(session: Session, mapping: ProductMapping) -> int:
    """product_mappings → factory_skus 单向回填（多 SKU：逐个回填列表中每个 SKU）。

    SKU 列表取自 product_mapping_skus 子表（旧列 sku_code 兜底），
    对每个 SKU 回填 factory_skus 的 name_cn/hs_code/inspection_required，
    name_en 非空时也回填。返回更新的总行数（同一 SKU 多工厂行都算）。
    仅回填，不删不改其他字段（unit_net_weight/unit_gross_weight 不动）。
    """
    codes = _mapping_sku_codes(mapping)
    if not codes:
        return 0
    rows = (
        session.query(FactorySKU)
        .filter(FactorySKU.sku_code.in_(codes))
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

    命中范围：product_mapping_skus 子表里 sku_code 等于该 SKU 的**所有**映射行
    （一品名多 SKU 后，含该 SKU 的每一行都算 SKU 级行，不限工厂全部更新）；
    为兼容未迁移的老数据，旧列 sku_code 精确匹配的行也并入。
    品名级行（SKU 列表为空）可能被多个 SKU 共享兜底，绝不触碰。

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
    # 子表反查：含该 SKU 的所有映射行（多 SKU 化后的主路径）
    link_ids = [
        row.mapping_id
        for row in session.query(ProductMappingSku)
        .filter(ProductMappingSku.sku_code == sku.sku_code)
        .all()
    ]
    # 兼容未迁移老数据：旧列 sku_code 精确匹配的行并入（去重）
    legacy_ids = [
        m.id
        for m in session.query(ProductMapping)
        .filter(ProductMapping.sku_code == sku.sku_code)
        .all()
    ]
    ids = list(dict.fromkeys(link_ids + legacy_ids))
    if not ids:
        return 0
    rows = (
        session.query(ProductMapping)
        .filter(ProductMapping.id.in_(ids))
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


def _is_blank(v) -> bool:
    """空值口径（与 mappings_api._blank 一致）：None 或纯空白字符串。"""
    return v is None or (isinstance(v, str) and not v.strip())


def auto_link_new_sku_to_mapping(
    session: Session,
    *,
    factory_name: str,
    sku_code: str,
    name_cn: str | None,
    hs_code: str | None = None,
    inspection_required: bool = False,
    name_en: str | None = None,
) -> str | None:
    """新 SKU 落库后的自动挂接：按中文品名挂/建产品映射行。

    只在 Node6 真·新 SKU（INSERT 分支）后由 writer 调用；name_cn 为空直接返回。
    - 品名 strip 后精确匹配 product_mappings（多条取最近更新，与
      lookup-by-name 同口径：updated_at 倒序 + id 倒序兜底）；
    - 命中：SKU 不在其子表列表则追加（已在则不动，幂等）；只挂接，
      不做反向回填（映射行既有字段一概不改，unit_code 等不受影响）；
    - 未命中：新建品名级映射行，hs_code/inspection_required/name_en 从
      该 SKU 继承，unit_code 留空，is_incomplete 按 hs_code 是否为空；
    - 防御：SKU 已被其他品名的映射行占用时跳过挂接并记 warning
      （与 UI 409 同语义，杜绝一品名一 SKU 约束被自动流程撞破）。

    返回 "created" / "appended" / None（未动作）。
    """
    name = (name_cn or "").strip()
    if not name or not sku_code:
        return None

    mapping = (
        session.query(ProductMapping)
        .filter(ProductMapping.product_name_cn == name)
        # 最新更新优先；updated_at 可能同秒并列，id 倒序兜底保证确定性
        .order_by(ProductMapping.updated_at.desc(), ProductMapping.id.desc())
        .first()
    )

    # 同 SKU 已被其他品名映射占用：自动流程不抢挂，留人工裁决
    # （exclude 按品名命中的行：SKU 已在该行列表里的幂等重跑不算冲突）
    conflicts = check_sku_conflicts(
        session, [sku_code],
        exclude_mapping_id=mapping.id if mapping is not None else None,
    )
    if conflicts:
        logger.warning(
            "[sync] 自动挂接跳过：SKU %s（工厂「%s」品名「%s」）已被映射「%s」(id=%s) 占用",
            sku_code, factory_name, name,
            conflicts[0]["product_name_cn"], conflicts[0]["mapping_id"],
        )
        return None

    if mapping is None:
        hs = (hs_code or "").strip() or None
        mapping = ProductMapping(
            product_name_cn=name,
            hs_code=hs,
            inspection_required=bool(inspection_required),
            name_en=(name_en or "").strip() or None,
            unit_code=None,  # 计量单位代码无源可继承，留空待人工补
            is_incomplete=_is_blank(hs),
        )
        session.add(mapping)
        session.flush()  # 拿到 mapping.id 供子表挂接
        mapping.sku_links.append(ProductMappingSku(sku_code=sku_code))
        # 旧列保持与列表一致（防启动迁移幽灵搬回，语义同 _replace_sku_links）
        mapping.sku_code = sku_code
        session.flush()
        logger.info(
            "[sync] 自动挂接：工厂「%s」新 SKU %s 品名「%s」→ 新建品名级映射行 "
            "(id=%s, hs_code=%s, is_incomplete=%s)",
            factory_name, sku_code, name, mapping.id, hs, mapping.is_incomplete,
        )
        return "created"

    if sku_code not in _mapping_sku_codes(mapping):
        mapping.sku_links.append(ProductMappingSku(sku_code=sku_code))
        if not mapping.sku_code:
            mapping.sku_code = sku_code  # 旧列与列表首个保持一致
        session.flush()
        logger.info(
            "[sync] 自动挂接：工厂「%s」新 SKU %s 品名「%s」→ 追加进既有映射行 (id=%s)",
            factory_name, sku_code, name, mapping.id,
        )
        return "appended"
    return None  # 幂等：SKU 已在列表中，不动
