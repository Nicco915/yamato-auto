# -*- coding: utf-8 -*-
"""工厂名归一化 + 商检判定。

商检判定的唯一权威源是 factory_skus.inspection_required
（用户双屏审核的最终确认值所在表）。解析优先级：
SKU 级（factory_skus 按 工厂名+sku 命中）→ 品名级回退
（product_mappings，复用 app.declare.mapping 的索引与 lookup）
→ 都未命中默认 False（不商检）。分票图与报关生成共用本模块，
杜绝两套口径。
"""

from __future__ import annotations

import logging

from app.declare.mapping import _get, lookup
from app.db.models import Factory, FactorySKU
from app.db.session import get_session
from app.split.schemas import RawItem

logger = logging.getLogger(__name__)


def normalize_maker(raw: str, alias_map: dict[str, str]) -> str:
    """用 alias_map 归一化工厂名。

    若 raw 在 alias_map 的 key 中，返回对应的 value；
    否则返回原字符串。
    """
    return alias_map.get(raw, raw)


def classify_sj_factories(
    items: list[RawItem],
    master_inspection: dict[str, bool],
    fallback_sj_factories: list[str],
) -> dict[str, bool]:
    """双层判定：master_inspection 优先（值 True→商检），fallback_sj_factories 兜底。

    返回 {factory_name: True/False}，仅包含批次中实际出现的工厂。
    """
    result: dict[str, bool] = {}

    # 收集批次中出现过的所有工厂（取 maker 字段，已归一化）
    factories_seen: set[str] = set()
    for item in items:
        if item.maker:
            factories_seen.add(item.maker)

    for factory in sorted(factories_seen):
        if factory in master_inspection:
            result[factory] = master_inspection[factory]
        elif factory in fallback_sj_factories:
            result[factory] = True
        else:
            result[factory] = False

    return result


# ---------------------------------------------------------------------------
# SKU 级商检索引与解析
# ---------------------------------------------------------------------------

def load_sku_inspection_map(session_or_none=None) -> dict[tuple[str, str], bool]:
    """一次性加载 SKU 级商检索引：{(factory_name, sku_code): inspection_required}。

    权威源为 factory_skus.inspection_required（双屏审核最终确认值所在表），
    factory_name 经 factory_skus ⋈ factories 关联取得。

    传入 session 时复用（避免嵌套开库）；否则自开 get_session()。
    查询任何异常记 warning 并返回 {}——DB 不可用绝不阻塞主流程，
    未命中的行由 resolve_inspection 逐级回退兜底。
    """

    def _query(sess) -> dict[tuple[str, str], bool]:
        rows = (
            sess.query(
                Factory.factory_name,
                FactorySKU.sku_code,
                FactorySKU.inspection_required,
            )
            .join(Factory, FactorySKU.factory_id == Factory.factory_id)
            .all()
        )
        return {
            (factory_name, sku_code): bool(required)
            for factory_name, sku_code, required in rows
            if factory_name and sku_code
        }

    try:
        if session_or_none is not None:
            return _query(session_or_none)
        with get_session() as sess:
            return _query(sess)
    except Exception as e:  # DB 不可用绝不阻塞主流程
        logger.warning("factory_skus 商检索引查询失败,按空表处理: %s", e)
        return {}


def resolve_inspection(
    maker: str,
    sku: str,
    name_cn: str,
    sku_map: dict[tuple[str, str], bool],
    mapping_index: dict,
) -> bool:
    """解析单行的商检标志，三级优先级：

    1. SKU 级：sku_map[(maker, sku)]（factory_skus 权威源，
       用户双屏审核的最终确认值）；
    2. 品名级回退：mapping lookup（sku 传空串走品名级，
       与报关聚合的映射口径同源），命中取 inspection_required；
    3. 都未命中默认 False（不商检）。
    """
    if maker and sku:
        hit = sku_map.get((maker, sku))
        if hit is not None:
            return hit
    m = lookup(mapping_index, sku="", name_cn=name_cn)
    if m is not None:
        return bool(_get(m, "inspection_required", False))
    return False