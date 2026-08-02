"""工厂名匹配唯一事实来源（W5）：规范化 / alias 读写 / 分档匹配 / 候选推荐。

Node2（folder_router）与批次预扫（service/tools）共用本模块：
- match_factory_folder：五档查找 —— 批次覆盖(override) → alias 精确 →
  alias 大小写不敏感 → 规范化精确 → rapidfuzz 模糊匹配；
- recommend_candidates：预扫「低置信推荐」档的候选生成，
  rapidfuzz top-N + 包含强信号（如 天津市依依衛生用品 ⊃ 依依）保底 70 分；
- load/save_alias_entries：alias_map.json 的读写，损坏容错、原子写、
  写前 .bak 备份、模块级锁串行。
"""
import json
import logging
import os
import shutil
import threading
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

from app.config import get_settings

logger = logging.getLogger(__name__)

# save_alias_entries 串行化：同一进程内并发保存互不踩踏
_save_lock = threading.Lock()


def normalize_name(name: str) -> str:
    """规范化名称：全角转半角、去空白、小写，提高模糊匹配命中率。"""
    s = unicodedata.normalize("NFKC", name)
    return "".join(s.split()).lower()


def _alias_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return get_settings().alias_map_abs


def load_alias_map(path: str | Path | None = None) -> dict:
    """读取 alias_map.json。文件不存在或 JSON 损坏时记日志并返回 {}，
    绝不抛异常（损坏即崩曾打崩 Node2 整条提取线）。"""
    p = _alias_path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.error("alias_map 读取失败,按空表处理: %s (%s)", p, e)
        return {}
    if not isinstance(data, dict):
        logger.error("alias_map 顶层不是 dict,按空表处理: %s", p)
        return {}
    return data


def match_factory_folder(
    factory: str,
    folders: list[str],
    alias_map: dict,
    cutoff: float,
    overrides: dict | None = None,
) -> tuple[str | None, float, str]:
    """按五档顺序为工厂名匹配本地文件夹。

    返回 (文件夹名|None, 得分, 方式)。方式为
    override / alias / alias_ci / exact / fuzzy / none 之一。
    override/alias 指向的文件夹不存在时落到后续档位（不硬失败）。
    """
    if not folders:
        return None, 0.0, "none"

    # 1) 批次级覆盖（用户本轮确认的「仅本次生效」对照，不落盘）
    if overrides:
        ov = overrides.get(factory)
        if ov and ov in folders:
            return ov, 100.0, "override"
        if ov:
            logger.warning(
                "批次覆盖「%s」->「%s」指向的文件夹不存在,落后续匹配档",
                factory, ov)

    # 2) 别名映射表精确命中（跨语言场景的确定性解法）
    alias_hit = (alias_map or {}).get(factory)
    if alias_hit and alias_hit in folders:
        return alias_hit, 100.0, "alias"

    # 3) 别名大小写不敏感兜底：
    # Windows/macOS 文件系统不区分大小写（"TOP" 与 "Top" 是同一文件夹），
    # 但 Python 字符串比较区分大小写。Linux 文件系统大小写敏感，
    # 不敏感匹配可能匹错目录，故仅在精确匹配失败时启用并打印日志提示。
    if alias_hit:
        ci_hit = next(
            (f for f in folders if f.lower() == alias_hit.lower()), None
        )
        if ci_hit:
            logger.info(
                "别名「%s」精确匹配未命中,大小写不敏感兜底命中文件夹「%s」",
                alias_hit, ci_hit)
            return ci_hit, 100.0, "alias_ci"

    # 4) 规范化后精确命中
    norm_map = {normalize_name(f): f for f in folders}
    norm_factory = normalize_name(factory)
    if norm_factory in norm_map:
        return norm_map[norm_factory], 100.0, "exact"

    # 5) rapidfuzz 模糊匹配兜底
    hit = process.extractOne(
        norm_factory,
        list(norm_map.keys()),
        scorer=fuzz.ratio,
        score_cutoff=cutoff,
    )
    if hit:
        return norm_map[hit[0]], hit[1], "fuzzy"

    return None, 0.0, "none"


def recommend_candidates(
    factory: str,
    folders: list[str],
    cutoff: float,
    top_n: int = 3,
) -> list[dict]:
    """为存疑工厂推荐候选文件夹（预扫「低置信推荐」档用）。

    返回 [{"folder", "score", "signals"}]，按分数降序、最多 top_n 条。
    signals 取值：
    - "contains"：规范化后工厂名包含文件夹名（如 天津市依依衛生用品 ⊃ 依依）；
    - "contained_by"：文件夹名包含工厂名。
    命中任一包含信号的候选 score 保底 70，且不受 fuzzy top-N 漏召影响。
    """
    if not folders:
        return []

    norm_factory = normalize_name(factory)
    norm_map = {normalize_name(f): f for f in folders}
    entries: dict[str, dict] = {}

    def _entry(folder: str) -> dict:
        return entries.setdefault(
            folder, {"folder": folder, "score": 0.0, "signals": []})

    # 包含强信号：全量扫描（fuzzy top-N 可能漏掉短名包含项）
    for norm_name, folder in norm_map.items():
        e = _entry(folder)
        if norm_name and norm_name in norm_factory:
            e["signals"].append("contains")
        if norm_factory and norm_factory in norm_name:
            e["signals"].append("contained_by")

    # rapidfuzz top-N
    for norm_name, score, _ in process.extract(
        norm_factory, list(norm_map.keys()), scorer=fuzz.ratio, limit=top_n
    ):
        e = _entry(norm_map[norm_name])
        e["score"] = max(e["score"], score)

    out = []
    for e in entries.values():
        if e["signals"]:
            e["score"] = max(e["score"], 70.0)
        if e["score"] >= cutoff:
            out.append(e)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_n]


def save_alias_entries(
    entries: dict,
    path: str | Path | None = None,
) -> dict:
    """追加/覆盖 alias 对照并落盘。

    模块级锁串行；写前备份 .bak；临时文件 + os.replace 原子写；
    已有 key 写入不同值时记入 overwritten。返回
    {"saved": 写入条数, "overwritten": [被覆盖的 key], "path": 落盘路径}。
    """
    p = _alias_path(path)
    with _save_lock:
        existing = load_alias_map(p)
        overwritten = [k for k, v in entries.items()
                       if k in existing and existing[k] != v]
        merged = {**existing, **entries}

        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + ".bak"))
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(tmp, p)

    if overwritten:
        logger.warning("alias 对照覆盖既有 key: %s", overwritten)
    return {"saved": len(entries), "overwritten": overwritten,
            "path": str(p)}
