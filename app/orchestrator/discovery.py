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


def discover_downstream_files(subfolder: Path) -> list[Path]:
    """在子文件夹内查找候选下游装箱单文件。

    - 只搜索子文件夹本身这一层，不递归；
    - 返回所有命中特征的文件路径；
    - 若一个都没有返回空列表。
    """
    candidates: list[Path] = []
    try:
        for child in subfolder.iterdir():
            if _is_downstream_candidate(child):
                candidates.append(child)
    except OSError as exc:
        logger.warning("枚举子文件夹 %s 失败: %s", subfolder, exc)
    return sorted(candidates, key=lambda p: p.name)


def discover_mx2_files(subfolder: Path) -> list[Path]:
    """在子文件夹内查找 MX2 入荷予定リスト文件（仅检测，不写入）。"""
    candidates: list[Path] = []
    try:
        for child in subfolder.iterdir():
            if child.is_file() and _MX2_NAME_PATTERN.search(child.name) \
                    and child.suffix.lower() in (".xlsx", ".xls"):
                candidates.append(child)
    except OSError as exc:
        logger.warning("枚举子文件夹 %s 失败: %s", subfolder, exc)
    return sorted(candidates, key=lambda p: p.name)


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

    existing_thread_ids: set[str] = set()
    if skip_existing:
        try:
            existing_thread_ids = {b["thread_id"] for b in batch_store.list_batches()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取已有 batch 失败: %s", exc)

    results: list[dict[str, Any]] = []
    try:
        for child in sorted(watch_path.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            folder_name = child.name
            # 用文件夹名作为默认 thread_id；允许后续用户修改
            if skip_existing and folder_name in existing_thread_ids:
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


def pick_default_thread_id(folder_name: str) -> str:
    """从文件夹名生成默认 batch thread_id。

    保留中文、日文、韩文、数字、字母、._-，其余替换为下划线。
    """
    safe = re.sub(r"[^0-9A-Za-z一-鿿぀-ヿ가-힯._-]", "_", folder_name)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_") or "batch"
