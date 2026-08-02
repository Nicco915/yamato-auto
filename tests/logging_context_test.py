# -*- coding: utf-8 -*-
"""L2 日志关联（contextvars + ContextFilter）回归测试。

固化三条依赖 langgraph 内部实现的行为（1.2.9 实证 BackgroundExecutor.submit
copy_context），防止升级静默回归：
  a) service 入口绑定的 thread_id 传播进节点日志 / 节点内可读；
  b) 节点内绑定的 factory 不污染调用处 context（节点独立 context）；
  c) logging_context 嵌套时，内层退出后外层绑定恢复（token reset，不抹 None）。

外加 L2 审查修复点 1 的端到端断言：多工厂 resume 链上，第二程
Node3 提取 / Node4 核算 / Node6 写回的日志携带【当前】工厂名，
而不是 service 层残留的上一工厂名。

隔离原则（与 validation/ui_api_test.py 相同，绝不碰 app/data/ 生产 db）：
- checkpoint / master db / output 全部指向临时目录（env 前置，import app 之前）；
- 下游装箱单为临时目录里生成的最小 xlsx（两个工厂各 1 个 SKU）；
- EXTRACTION_MOCK=1：提取走 mock，不调 LLM。

用法（在 app/ 目录下）：
  python3 tests/logging_context_test.py
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"                      # 提取走 mock，不调 LLM

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.logging_config import (  # noqa: E402
    ContextFilter,
    bind_context,
    bind_factory_from_state,
    clear_context,
    log_factory,
    log_thread_id,
    logging_context,
)

from _test_isolation import isolate_to_tmp  # noqa: E402

# 防御：import 链里 llm_client 的 load_dotenv(override=True) 会把 env 盖回去，
# 这里重设并清 lru_cache + 真实库断言守卫——graph/engine 都是惰性单例，
# 首次调用才建连接，此刻清缓存重建 Settings 即指向临时目录
TMP = isolate_to_tmp("yamato_logctx_test_")

# ---- 测试夹具：两个工厂的最小下游装箱单 + 上游空文件夹 ----
F1, F2 = "工厂甲", "工厂乙"
THREAD = "LOGCTX-E2E"


def _make_fixtures() -> tuple[str, str]:
    """生成最小下游 xlsx（两个工厂各 1 SKU）与上游工厂文件夹，返回路径。"""
    xlsx = TMP / "downstream.xlsx"
    wb = Workbook()
    ws = wb.active
    # Node6 首次写入时会在 SHOHIN_MEI_E 后插入 中文品名/净重/毛重 三列
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    ws.append([F1, "4900000000001", "ITEM-A", 10])
    ws.append([F2, "4900000000002", "ITEM-B", 20])
    wb.save(xlsx)

    upstream = TMP / "upstream"
    (upstream / F1).mkdir(parents=True)
    (upstream / F2).mkdir(parents=True)
    return str(xlsx), str(upstream)


# ---- 日志捕获：挂 root handler + ContextFilter，逐条留 record ----
records: list[logging.LogRecord] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        records.append(record)


def _install_capture() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    cap = _Capture()
    cap.addFilter(ContextFilter())  # 与生产一致：filter 挂 handler，注入关联字段
    root.addHandler(cap)


def _node_records(name: str) -> list[logging.LogRecord]:
    return [r for r in records if r.name == name]


# ---------------------------------------------------------------------------
# 单元层：logging_context 嵌套恢复 / clear_context / bind_factory_from_state
# ---------------------------------------------------------------------------

def test_logging_context_nesting():
    """c) 嵌套时内层退出恢复外层绑定（修复点 2 核心）。"""
    clear_context()
    with logging_context(thread_id="T-OUTER"):
        assert log_thread_id.get() == "T-OUTER"
        assert log_factory.get() is None
        with logging_context(thread_id="T-INNER", factory="厂X"):
            assert log_thread_id.get() == "T-INNER"
            assert log_factory.get() == "厂X"
        # 内层退出：外层 thread_id 原样恢复，factory 恢复为进入前的 None
        assert log_thread_id.get() == "T-OUTER"
        assert log_factory.get() is None
    # 全部退出：恢复为最初的无绑定状态（不是残留 T-OUTER）
    assert log_thread_id.get() is None
    assert log_factory.get() is None

    # 嵌套调用的真实形态：外层是裸 bind_context（如 dispatcher 循环绑定），
    # 内层 logging_context（如 service 入口）退出后外层绑定不被抹掉
    bind_context(thread_id="T-DISPATCHER")
    with logging_context(thread_id="T-SERVICE", factory="厂Y"):
        assert log_thread_id.get() == "T-SERVICE"
    assert log_thread_id.get() == "T-DISPATCHER"
    assert log_factory.get() is None
    clear_context()
    assert log_thread_id.get() is None
    print("[断言通过] logging_context 嵌套恢复 + clear_context 语义")


def test_bind_factory_from_state():
    """helper 容错：取得到就绑，取不到不绑不抛。"""
    clear_context()
    bind_factory_from_state({"current_factory_data": {"factory_name": "厂Z"}})
    assert log_factory.get() == "厂Z"
    clear_context()
    for bad in ({}, {"current_factory_data": {}}, {"current_factory_data": None}, None):
        bind_factory_from_state(bad)  # 不抛
        assert log_factory.get() is None
    clear_context()
    print("[断言通过] bind_factory_from_state 绑定 + 缺省容错")


# ---------------------------------------------------------------------------
# 图行为层：最小探针图固化 langgraph context 拷贝语义
# ---------------------------------------------------------------------------

def test_minimal_graph_context():
    """a)+b) 最小图：入口绑定传播进节点、节点内绑定不泄漏回调用处。"""
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    seen: dict = {}

    class _ProbeState(TypedDict, total=False):
        pass

    def _probe(state):
        seen["thread_id_in_node"] = log_thread_id.get()   # a) 节点内可读
        seen["factory_in_node_before"] = log_factory.get()
        bind_context(factory="探针厂")
        seen["factory_in_node_after"] = log_factory.get()
        return {}

    builder = StateGraph(_ProbeState)
    builder.add_node("probe", _probe)
    builder.add_edge(START, "probe")
    builder.add_edge("probe", END)
    graph = builder.compile()

    with logging_context(thread_id="T-PROBE"):
        graph.invoke({})
        # b) 节点内 bind_context 不污染调用处 context
        assert log_factory.get() is None
        assert log_thread_id.get() == "T-PROBE"

    assert seen["thread_id_in_node"] == "T-PROBE"
    assert seen["factory_in_node_before"] is None
    assert seen["factory_in_node_after"] == "探针厂"
    print("[断言通过] 最小图：入口绑定传播进节点，节点内绑定不泄漏")


# ---------------------------------------------------------------------------
# 端到端层：真实 service + 双工厂 resume 链（修复点 1 核心断言）
# ---------------------------------------------------------------------------

def _assert_records_thread(recs: list[logging.LogRecord], what: str):
    assert recs, f"{what} 未产生任何日志"
    for r in recs:
        assert getattr(r, "thread_id", None) == THREAD, \
            f"{what} 日志缺 thread_id: {r.getMessage()}"


def test_e2e_resume_chain_factory():
    """多工厂 resume 链：第二程 Node3/4/6 日志带当前工厂名（非残留 F1）。"""
    xlsx, upstream = _make_fixtures()
    clear_context()

    # ---- 第一程：run 到 F1 挂起 ----
    r1 = service.run_until_interrupt(
        THREAD, downstream_file_path=xlsx, upstream_root=upstream
    )
    assert r1["status"] == "pending_human_review", f"未挂起: {r1}"
    assert r1["review_data"]["factory_name"] == F1

    node_recs = [r for r in records if r.name.startswith("app.nodes")]
    _assert_records_thread(node_recs, "第一程节点")            # a) thread_id 传播
    for r in _node_records("app.nodes.extraction_node"):
        assert getattr(r, "factory", None) == F1, r.getMessage()
    # b) service 返回后调用处 context 干净（节点绑定不泄漏 + 入口恢复前值）
    assert log_thread_id.get() is None and log_factory.get() is None

    # ---- 第二程：嵌套在"外层 dispatcher 绑定"里 resume F1 -> F2 挂起 ----
    # （同时验证修复点 2：内层 service 的 logging_context 退出不抹外层绑定）
    records.clear()
    bind_context(thread_id="T-DISPATCHER")
    r2 = service.resume_order(THREAD, {"approved": True, "items": []})
    assert log_thread_id.get() == "T-DISPATCHER", "外层绑定被内层 service 抹掉"
    assert log_factory.get() is None
    clear_context()

    assert r2["status"] == "pending_human_review", f"未二次挂起: {r2}"
    assert r2["review_data"]["factory_name"] == F2
    # 修复点 1：第二程 Node3/Node4 处理 F2，日志必须带 F2（而非残留的 F1）
    n3 = _node_records("app.nodes.extraction_node")
    n4 = _node_records("app.nodes.compute_align")
    assert n3 and all(getattr(r, "factory", None) == F2 for r in n3), \
        [getattr(r, "factory", None) for r in n3]
    assert n4 and all(getattr(r, "factory", None) == F2 for r in n4), \
        [getattr(r, "factory", None) for r in n4]
    # 本程 Node6 写的是刚审核完的 F1
    n6 = _node_records("app.nodes.writer")
    assert n6 and all(getattr(r, "factory", None) == F1 for r in n6), \
        [getattr(r, "factory", None) for r in n6]
    _assert_records_thread(n3 + n4 + n6, "第二程 Node3/4/6")
    print("[断言通过] resume 第二程：Node3/4 日志带 F2，Node6 带 F1，外层绑定未泄漏")

    # ---- 第三程：resume F2 -> 跑完 END ----
    records.clear()
    r3 = service.resume_order(THREAD, {"approved": True, "items": []})
    assert r3["status"] == "success", f"未成功收尾: {r3}"
    n3 = _node_records("app.nodes.extraction_node")
    n6 = _node_records("app.nodes.writer")
    # 本程 Node3/4 不再执行（无新工厂），Node6 写 F2
    assert not n3
    assert n6 and all(getattr(r, "factory", None) == F2 for r in n6), \
        [getattr(r, "factory", None) for r in n6]
    _assert_records_thread(n6, "第三程 Node6")
    assert log_thread_id.get() is None and log_factory.get() is None
    print("[断言通过] resume 第三程：Node6 日志带 F2，流程走到 END")


def main():
    _install_capture()
    test_logging_context_nesting()
    test_bind_factory_from_state()
    test_minimal_graph_context()
    test_e2e_resume_chain_factory()
    print("\nlogging_context_test: PASS")


if __name__ == "__main__":
    main()
