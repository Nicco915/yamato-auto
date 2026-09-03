"""工厂文件夹预处理服务。

负责在提取前完成：
1. 解析下游装箱单得到工厂列表；
2. 把每个工厂匹配到上游子文件夹；
3. 低置信时推荐 top 3 候选；
4. 无匹配时创建空文件夹；
5. 匹配成功后自动维护 factory_aliases（可撤销）。

所有路径处理使用 pathlib.Path，兼容 macOS/Windows。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import FactoryAlias
from app.db.session import get_session
from app.factory_match import (
    load_alias_map,
    load_folder_match_candidates,
    match_factory_folder,
    recommend_candidates,
)
from app.nodes.parse_downstream import parse_downstream_file

logger = logging.getLogger(__name__)

# 高置信匹配阈值：>= 该分数直接接受
_HIGH_CONFIDENCE_THRESHOLD = 70.0


def _sanitize_folder_name(name: str) -> str:
    """跨平台文件名安全化。"""
    illegal = r'\\/:*?"<>|'
    for ch in illegal:
        name = name.replace(ch, "_")
    return name.strip("._ ") or "factory"


def _short_name_for_factory(factory_name: str) -> str | None:
    """从工厂主数据取 short_name；不存在返回 None。"""
    try:
        with get_session() as s:
            # factories.factory_name 唯一
            from app.db.models import Factory
            row = s.query(Factory).filter(Factory.factory_name == factory_name).first()
            if row and row.short_name:
                return row.short_name
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询工厂 short_name 失败: %s", exc)
    return None


def _create_empty_folder(upstream_root: Path, factory_name: str) -> Path | None:
    """为缺失工厂创建空文件夹，优先使用 short_name，否则用工厂名安全化。"""
    short = _short_name_for_factory(factory_name)
    folder_name = _sanitize_folder_name(short or factory_name)
    target = upstream_root / folder_name
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError as exc:
        logger.warning("创建空文件夹失败 %s: %s", target, exc)
        return None


def _write_alias(factory_name: str, folder_name: str) -> bool:
    """把 folder_name 作为 factory_name 的别名写入 factory_aliases（不覆盖 short_name）。

    幂等：同一 (factory_id, alias) 已存在则不重复写入。
    """
    try:
        with get_session() as s:
            from app.db.models import Factory
            factory = s.query(Factory).filter(Factory.factory_name == factory_name).first()
            if factory is None:
                # 工厂主数据不存在时无法写别名；返回 False 让调用方知道
                logger.warning("工厂主数据不存在，无法写别名: %s", factory_name)
                return False
            exists = s.query(FactoryAlias).filter(
                FactoryAlias.factory_id == factory.factory_id,
                FactoryAlias.alias == folder_name,
            ).first()
            if exists:
                return True
            alias = FactoryAlias(
                factory_id=factory.factory_id,
                alias=folder_name,
                use_folder_match=True,
                use_excel_normalize=False,
            )
            s.add(alias)
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入别名失败 %s -> %s: %s", factory_name, folder_name, exc)
        return False


def prepare_factory_folders(
    downstream_file_path: str,
    upstream_root: str,
    *,
    auto_create_empty: bool = True,
    auto_write_alias: bool = True,
) -> dict[str, Any]:
    """预处理工厂文件夹。

    返回：
    {
        "resolved": {工厂名: {"folder": "...", "method": "...", "score": 70}},
        "candidates": {工厂名: [{"folder": "...", "score": 50, "signals": [...]}]},
        "created": ["..."],          # 新建的空文件夹名
        "alias_written": ["..."],    # 自动写入的别名
        "unmatched": ["..."],        # 既无候选也未建夹的工厂
        "factory_count": 10,
    }
    """
    settings = get_settings()
    upstream_path = Path(upstream_root).expanduser()
    if not upstream_path.is_dir():
        return {"error": f"上游文件夹不存在或不是目录: {upstream_root}"}

    try:
        requirements, _ = parse_downstream_file(downstream_file_path)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"解析下游装箱单失败: {exc}"}

    factories = list(requirements.keys())
    folders = [d.name for d in upstream_path.iterdir() if d.is_dir()]
    alias_map = load_alias_map()
    folder_candidates = load_folder_match_candidates()
    cutoff = settings.fuzzy_match_score_cutoff

    resolved: dict[str, dict] = {}
    candidates: dict[str, list[dict]] = {}
    created: list[str] = []
    alias_written: list[str] = []
    unmatched: list[str] = []

    for factory in factories:
        folder, score, method = match_factory_folder(
            factory,
            folders,
            alias_map,
            cutoff=cutoff,
            overrides=None,
            folder_candidates=folder_candidates,
        )

        if folder and score >= _HIGH_CONFIDENCE_THRESHOLD:
            resolved[factory] = {"folder": folder, "score": score, "method": method}
            if auto_write_alias:
                if _write_alias(factory, folder):
                    alias_written.append(f"{factory} -> {folder}")
            continue

        # 低置信：收集候选
        recs = recommend_candidates(factory, folders, cutoff=cutoff, top_n=3)
        if recs:
            candidates[factory] = recs
            continue

        # 无候选：创建空文件夹
        if auto_create_empty:
            created_path = _create_empty_folder(upstream_path, factory)
            if created_path:
                created_folder = created_path.name
                created.append(created_folder)
                resolved[factory] = {
                    "folder": created_folder,
                    "score": 0.0,
                    "method": "created_empty",
                }
                if auto_write_alias:
                    if _write_alias(factory, created_folder):
                        alias_written.append(f"{factory} -> {created_folder}")
                continue

        unmatched.append(factory)

    return {
        "resolved": resolved,
        "candidates": candidates,
        "created": created,
        "alias_written": alias_written,
        "unmatched": unmatched,
        "factory_count": len(factories),
    }


def undo_alias_write(factory_name: str, folder_name: str) -> bool:
    """撤销最近一次自动写入的别名（仅删除 use_folder_match=True 且匹配的记录）。"""
    try:
        with get_session() as s:
            from app.db.models import Factory
            factory = s.query(Factory).filter(Factory.factory_name == factory_name).first()
            if factory is None:
                return False
            row = s.query(FactoryAlias).filter(
                FactoryAlias.factory_id == factory.factory_id,
                FactoryAlias.alias == folder_name,
                FactoryAlias.use_folder_match.is_(True),
            ).first()
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("撤销别名失败 %s -> %s: %s", factory_name, folder_name, exc)
        return False
