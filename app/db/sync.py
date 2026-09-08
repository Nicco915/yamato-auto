"""product_mappings ↔ factory_skus 同步与 SKU 归属工具（供流水线 / UI / 脚本复用）。

设计定型（2026-09-08 与用户确认）：产品映射是「SKU → 单位代码」查找表，
有效字段只有中文品名（匹配跳板）、unit_code、SKU 列表；一个 SKU 在映射表里
最多归属一个品名行。SKU 侧任何字段变化都不再反向回填映射行。

正向 sync_mapping_to_sku：品名映射 Tab 改名 → 批量回填绑定 SKU 的 name_cn
（仅品名；税号/商检/英文名不回填，主数据是这三项的唯一权威源）；
待完善判定 is_mapping_incomplete：unit_code 空 且 品名不是任何品名组的
源品名（组源品名如「6件套」天经地义无单位代码，不算待完善）；
归属三原子操作：
- detach_sku_from_mappings：把 SKU 从所有映射行摘除（行保留，摘空变品名级兜底行）；
- attach_sku_to_mapping：按中文品名挂接——命中追加，未命中建行（一次性继承
  税号/商检/英文名，unit_code 留空，is_incomplete 走 is_mapping_incomplete）；
- relink_sku_to_name：detach + attach 组合（品名空 → 只 detach），
  流水线老 SKU 改品名与手动编辑品名共用；
启动迁移 ensure_mapping_skus_migrated：旧 sku_code 单列只读搬迁到
product_mapping_skus 子表；启动对账 reconcile_incomplete_flags：
is_incomplete 按 is_mapping_incomplete 全量重算（均幂等，失败只记 warning）。
"""
import logging

from sqlalchemy.orm import Session

from app.db.models import (
    FactorySKU,
    ProductGroup,
    ProductMapping,
    ProductMappingSku,
)

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


def _is_blank(v) -> bool:
    """空值口径（与 mappings_api._blank 一致）：None 或纯空白字符串。

    供 scripts/merge_duplicate_mappings.py 等老脚本复用，勿删。
    """
    return v is None or (isinstance(v, str) and not v.strip())


def is_group_source_name(session: Session, product_name_cn: str | None) -> bool:
    """该品名是否是任一品名组的源品名（如「6件套」之于 set_split 组）。"""
    name = (product_name_cn or "").strip()
    if not name:
        return False
    return (
        session.query(ProductGroup)
        .filter(ProductGroup.source_name_cn == name)
        .first()
    ) is not None


def is_mapping_incomplete(
    session: Session,
    product_name_cn: str | None,
    unit_code: str | None,
) -> bool:
    """待完善统一判定（2026-09-08 定）：unit_code 空 且 品名不是品名组源品名。

    组源品名（「6件套」等复合品名）拆成组员报关，自身天经地义无单位代码，
    不算待完善——否则永远挂在待完善列表里诱导误填。
    所有 is_incomplete 写入点（attach 建行、UI 建/改、dispatcher 工具、
    品名组联动）与启动对账 reconcile_incomplete_flags 都必须走本函数。
    """
    if not _is_blank(unit_code):
        return False
    return not is_group_source_name(session, product_name_cn)


def reconcile_incomplete_flags() -> int:
    """启动幂等对账：product_mappings.is_incomplete 按 is_mapping_incomplete 全量重算。

    覆盖历史存量（旧口径=税号空）与品名组变化后的漂移；幂等可重跑，
    失败只记 warning 绝不阻断启动。返回本次翻转的行数。
    """
    from app.db.session import get_session

    try:
        changed = 0
        with get_session() as session:
            sources = {
                (g.source_name_cn or "").strip()
                for g in session.query(ProductGroup).all()
            }
            sources.discard("")
            for m in session.query(ProductMapping).all():
                # 与 is_mapping_incomplete 同口径，但批量走内存集合避免逐行查组表
                want = _is_blank(m.unit_code) and (
                    (m.product_name_cn or "").strip() not in sources
                )
                if bool(m.is_incomplete) != want:
                    m.is_incomplete = want
                    changed += 1
            session.commit()
        if changed:
            logger.info("[对账] is_incomplete 口径重算完成：翻转 %d 行", changed)
        else:
            logger.debug("[对账] is_incomplete 无需重算（0 行）")
        return changed
    except Exception as e:  # noqa: BLE001 对账失败绝不阻断启动
        logger.warning("[对账] is_incomplete 重算失败（不阻断启动）: %s", e)
        return 0


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
    """product_mappings → factory_skus 单向回填，**仅品名**（2026-09-08 收窄）。

    用途收敛为「批量改名」：映射行品名 编织袋→编织袋A，绑定 SKU 的 name_cn
    全部跟随（跳板改名，主数据跟跳板走）。税号/商检/英文名**不回填**——
    这三项以 SKU 主数据为唯一权威源，杜绝改映射污染主库。
    SKU 列表取自 product_mapping_skus 子表（旧列 sku_code 兜底）。
    返回更新的总行数（同一 SKU 多工厂行都算）。
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
    session.flush()
    return len(rows)


def detach_sku_from_mappings(session: Session, sku_code: str) -> list[ProductMapping]:
    """把 SKU 从所有包含它的映射行中摘除（一品名一 SKU 归属的前置步骤）。

    - 子表 product_mapping_skus 里 sku_code 相等的关联全部删除（不限工厂）；
    - 旧列 sku_code 等于该 SKU 的行：旧列同步为剩余列表首个 / None
      （与 attach 同约定，防启动迁移 ensure_mapping_skus_migrated 幽灵搬回）；
    - 映射行本身保留：摘空后成为品名级兜底行（设计决策，不自动删行）；
    - 返回受影响的映射行列表（调用方用于日志/用户提示）。
    """
    sku_code = (sku_code or "").strip()
    if not sku_code:
        return []
    mapping_ids = {
        row.mapping_id
        for row in session.query(ProductMappingSku)
        .filter(ProductMappingSku.sku_code == sku_code)
        .all()
    }
    # 兼容未迁移老数据：旧列精确匹配的行并入
    mapping_ids |= {
        m.id
        for m in session.query(ProductMapping)
        .filter(ProductMapping.sku_code == sku_code)
        .all()
    }
    if not mapping_ids:
        return []
    rows = (
        session.query(ProductMapping)
        .filter(ProductMapping.id.in_(mapping_ids))
        .all()
    )
    affected: list[ProductMapping] = []
    for m in rows:
        before = len(m.sku_links)
        # delete-orphan 级联：从集合移除即 flush 时删子表行
        m.sku_links[:] = [l for l in m.sku_links if l.sku_code != sku_code]
        removed = len(m.sku_links) != before
        legacy_hit = (m.sku_code or "") == sku_code
        if not removed and not legacy_hit:
            continue
        if legacy_hit:
            m.sku_code = m.sku_links[0].sku_code if m.sku_links else None
        affected.append(m)
    session.flush()
    if affected:
        logger.info(
            "[sync] 摘除 SKU %s：从 %d 行映射移除（行保留）→ %s",
            sku_code, len(affected),
            [(m.id, m.product_name_cn) for m in affected],
        )
    return affected


def attach_sku_to_mapping(
    session: Session,
    *,
    sku_code: str,
    name_cn: str | None,
    hs_code: str | None = None,
    inspection_required: bool = False,
    name_en: str | None = None,
    factory_name: str = "",
) -> str | None:
    """按中文品名挂接 SKU：命中既有映射行则追加，未命中则新建品名级行。

    - 品名 strip 后精确匹配 product_mappings（多条取最近更新，与
      lookup-by-name 同口径：updated_at 倒序 + id 倒序兜底）；
    - 命中：SKU 不在其子表列表则追加（已在则不动，幂等）；只挂接，
      映射行既有字段一概不改（unit_code 等不受影响）；
    - 未命中：新建品名级映射行，hs_code/inspection_required/name_en 从触发
      SKU 一次性继承（审核页「失焦带出税号/商检」依赖这些字段），
      unit_code 留空待人工补；is_incomplete 新语义 = unit_code 为空，
      新建行必为 True；
    - 防御：SKU 已被其他品名的映射行占用时跳过挂接并记 warning
      （正常流程调用方已先 detach，不会撞；此为兜底）。
    - factory_name 仅用于日志；品名为空直接返回 None。

    返回 "created" / "appended" / None（未动作）。
    """
    name = (name_cn or "").strip()
    sku_code = (sku_code or "").strip()
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
            "[sync] 挂接跳过：SKU %s（工厂「%s」品名「%s」）已被映射「%s」(id=%s) 占用",
            sku_code, factory_name, name,
            conflicts[0]["product_name_cn"], conflicts[0]["mapping_id"],
        )
        return None

    if mapping is None:
        mapping = ProductMapping(
            product_name_cn=name,
            hs_code=(hs_code or "").strip() or None,
            inspection_required=bool(inspection_required),
            name_en=(name_en or "").strip() or None,
            unit_code=None,  # 计量单位代码无源可继承，留空待人工补
            # 待完善统一口径：unit_code 空且非组源品名（组源品名豁免）
            is_incomplete=is_mapping_incomplete(session, name, None),
        )
        session.add(mapping)
        session.flush()  # 拿到 mapping.id 供子表挂接
        mapping.sku_links.append(ProductMappingSku(sku_code=sku_code))
        # 旧列保持与列表一致（防启动迁移幽灵搬回）
        mapping.sku_code = sku_code
        session.flush()
        logger.info(
            "[sync] 挂接：工厂「%s」SKU %s 品名「%s」→ 新建品名级映射行 "
            "(id=%s, hs_code=%s, is_incomplete=%s)",
            factory_name, sku_code, name, mapping.id, mapping.hs_code,
            mapping.is_incomplete,
        )
        return "created"

    if sku_code not in _mapping_sku_codes(mapping):
        mapping.sku_links.append(ProductMappingSku(sku_code=sku_code))
        if not mapping.sku_code:
            mapping.sku_code = sku_code  # 旧列与列表首个保持一致
        session.flush()
        logger.info(
            "[sync] 挂接：工厂「%s」SKU %s 品名「%s」→ 追加进既有映射行 (id=%s)",
            factory_name, sku_code, name, mapping.id,
        )
        return "appended"
    return None  # 幂等：SKU 已在列表中，不动


def relink_sku_to_name(
    session: Session,
    *,
    sku_code: str,
    name_cn: str | None,
    hs_code: str | None = None,
    inspection_required: bool = False,
    name_en: str | None = None,
    factory_name: str = "",
) -> dict:
    """SKU 品名归属变更的统一入口：先摘除旧归属，再按新品名挂接。

    流水线老 SKU 改品名（writer UPDATE 分支）与手动编辑 SKU 品名
    （mappings_api update_sku）共用；品名为空 → 只摘除不挂接
    （SKU 暂时不属于任何映射，待补了品名再挂）。

    返回 {"detached_from": [(mapping_id, 品名)], "action": created/appended/None}。
    """
    detached = detach_sku_from_mappings(session, sku_code)
    action = attach_sku_to_mapping(
        session,
        sku_code=sku_code,
        name_cn=name_cn,
        hs_code=hs_code,
        inspection_required=inspection_required,
        name_en=name_en,
        factory_name=factory_name,
    )
    return {
        "detached_from": [(m.id, m.product_name_cn) for m in detached],
        "action": action,
    }
