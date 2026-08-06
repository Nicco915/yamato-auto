# -*- coding: utf-8 -*-
"""分票 LangGraph 图

独立于现有提取图（app/graph.py），两条流水线上下游关系：
  提取图 Node7 导出 filled Excel → 分票图 load_filled 消费

SplitState 独立于 AgentState，不混用。
复用同一个 checkpoints.db 文件（SqliteSaver），thread_id 用 ``split-{批次thread_id}`` 前缀区分。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.split.nodes import (
    generate_docs,
    human_review,
    load_filled,
    persist_split,
    propose_split,
)


class SplitState(TypedDict, total=False):
    """分票图共享状态（total=False：节点只返回需要更新的键）。"""

    # ---- 输入参数 ----
    split_thread_id: str          # 分票图 thread_id，建议 "split-{批次thread_id}"
    source_file_path: str         # 批次 filled Excel 的 final_output_path

    # ---- Node 1 产物 ----
    raw_items: list[dict]         # RawItem.model_dump() 列表
    sj_map: dict[str, bool]       # {factory_name: is_sj}

    # ---- Node 2 产物 ----
    proposal: dict                # SplitProposal.model_dump()

    # ---- 流程控制 ----
    status: str                   # loading / pending_review / confirmed / reset / completed / declare_failed
    version: int                  # Declaration 版本号，每次确认递增
    force_confirmed: bool         # 人工审核时是否强制通过
    errors: list[str]             # 错误信息收集

    # ---- Node 5 报关生成 ----
    invoice_number: str           # 发票号码段（confirm 时人工输入，可空→跳过生成）
    declare_result: dict          # generate_declarations 返回（generated/count/warnings/out_dir）


# ---- 节点名常量（供外部引用）----
NODE1_SPLIT = "load_filled"
NODE2_SPLIT = "propose_split"
NODE3_SPLIT = "human_review"
NODE4_SPLIT = "persist_split"
NODE5_SPLIT = "generate_docs"


def build_split_graph(checkpointer=None):
    """构建并编译分票状态机。checkpointer 为 None 时不挂载持久化（仅调试用）。"""
    builder = StateGraph(SplitState)

    builder.add_node(NODE1_SPLIT, load_filled)
    builder.add_node(NODE2_SPLIT, propose_split)
    builder.add_node(NODE3_SPLIT, human_review)
    builder.add_node(NODE4_SPLIT, persist_split)
    builder.add_node(NODE5_SPLIT, generate_docs)

    builder.add_edge(START, NODE1_SPLIT)
    builder.add_edge(NODE1_SPLIT, NODE2_SPLIT)
    builder.add_edge(NODE2_SPLIT, NODE3_SPLIT)  # 到此 interrupt
    builder.add_edge(NODE3_SPLIT, NODE4_SPLIT)
    builder.add_edge(NODE4_SPLIT, NODE5_SPLIT)
    builder.add_edge(NODE5_SPLIT, END)

    return builder.compile(checkpointer=checkpointer)


# ---- 全局惰性单例 ----
_split_graph = None
_split_ckpt_conn = None


def get_split_graph():
    """返回挂载了 SqliteSaver 的编译后分票图（单例）。

    复用与提取图相同的 checkpoints.db 文件，独立的 sqlite3 connection。
    """
    global _split_graph, _split_ckpt_conn
    if _split_graph is None:
        settings = get_settings()
        Path(settings.checkpoint_db_abs).parent.mkdir(parents=True, exist_ok=True)
        _split_ckpt_conn = sqlite3.connect(
            str(settings.checkpoint_db_abs), check_same_thread=False
        )
        checkpointer = SqliteSaver(_split_ckpt_conn)
        _split_graph = build_split_graph(checkpointer=checkpointer)
    return _split_graph