# -*- coding: utf-8 -*-
"""工厂名归一化 + 商检判定。"""

from __future__ import annotations

from app.split.schemas import RawItem


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