"""端到端批次发现服务。

负责：
- 扫描监控目录下的新子文件夹；
- 在每个子文件夹内自动匹配 ContentsOfTheContainer 下游装箱单；
- 返回候选批次列表供用户选择。

所有路径处理使用 pathlib.Path，兼容 macOS/Windows。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import batch_store

logger = logging.getLogger(__name__)

# 下游装箱单文件名特征（大小写不敏感）
_DOWNSTREAM_NAME_PATTERNS = [
    re.compile(r"content", re.IGNORECASE),
    re.compile(r"コンテナ", re.IGNORECASE),
    re.compile(r"装箱单", re.IGNORECASE),
]

# MX2 入荷予定リスト文件名特征（检测用，不自动写入）
_MX2_NAME_PATTERN = re.compile(r"入荷予定リスト", re.IGNORECASE)


def _sanitize_filename(name: str) -> str:
    """把字符串中的文件系统非法字符替换为下划线，用于自动创建文件夹名。"""
    # Windows/macOS/Linux 共同敏感字符
    illegal = r'\\/:*?"<>|'
    for ch in illegal:
        name = name.replace(ch, "_")
    return name.strip("._ ") or "factory"


def _is_downstream_candidate(path: Path) -> bool:
    """判断一个文件是否可能是 ContentsOfTheContainer 下游装箱单。"""
    if not path.is_file():
        return False
    if path.suffix.lower() not in (".xlsx", ".xls"):
        return False
    name = path.name
    return any(p.search(name) for p in _DOWNSTREAM_NAME_PATTERNS)


def _collect_matching(folder: Path, predicate) -> list[Path]:
    """枚举 folder 一层（不递归），返回命中 predicate 的文件，按文件名排序。"""
    hits: list[Path] = []
    try:
        for child in folder.iterdir():
            if predicate(child):
                hits.append(child)
    except OSError as exc:
        logger.warning("枚举子文件夹 %s 失败: %s", folder, exc)
    return sorted(hits, key=lambda p: p.name)


def discover_downstream_files(subfolder: Path) -> list[Path]:
    """在子文件夹内查找候选下游装箱单文件。

    - 先搜子文件夹本身这一层；
    - 无命中时向下钻一层：枚举其一级子目录各搜一层
      （覆盖「批次文件夹/中间层（如 84）/装箱单+工厂文件夹」结构，
      2026-09-07 生产实测：14 个批次全部是这种嵌套）；
    - 更深层级不钻（避免误吸无关文件）；
    - 若一个都没有返回空列表。
    """
    candidates = _collect_matching(subfolder, _is_downstream_candidate)
    if candidates:
        return candidates
    drilled: list[Path] = []
    try:
        subdirs = [c for c in subfolder.iterdir() if c.is_dir()]
    except OSError as exc:
        logger.warning("枚举子文件夹 %s 失败: %s", subfolder, exc)
        return []
    for child in sorted(subdirs, key=lambda p: p.name):
        drilled.extend(_collect_matching(child, _is_downstream_candidate))
    return sorted(drilled, key=lambda p: p.name)


def discover_mx2_files(subfolder: Path) -> list[Path]:
    """在子文件夹内查找 MX2 入荷予定リスト文件（仅检测，不写入）。

    与 discover_downstream_files 同样的两层探测（本层无命中再钻一层）。"""
    def _is_mx2(p: Path) -> bool:
        return (p.is_file() and bool(_MX2_NAME_PATTERN.search(p.name))
                and p.suffix.lower() in (".xlsx", ".xls"))

    candidates = _collect_matching(subfolder, _is_mx2)
    if candidates:
        return candidates
    drilled: list[Path] = []
    try:
        subdirs = [c for c in subfolder.iterdir() if c.is_dir()]
    except OSError as exc:
        logger.warning("枚举子文件夹 %s 失败: %s", subfolder, exc)
        return []
    for child in sorted(subdirs, key=lambda p: p.name):
        drilled.extend(_collect_matching(child, _is_mx2))
    return sorted(drilled, key=lambda p: p.name)


def _match_by_path(child: Path, records: list[dict]) -> dict | None:
    """按路径归属匹配：batch 的 upstream_root / downstream_file_path 落在
    该文件夹内（含相等）即视为该文件夹的批次。"""
    try:
        child_res = child.resolve()
    except OSError:
        child_res = child
    for b in records:
        for key in ("upstream_root", "downstream_file_path"):
            raw = b.get(key)
            if not raw:
                continue
            try:
                p = Path(raw).expanduser().resolve()
            except OSError:
                continue
            if p == child_res or child_res in p.parents:
                return b
    return None


def match_watch_folders(watch_path: Path) -> dict[str, dict]:
    """把监控目录下的子文件夹与 batches 表记录配对。

    返回 {文件夹名: batch 记录}。匹配键（任一命中即视为已建批次）：
    1. thread_id == 文件夹名（扫描建批默认约定）；
    2. folder_name == 文件夹名（thread_id 被改名或消毒过的兜底）；
    3. upstream_root / downstream_file_path 落在文件夹内
       （手动建批时 thread_id 与文件夹名无关，路径是唯一纽带）。
    """
    try:
        records = batch_store.list_batches()
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取已有 batch 失败: %s", exc)
        records = []
    by_thread = {b["thread_id"]: b for b in records}
    by_folder = {b["folder_name"]: b for b in records if b.get("folder_name")}
    matched: dict[str, dict] = {}
    try:
        children = [c for c in sorted(watch_path.iterdir(), key=lambda p: p.name)
                    if c.is_dir()]
    except OSError as exc:
        logger.warning("枚举监控目录失败: %s", watch_path, exc)
        return matched
    for child in children:
        rec = by_thread.get(child.name) or by_folder.get(child.name)
        if rec is None:
            rec = _match_by_path(child, records)
        if rec is not None:
            matched[child.name] = rec
    return matched


def scan_new_batches(
    watch_dir: str | None = None,
    *,
    skip_existing: bool = True,
) -> list[dict[str, Any]]:
    """扫描监控目录，返回尚未创建 batch 记录的候选子文件夹。

    返回结构：
    [
      {
        "folder_name": "XD439-ETD0711",
        "folder_path": "/.../1/XD439-ETD0711",
        "downstream_candidates": ["/.../ContentsOfTheContainer_xxx.xlsx"],
        "mx2_files": ["/.../青島MX2入荷予定リスト_xxx.xlsx"],
        "has_content": True,
      },
      ...
    ]
    """
    settings = get_settings()
    watch = watch_dir or settings.watch_dir
    if not watch:
        return []

    watch_path = Path(watch).expanduser()
    if not watch_path.is_dir():
        logger.warning("监控目录不存在或不是目录: %s", watch)
        return []

    matched = match_watch_folders(watch_path) if skip_existing else {}

    results: list[dict[str, Any]] = []
    try:
        for child in sorted(watch_path.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            folder_name = child.name
            if skip_existing and folder_name in matched:
                continue

            downstream_candidates = discover_downstream_files(child)
            mx2_files = discover_mx2_files(child)
            results.append({
                "folder_name": folder_name,
                "folder_path": str(child),
                "downstream_candidates": [str(p) for p in downstream_candidates],
                "mx2_files": [str(p) for p in mx2_files],
                "has_content": bool(downstream_candidates),
            })
    except OSError as exc:
        logger.warning("扫描监控目录失败: %s", exc)

    return results


# 工厂文件夹统一收容目录名（生产结构约定：
# 批次文件夹/中间层（如 93）/ContentsOfTheContainer.xlsx + 工厂/{各工厂文件夹}）
_FACTORY_DIR_NAME = "工厂"


def pick_upstream_root(base: Path) -> Path:
    """上游工厂根目录推断：base 下存在「工厂」子目录则取它
    （工厂文件夹统一收在「工厂」下的结构约定），否则返回 base 本身。"""
    factory_dir = base / _FACTORY_DIR_NAME
    return factory_dir if factory_dir.is_dir() else base


def pick_default_thread_id(folder_name: str) -> str:
    """从文件夹名生成默认 batch thread_id。

    保留中文、日文、韩文、数字、字母、._-，其余替换为下划线。
    """
    safe = re.sub(r"[^0-9A-Za-z一-鿿぀-ヿ가-힯._-]", "_", folder_name)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "batch"
