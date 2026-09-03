"""下游装箱单差异对比器。

比较新旧两份 ContentsOfTheContainer，输出：
- 新增工厂 / 删除工厂
- 列结构变化（列名增删）
- 各工厂 SKU 级内容变化
- 推荐策略：diff（只刷新变化工厂）或 full（全量重识别）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)


def _read_content(path: str | Path) -> pd.DataFrame | None:
    """读取 Content 表，SKU 按字符串处理。"""
    try:
        settings = get_settings()
        df = pd.read_excel(str(Path(path).expanduser()), sheet_name=0,
                           dtype={settings.col_sku: str})
        df[settings.col_sku] = df[settings.col_sku].astype(str).str.strip()
        df[settings.col_factory] = df[settings.col_factory].astype(str).str.strip()
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 Content 表失败 %s: %s", path, exc)
        return None


def _factory_items(df: pd.DataFrame) -> dict[str, dict[str, dict[str, Any]]]:
    """把 DataFrame 转成 {工厂: {SKU: {列名: 值}}}。"""
    settings = get_settings()
    factory_col = settings.col_factory
    sku_col = settings.col_sku
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for _, row in df.iterrows():
        factory = row.get(factory_col)
        sku = row.get(sku_col)
        if not factory or factory == "nan" or not sku or sku == "nan":
            continue
        result.setdefault(factory, {})[sku] = row.to_dict()
    return result


def compare_downstream(
    old_path: str | Path,
    new_path: str | Path,
) -> dict[str, Any]:
    """对比新旧 Content 表，返回差异结构。"""
    old_df = _read_content(old_path)
    new_df = _read_content(new_path)

    if old_df is None:
        return {"error": f"无法读取旧文件: {old_path}"}
    if new_df is None:
        return {"error": f"无法读取新文件: {new_path}"}

    old_cols = list(old_df.columns)
    new_cols = list(new_df.columns)

    # 列结构变化
    added_cols = [c for c in new_cols if c not in old_cols]
    removed_cols = [c for c in old_cols if c not in new_cols]
    structure_changed = bool(added_cols or removed_cols)

    old_items = _factory_items(old_df)
    new_items = _factory_items(new_df)

    old_factories = set(old_items.keys())
    new_factories = set(new_items.keys())

    added_factories = sorted(new_factories - old_factories)
    removed_factories = sorted(old_factories - new_factories)

    changed_factories: list[str] = []
    unchanged_factories: list[str] = []

    for factory in sorted(old_factories & new_factories):
        old_skus = old_items[factory]
        new_skus = new_items[factory]
        if set(old_skus.keys()) != set(new_skus.keys()):
            changed_factories.append(factory)
            continue
        # 比较每行关键字段
        different = False
        for sku in old_skus:
            old_row = old_skus[sku]
            new_row = new_skus[sku]
            # 比较所有共有列（忽略 NaN 与字符串 nan）
            for col in old_cols:
                if col not in new_row:
                    different = True
                    break
                ov = old_row.get(col)
                nv = new_row.get(col)
                # 统一 NaN
                if pd.isna(ov) and pd.isna(nv):
                    continue
                if str(ov).strip() != str(nv).strip():
                    different = True
                    break
            if different:
                break
        if different:
            changed_factories.append(factory)
        else:
            unchanged_factories.append(factory)

    # 策略推荐
    # 当前版本对「已有工厂内容变化」没有安全的单工厂 diff 重提能力，
    # 因此内容变化也推荐 full；仅新增/删除工厂时走 diff 补充。
    if structure_changed:
        recommendation = "full"
        reason = "列结构发生变化"
    elif changed_factories:
        recommendation = "full"
        reason = "部分工厂内容变化"
    elif added_factories or removed_factories:
        recommendation = "diff"
        reason = "工厂增删，按差异处理"
    else:
        recommendation = "none"
        reason = "无变化"

    return {
        "old_path": str(old_path),
        "new_path": str(new_path),
        "structure_changed": structure_changed,
        "added_columns": added_cols,
        "removed_columns": removed_cols,
        "added_factories": added_factories,
        "removed_factories": removed_factories,
        "changed_factories": changed_factories,
        "unchanged_factories": unchanged_factories,
        "recommendation": recommendation,
        "reason": reason,
    }
