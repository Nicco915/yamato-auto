# -*- coding: utf-8 -*-
"""报关产品映射查询——把 product_mappings 记录转成三级索引。

纯函数，不碰 DB：调用方负责把 ORM 对象或 dict 列表传进来。
记录需具备字段（属性或键）：product_name_cn、factory_id、
inspection_required、unit_code（其余字段原样保留在索引里）。

SKU 取值（一品名多 SKU）：ORM 对象走 sku_links 关系（需在 session 内或已预取），
dict 走 sku_codes 键；为兼容旧调用方，sku_code 单值（属性或键）也认。
"""

from __future__ import annotations

from typing import Any


def _get(rec: Any, key: str, default=None):
    """同时支持 ORM 对象与 dict 的取值。"""
    if isinstance(rec, dict):
        return rec.get(key, default)
    return getattr(rec, key, default)


def _record_skus(rec: Any) -> list[str]:
    """从记录提取 SKU 列表：sku_links（ORM）→ sku_codes（dict）→ sku_code 单值兜底。"""
    links = _get(rec, "sku_links")
    if links:
        return [
            c for c in (_get(link, "sku_code") for link in links) if c
        ]
    codes = _get(rec, "sku_codes")
    if codes:
        return [c for c in codes if c]
    single = _get(rec, "sku_code")
    return [single] if single else []


def build_mapping_index(mappings: list) -> dict:
    """product_mappings 记录 → 三级索引。

    Returns:
        {
            "by_sku":         {sku_code: mapping},                    # SKU 精确
            "by_name_factory": {(product_name_cn, factory_id): mapping},  # 品名+工厂
            "by_name":        {product_name_cn: mapping},             # 品名级
        }
        同一键多条时后写覆盖先写（品名级主匹配键设计上唯一）。
    """
    by_sku: dict[str, Any] = {}
    by_name_factory: dict[tuple[str, Any], Any] = {}
    by_name: dict[str, Any] = {}
    for m in mappings:
        name = _get(m, "product_name_cn")
        factory_id = _get(m, "factory_id")
        if not name:
            continue
        for sku in _record_skus(m):
            by_sku[sku] = m  # 一个映射行的每个 SKU 都进精确索引
        if factory_id is not None:
            by_name_factory[(name, factory_id)] = m
        by_name[name] = m
    return {"by_sku": by_sku, "by_name_factory": by_name_factory, "by_name": by_name}


def lookup(index: dict, sku: str, name_cn: str, factory_id=None):
    """按优先级查映射：SKU 精确 > 品名+工厂 > 品名。

    返回 mapping 记录或 None（未命中由调用方记 warning，不阻断）。
    """
    if sku and sku in index["by_sku"]:
        return index["by_sku"][sku]
    if factory_id is not None:
        hit = index["by_name_factory"].get((name_cn, factory_id))
        if hit is not None:
            return hit
    return index["by_name"].get(name_cn)
