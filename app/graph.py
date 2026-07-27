"""StateGraph 编排 + SqliteSaver checkpointer 编译。

状态流转（《第一阶段.md》第 5 节）：
    START -> Node1 -> Node2 -> Node3 -> Node4 -> Node5(🔴interrupt)
          -> Node6 --(队列非空)--> Node2 ...
                   --(队列已空)--> Node7 -> END
"""
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.nodes.compute_align import compute_align
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
NODE5 = "node5_human_review"
NODE6 = "node6_writer"
NODE7 = "node7_export"


def _route_after_writer(state: AgentState) -> str:
    """Node6 后的条件边：队列非空则循环回 Node2，否则进 Node7 终态导出。"""
    if state.get("pending_factories"):
        return NODE2
    return NODE7


def build_graph(checkpointer=None):
    """构建并编译状态机。checkpointer 为 None 时不挂载持久化（仅调试用）。"""
    builder = StateGraph(AgentState)

    builder.add_node(NODE1, parse_downstream)
    builder.add_node(NODE2, folder_router)
    builder.add_node(NODE3, extraction_node)
    builder.add_node(NODE4, compute_align)
    builder.add_node(NODE5, human_review)
    builder.add_node(NODE6, writer)
    builder.add_node(NODE7, export_node)

    builder.add_edge(START, NODE1)
    builder.add_edge(NODE1, NODE2)
    builder.add_edge(NODE2, NODE3)
    builder.add_edge(NODE3, NODE4)
    builder.add_edge(NODE4, NODE5)
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
