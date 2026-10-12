"""StateGraph 编排 + SqliteSaver checkpointer 编译。

状态流转（《第一阶段.md》第 5 节 + W6a 暂缓队列 + 2026-10-12 自动跳过）：
    START -> Node1 -> Node2 -> Node3 -> Node4 --(成功/二遍仍失败/最后一个)--> Node5(🔴interrupt)
          -> Node6 --(主队列或暂缓队列非空)--> Node2 ...
                   --(两队列皆空)--> Node7 -> END
          Node4 --(失败且后面还有厂)--> Node4B(暂缓) --> Node2
          Node4 --(缺资料且二遍/最后一家)--> Node4C(自动跳过)
                   --(还有厂)--> Node2 / --(两队列皆空)--> Node7
"""
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes.auto_skip_node import auto_skip_node
from app.nodes.compute_align import compute_align
from app.nodes.defer_node import defer_node
from app.nodes.export_node import export_node
from app.nodes.extraction_node import extraction_node
from app.nodes.folder_router import folder_router
from app.nodes.human_review import human_review
from app.nodes.parse_downstream import parse_downstream
from app.nodes.writer import writer
from app.state import AgentState

# ---- 节点名常量（供条件边与外部引用）----
NODE1 = "node1_parse_downstream"
NODE2 = "node2_folder_router"
NODE3 = "node3_extraction"
NODE4 = "node4_compute_align"
NODE4B = "node4b_defer"
NODE4C = "node4c_auto_skip"
NODE5 = "node5_human_review"
NODE6 = "node6_writer"
NODE7 = "node7_export"


# 可自动跳过的缺资料类失败原因（2026-10-12 需求 1；与 auto_skip_node 口径一致）
_AUTO_SKIP_REASONS = {"no_folder_matched", "no_documents"}


def _route_after_compute(state: AgentState) -> str:
    """Node4 后的条件边（W6a 暂缓队列 + 2026-10-12 缺资料自动跳过，三路分流）。

    1. 提取失败且不是二遍重试、且后面还有工厂（主队列或暂缓队列非空）
       → Node4B 暂缓，跳过 Node5/Node6（占位数据绝不写回）；
    2. 提取失败且失败原因属缺资料类（no_folder_matched/no_documents）、
       且为二遍重试或已是最后一个工厂（两队列皆空不空转）
       → Node4C 自动跳过：不挂人工审核，记 auto_skipped_factories + 审计
       留痕 factory_auto_skipped，批次跑完进「待补充」；
    3. 其余（成功 / 有单据但提取为空 no_items_extracted / extraction_error
       即使二遍仍失败）→ Node5 挂起人工审核/补录，行为不变。
    """
    cur = state.get("current_factory_data") or {}
    failed = cur.get("extraction_ok") is False
    final = bool(cur.get("is_final_attempt"))
    has_more = bool(state.get("pending_factories")) or bool(state.get("deferred_factories"))
    if failed and not final and has_more:
        return NODE4B
    if failed and cur.get("failure_reason") in _AUTO_SKIP_REASONS \
            and (final or not has_more):
        return NODE4C
    return NODE5


def _route_after_writer(state: AgentState) -> str:
    """Node6 后的条件边：主队列或暂缓队列非空则循环回 Node2，否则进 Node7 终态导出。

    暂缓队列（W6a）也算未完——失败工厂要等主队列走完后二遍重试。
    """
    if state.get("pending_factories") or state.get("deferred_factories"):
        return NODE2
    return NODE7


def build_graph(checkpointer=None):
    """构建并编译状态机。checkpointer 为 None 时不挂载持久化（仅调试用）。"""
    builder = StateGraph(AgentState)

    builder.add_node(NODE1, parse_downstream)
    builder.add_node(NODE2, folder_router)
    builder.add_node(NODE3, extraction_node)
    builder.add_node(NODE4, compute_align)
    builder.add_node(NODE4B, defer_node)
    builder.add_node(NODE4C, auto_skip_node)
    builder.add_node(NODE5, human_review)
    builder.add_node(NODE6, writer)
    builder.add_node(NODE7, export_node)

    builder.add_edge(START, NODE1)
    builder.add_edge(NODE1, NODE2)
    builder.add_edge(NODE2, NODE3)
    builder.add_edge(NODE3, NODE4)
    # W6a：失败且后面还有厂 → Node4B 暂缓回 Node2；缺资料类二遍/最后一家
    # → Node4C 自动跳过；其余 → Node5 挂起
    builder.add_conditional_edges(NODE4, _route_after_compute,
                                  {NODE4B: NODE4B, NODE4C: NODE4C, NODE5: NODE5})
    builder.add_edge(NODE4B, NODE2)
    # Node4C 出口与 Node6 同口径：队列非空回 Node2 续跑，两队列皆空进
    # Node7 导出。（计划原文为 add_edge(NODE4C, NODE2)，但 Node2 空队列守卫
    # 会造出「未知工厂」空数据并被条件边再次路由回 Node4C 形成死循环，
    # 故按 _route_after_writer 同语义改为条件边终结）
    builder.add_conditional_edges(NODE4C, _route_after_writer,
                                  {NODE2: NODE2, NODE7: NODE7})
    builder.add_edge(NODE5, NODE6)
    # 工厂维度批处理循环
    builder.add_conditional_edges(NODE6, _route_after_writer, {NODE2: NODE2, NODE7: NODE7})
    builder.add_edge(NODE7, END)

    return builder.compile(checkpointer=checkpointer)


# ---- 全局惰性单例（API 与 CLI 共用同一份 checkpoint 连接）----
_graph = None
_ckpt_conn = None


def get_graph():
    """返回挂载了 SqliteSaver 的编译后图（单例）。"""
    global _graph, _ckpt_conn
    if _graph is None:
        settings = get_settings()
        Path(settings.checkpoint_db_abs).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：配合 FastAPI 的 asyncio.to_thread 跨线程调用
        _ckpt_conn = sqlite3.connect(
            str(settings.checkpoint_db_abs), check_same_thread=False
        )
        checkpointer = SqliteSaver(_ckpt_conn)
        _graph = build_graph(checkpointer=checkpointer)
    return _graph
