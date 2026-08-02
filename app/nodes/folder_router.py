"""Node 2: 文件夹路由与读取（Folder Router Node）。

从 pending_factories 队列弹出一个工厂，定位本地上游文件夹。
匹配逻辑（批次覆盖 -> alias -> 规范化精确 -> fuzzy）已抽至
app/factory_match.py（匹配/推荐/alias 读写唯一事实来源），
本节点只负责队列调度、目录枚举与单据收集。

匹配到后列出文件夹下所有单据文件（PDF/Excel/图片），写入 state。
"""
import logging
from pathlib import Path

from app.config import get_settings
from app.factory_match import load_alias_map, match_factory_folder
from app.logging_config import bind_context
from app.state import AgentState

logger = logging.getLogger(__name__)

# 支持的上游单据扩展名（.doc/.docx 走 doc_channel：soffice/textutil 转换）
SUPPORTED_EXTS = {".pdf", ".xlsx", ".xls", ".jpg", ".jpeg", ".png", ".csv",
                  ".doc", ".docx"}


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
    match_method = "none"

    if upstream_root.is_dir():
        folders = [d.name for d in upstream_root.iterdir() if d.is_dir()]

        # 批次级「仅本次生效」对照（用户在预扫确认时给出，不落盘）；
        # state 字段由另一 workstream 补进 AgentState，此处防御式读取。
        overrides = state.get("factory_alias_overrides") or None

        folder_name, match_score, match_method = match_factory_folder(
            factory,
            folders,
            load_alias_map(),
            cutoff=settings.fuzzy_match_score_cutoff,
            overrides=overrides,
        )
        if folder_name:
            folder_path = str(upstream_root / folder_name)

    # 收集该文件夹下的单据文件
    source_documents: list[str] = []
    if folder_path:
        source_documents = sorted(
            str(p) for p in Path(folder_path).rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
        )

    logger.info(
        "[Node2] 工厂「%s」-> 文件夹 %s (方式 %s，得分 %s)，单据 %d 个，"
        "期望 SKU %d 个",
        factory, folder_path or "未匹配", match_method, match_score,
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
