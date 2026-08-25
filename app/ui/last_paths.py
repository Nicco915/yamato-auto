# -*- coding: utf-8 -*-
"""工作台最近使用路径持久化。

把用户每次成功发起批次时输入的 upstream_root / downstream_file_path
记录到 app/data/last_paths.json，下次打开工作台时自动回填。
与 .env 默认路径解耦：.env 是项目级静态配置；last_paths.json 是
本机用户最近操作，随用随更新，不进 git。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_LAST_PATHS_FILE = "last_paths.json"


def _last_paths_path() -> Path:
    """返回 last_paths.json 的绝对路径（放在数据目录下）。"""
    return Path(get_settings().checkpoint_db_abs).parent / _LAST_PATHS_FILE


def load_last_paths() -> dict[str, str | None]:
    """读取最近使用路径。文件不存在或损坏时返回空 dict。"""
    path = _last_paths_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 last_paths.json 失败：%s", e)
    return {}


def save_last_paths(upstream_root: str | None, downstream_file_path: str | None) -> None:
    """保存最近使用路径；空字符串视为 None，不覆盖旧值。"""
    path = _last_paths_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    current = load_last_paths()
    if upstream_root and upstream_root.strip():
        current["upstream_root"] = upstream_root.strip()
    if downstream_file_path and downstream_file_path.strip():
        current["downstream_file_path"] = downstream_file_path.strip()

    try:
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("写入 last_paths.json 失败：%s", e)
