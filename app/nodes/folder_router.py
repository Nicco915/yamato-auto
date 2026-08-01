"""Node 2: 文件夹路由与读取（Folder Router Node）。

从 pending_factories 队列弹出一个工厂，定位本地上游文件夹：
1. 先查 alias_map.json（人工维护：下游日文/英文名 -> 本地中文文件夹名），
   解决跨语言完全无法模糊匹配的问题（如 山東中地 -> 中地）；
2. 查不到再用 rapidfuzz 对规范化后的名字做模糊匹配兜底。

匹配到后列出文件夹下所有单据文件（PDF/Excel/图片），写入 state。
"""
import json
import logging
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

from app.config import get_settings
from app.logging_config import bind_context
from app.state import AgentState

logger = logging.getLogger(__name__)

# 支持的上游单据扩展名（.doc/.docx 走 doc_channel：soffice/textutil 转换）
SUPPORTED_EXTS = {".pdf", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".csv",
                  ".doc", ".docx"}


def _normalize(name: str) -> str:
    """规范化名称：全角转半角、去空白、小写，提高模糊匹配命中率。"""
    s = unicodedata.normalize("NFKC", name)
    return "".join(s.split()).lower()


def _load_alias_map() -> dict:
    path = get_settings().alias_map_abs
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def folder_router(state: AgentState) -> dict:
    settings = get_settings()
    queue = list(state.get("pending_factories") or [])
    if not queue:
        # 队列已空，理论上不会走到这里（Node6 条件边已拦截）
        return {"current_factory_data": {}}

    factory = queue.pop(0)

    # L2 日志关联：多工厂循环的调度入口，进入某工厂处理即绑定工厂名。
    # 实测：LangGraph 每个节点在拷贝的 context 中执行，节点内 set 不外泄
    # （后续节点如需工厂名要各自从 state 绑定），故此处绑定只覆盖本节点
    # 日志，离开自动失效、无需显式清理；批次号由 service 层绑定并随
    # context 拷贝传播进所有节点。
    bind_context(factory=factory)

    upstream_root = Path(state.get("upstream_root") or settings.upstream_root)
    expected_skus = (state.get("downstream_requirements") or {}).get(factory, [])

    folder_path: str | None = None
    match_score = 0.0

    if upstream_root.is_dir():
        folders = [d.name for d in upstream_root.iterdir() if d.is_dir()]

        # 1) 别名映射表优先（跨语言场景的确定性解法）
        alias_map = _load_alias_map()
        alias_hit = alias_map.get(factory)
        if alias_hit and alias_hit in folders:
            folder_path = str(upstream_root / alias_hit)
            match_score = 100.0
        elif alias_hit and folders:
            # 精确匹配失败时追加一次大小写不敏感兜底：
            # Windows/macOS 文件系统不区分大小写（"TOP" 与 "Top" 是同一文件夹），
            # 但 Python 字符串比较区分大小写。Linux 文件系统大小写敏感，
            # 不敏感匹配可能匹错目录，故仅在精确匹配失败时启用并打印日志提示。
            ci_hit = next(
                (f for f in folders if f.lower() == alias_hit.lower()), None
            )
            if ci_hit:
                logger.info(
                    "[Node2] 别名「%s」精确匹配未命中，"
                    "大小写不敏感兜底命中文件夹「%s」", alias_hit, ci_hit)
                folder_path = str(upstream_root / ci_hit)
                match_score = 100.0
        if folder_path is None and folders:
            # 2) rapidfuzz 模糊匹配兜底（先精确比对规范化结果，再算相似分）
            norm_map = {_normalize(f): f for f in folders}
            if _normalize(factory) in norm_map:
                folder_path = str(upstream_root / norm_map[_normalize(factory)])
                match_score = 100.0
            else:
                hit = process.extractOne(
                    _normalize(factory),
                    list(norm_map.keys()),
                    scorer=fuzz.ratio,
                    score_cutoff=settings.fuzzy_match_score_cutoff,
                )
                if hit:
                    folder_path = str(upstream_root / norm_map[hit[0]])
                    match_score = hit[1]

    # 收集该文件夹下的单据文件
    source_documents: list[str] = []
    if folder_path:
        source_documents = sorted(
            str(p) for p in Path(folder_path).rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )

    logger.info(
        "[Node2] 工厂「%s」-> 文件夹 %s (得分 %s)，单据 %d 个，期望 SKU %d 个",
        factory, folder_path or "未匹配", match_score,
        len(source_documents), len(expected_skus))

    return {
        "pending_factories": queue,
        "current_factory_data": {
            "factory_name": factory,
            "folder_path": folder_path,
            "match_score": match_score,
            "source_documents": source_documents,
            "expected_skus": expected_skus,
            "extracted_items": [],
            "calculated_items": [],
            "missing_skus": [],
        },
    }
