"""审核页 payload 构建（Node5 首次审核 + reopen 共用）。

把 `human_review.human_review()` 里构建 review_payload / items_payload 的逻辑
抽出来，让 reopen 也能复用同一份构建逻辑，保证两侧界面字段一致。

字段约定（与第一阶段.md 第 6 节、review.html 数据消费契约对齐）：
- items: 每条含 sku / extracted_data / calculation / status /
  is_human_edited / is_new_sku / db_record / error_msg / unexpected_sku
- 新 SKU 多带 fields_to_fill / inspection_required（工厂默认）
- 老 SKU 多带顶层 name_cn / hs_code / inspection_required（来自 db_record）
- 顶层 payload: factory_name / folder_path / source_documents /
  missing_skus / items / extraction_issues / extraction_coverage /
  weight_diff_warn_ratio
- 失败兜底分支透传 final_attempt / failure_reason
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Factory, FactoryAlias
from app.db.session import get_session

logger = logging.getLogger(__name__)

# 新 SKU 需人工补录的合规字段清单（与 Node5 一致）
NEW_SKU_REQUIRED_FIELDS = ["name_cn", "hs_code", "inspection_required"]

# 工厂商检默认值（按工厂名）
_FACTORY_INSPECTION_CACHE: dict[str, int] | None = None


def _load_factory_inspection_defaults() -> dict[str, int]:
    """加载 factory_inspection_defaults.json，缓存到模块级变量。"""
    global _FACTORY_INSPECTION_CACHE
    if _FACTORY_INSPECTION_CACHE is not None:
        return _FACTORY_INSPECTION_CACHE
    config_path = Path(__file__).parents[1] / "config" / "factory_inspection_defaults.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            _FACTORY_INSPECTION_CACHE = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("[review_payload] 加载工厂商检配置失败，使用默认值: %s", e)
        _FACTORY_INSPECTION_CACHE = {}
    return _FACTORY_INSPECTION_CACHE


def reset_factory_inspection_cache() -> None:
    """测试用：清空商检默认值缓存。"""
    global _FACTORY_INSPECTION_CACHE
    _FACTORY_INSPECTION_CACHE = None


def build_review_items_payload(
    calculated_items: list[dict],
    factory_name: str,
) -> list[dict]:
    """构建审核页 items（与 Node5 首次审核完全一致）。

    - 新 SKU：补 fields_to_fill 与 inspection_required（工厂默认）
    - 老 SKU：从 db_record 提取顶层 name_cn / hs_code / inspection_required
    - 透传 sku / extracted_data / calculation / status / error_msg /
      is_human_edited / is_new_sku / db_record / unexpected_sku
    """
    inspection_defaults = _load_factory_inspection_defaults()
    default_inspection = inspection_defaults.get(factory_name, 0)

    items_payload: list[dict] = []
    for item in calculated_items or []:
        entry: dict[str, Any] = {
            "sku": item.get("sku"),
            "extracted_data": item.get("extracted_data"),
            "calculation": item.get("calculation"),
            "status": item.get("status"),
            "is_human_edited": item.get("is_human_edited", False),
            "is_new_sku": item.get("is_new_sku", False),
            "db_record": item.get("db_record") or {},
            "error_msg": item.get("error_msg"),
            "unexpected_sku": item.get("unexpected_sku", False),
        }
        if item.get("is_new_sku"):
            entry["fields_to_fill"] = list(NEW_SKU_REQUIRED_FIELDS)
            entry["inspection_required"] = default_inspection
        else:
            db = entry["db_record"]
            entry["name_cn"] = db.get("name_cn")
            entry["hs_code"] = db.get("hs_code")
            entry["inspection_required"] = db.get("inspection_required")
        items_payload.append(entry)
    return items_payload


# 低置信匹配档：审核页给别名建议卡（高置信档由 Node6 C 级自动回填 short_name）
_FUZZY_SUGGESTION_METHODS = frozenset({"fuzzy", "contains"})


def _build_alias_suggestion(cur: dict, overrides: dict | None) -> dict | None:
    """构建审核页别名建议卡数据，无建议时返回 None。

    判定规则：
    - 当前工厂在批次覆盖 overrides 里 → kind="override"，folder=覆盖值；
    - 否则 match_method ∈ {fuzzy, contains} 且 folder_path 非空
      → kind="fuzzy"，folder=文件夹名；
    - 其他情况（含高置信档 / 未匹配）→ None。

    已沉淀去重：该工厂的别名已在 factory_aliases 且指向工厂的
    short_name == folder（即已经存过了）→ None，不再提示。
    DB 查询是辅助设施：任何异常只记警告并返回 None，绝不阻塞审核挂起。
    """
    factory_name = cur.get("factory_name")
    if not factory_name:
        return None

    kind: str | None = None
    folder: str | None = None
    if overrides and factory_name in overrides:
        kind, folder = "override", overrides[factory_name]
    elif (cur.get("match_method") in _FUZZY_SUGGESTION_METHODS
          and cur.get("folder_path")):
        kind, folder = "fuzzy", Path(cur["folder_path"]).name
    if not kind or not folder:
        return None

    try:
        with get_session() as session:
            factory = session.scalar(
                select(Factory).where(Factory.factory_name == factory_name)
            )
            current_short_name = factory.short_name if factory else None
            already_saved = session.scalar(
                select(FactoryAlias.id)
                .join(Factory, FactoryAlias.factory_id == Factory.factory_id)
                .where(
                    FactoryAlias.alias == factory_name,
                    FactoryAlias.use_folder_match.is_(True),
                    Factory.short_name == folder,
                )
            )
            if already_saved is not None:
                return None
    except Exception as e:  # noqa: BLE001 建议卡是辅助设施，失败静默降级
        logger.warning("[review_payload] alias_suggestion 查库失败，按无建议处理："
                       "%s: %s", type(e).__name__, e)
        return None

    return {
        "factory": factory_name,
        "folder": folder,
        "kind": kind,
        "match_score": float(cur.get("match_score") or 0),
        "current_short_name": current_short_name,
        "conflict": bool(current_short_name and current_short_name != folder),
    }


def build_review_payload(cur: dict, overrides: dict | None = None) -> dict:
    """构建完整 review payload（与 Node5 首次审核完全一致）。

    cur 是 current_factory_data 快照（必须包含 factory_name / calculated_items；
    缺省字段视作空，按 Node5 行为兜底）。
    overrides 是 state["factory_alias_overrides"]（批次级「仅本次生效」对照），
    仅用于 alias_suggestion 的 override 判定；reopen 路径无此上下文可省略。
    """
    factory_name = cur.get("factory_name")
    items_payload = build_review_items_payload(
        cur.get("calculated_items") or [], factory_name,
    )

    payload: dict[str, Any] = {
        "factory_name": cur.get("factory_name"),
        "folder_path": cur.get("folder_path"),
        "source_documents": cur.get("source_documents") or [],
        "missing_skus": cur.get("missing_skus") or [],
        "items": items_payload,
        "extraction_issues": cur.get("extraction_issues") or [],
        "extraction_coverage": cur.get("extraction_coverage") or {},
        "weight_diff_warn_ratio": get_settings().weight_diff_warn_ratio,
        # 新工厂别名建议卡（A 级）：override 命中 / fuzzy|contains 低置信命中时给出；
        # 无建议或别名已沉淀过则为 None（前端按字段存在性渲染）
        "alias_suggestion": _build_alias_suggestion(cur, overrides),
    }

    # W6a 暂缓二遍重试仍失败的最终挂起透传标记
    if cur.get("is_final_attempt") and cur.get("extraction_ok") is False:
        payload["final_attempt"] = True
        payload["failure_reason"] = cur.get("failure_reason") or "unknown"

    return payload
