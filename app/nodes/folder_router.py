"""Node 2: 文件夹路由与读取（Folder Router Node）。

从 pending_factories 队列弹出一个工厂，定位本地上游文件夹：
1. 先查 alias_map.json（人工维护：下游日文/英文名 -> 本地中文文件夹名），
   解决跨语言完全无法模糊匹配的问题（如 山東中地 -> 中地）；
2. 查不到再用 rapidfuzz 对规范化后的名字做模糊匹配兜底。

匹配到后列出文件夹下所有单据文件（PDF/Excel/图片），写入 state。
"""
import json
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

from app.config import get_settings
from app.state import AgentState

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
        elif folders:
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

    print(f"[Node2] 工厂「{factory}」-> 文件夹 {folder_path or '未匹配'} "
          f"(得分 {match_score})，单据 {len(source_documents)} 个，"
          f"期望 SKU {len(expected_skus)} 个")

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
