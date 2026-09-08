# -*- coding: utf-8 -*-
"""分票图 5 个节点

load_filled → propose_split → human_review (interrupt) → persist_split → generate_docs
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langgraph.types import interrupt

from app.db.models import Container, Declaration, ProductMapping
from app.db.session import get_session
from app.declare.mapping import build_mapping_index
from app.factory_match import load_excel_normalize_map
from app.split.engine import propose
from app.split.loader import load_filled_excel
from app.split.normalize import (
    load_sku_inspection_map,
    normalize_maker,
    resolve_inspection,
)
from app.split.schemas import RawItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 辅助：按柜号聚合成 Container 行（与 engine._collect_container_info 同逻辑但无校验）
# ---------------------------------------------------------------------------

def _build_container_rows(items: list[RawItem]) -> list[dict]:
    """聚合 RawItem → 每柜一行，含工厂、商检工厂、行数、港口、箱型。

    sj_factories 口径：柜内有任一行 inspection=True 的工厂
    （与 sj_map 同为 RawItem.inspection 的派生量）。"""
    containers: dict[str, dict] = {}
    for item in items:
        k = item.kanri_no
        if k not in containers:
            containers[k] = dict(
                kanri_no=k,
                port=item.port,
                container_type=item.container_type,
                makers=set(),
                sj_factories=set(),
                row_count=0,
            )
        c = containers[k]
        c["makers"].add(item.maker)
        if item.inspection:
            c["sj_factories"].add(item.maker)
        c["row_count"] += 1
    return list(containers.values())


# ===================================================================
# Node 1
# ===================================================================

def load_filled(state: dict) -> dict:
    """读 filled Excel + 归一化 + 商检判定 + Container 落库。

    输入 state['source_file_path']（批次 final_output_path）。
    输出 state['raw_items']（list[dict]）、state['sj_map']。
    """
    source = state["source_file_path"]

    # 1) 读取 filled Excel
    raw = load_filled_excel(source)
    if not raw:
        logger.warning("load_filled: 文件 %s 未读取到任何数据行", source)
        return {"status": "loading", "raw_items": [], "sj_map": {}, "errors": ["empty_file"]}

    # 2) 工厂名归一化（DB factory_aliases 优先，config 兜底）
    normalize_map = load_excel_normalize_map()
    for r in raw:
        r.maker = normalize_maker(r.maker, normalize_map)

    # 3) SKU 级商检标注：factory_skus 权威源 → 品名级映射回退 → 默认不商检
    #    （与 declare.service.generate_declarations 共用同一解析逻辑）
    with get_session() as sess:
        from sqlalchemy.orm import selectinload

        sku_map = load_sku_inspection_map(sess)
        # selectinload 预取 sku_links，避免 build_mapping_index 逐行懒加载
        mapping_index = build_mapping_index(
            sess.query(ProductMapping)
            .options(selectinload(ProductMapping.sku_links))
            .all()
        )
    for r in raw:
        r.inspection = resolve_inspection(
            r.maker, r.sku, r.name_cn, sku_map, mapping_index
        )

    # 4) sj_map 派生量：本批次该工厂任一行商检 → 该厂即本次商检工厂
    #    （state 结构不变，下游 validate / service 仍按 {工厂名: bool} 消费）
    sj_map: dict[str, bool] = {}
    for r in raw:
        if r.maker:
            sj_map[r.maker] = sj_map.get(r.maker, False) or r.inspection

    # 5) Container 落库（删除该 split_thread_id 的旧记录后重新插入）
    container_rows = _build_container_rows(raw)
    split_tid = state["split_thread_id"]
    with get_session() as sess:
        sess.query(Container).filter(
            Container.batch_thread_id == split_tid
        ).delete()
        for c in container_rows:
            sess.add(Container(
                batch_thread_id=split_tid,
                kanri_no=c["kanri_no"],
                port=c["port"],
                container_type=c["container_type"],
                factories=sorted(c["makers"]),
                sj_factories=sorted(c["sj_factories"]),
                row_count=c["row_count"],
            ))
        sess.commit()

    logger.info(
        "load_filled: 读取 %d 行, %d 柜, %d 商检工厂, 来源 %s",
        len(raw), len(container_rows),
        sum(1 for v in sj_map.values() if v), source,
    )

    return {
        "raw_items": [r.model_dump() for r in raw],
        "sj_map": sj_map,
        "status": "loading",
    }


# ===================================================================
# Node 2
# ===================================================================

def propose_split(state: dict) -> dict:
    """调用 engine.propose() 产出推荐方案。

    输入 raw_items + sj_map。
    输出 state['proposal']（SplitProposal 的 dict）。
    """
    raw_items = [RawItem(**d) for d in state["raw_items"]]
    sj_map = state["sj_map"]

    proposal = propose(raw_items, sj_map)
    proposal.split_thread_id = state["split_thread_id"]
    proposal.source_file = state.get("source_file_path", "")

    total_tickets = sum(
        len(pg.groups) for pg in proposal.ports
    )
    logger.info("propose_split: 产出 %d 票", total_tickets)

    return {
        "proposal": proposal.model_dump(),
        "status": "pending_review",
    }


# ===================================================================
# Node 3
# ===================================================================

def human_review(state: dict) -> dict:
    """interrupt() 挂起，等待人工审核分票方案。

    复用 app/nodes/human_review.py 的 interrupt 模式：
    - interrupt(state['proposal']) —— proposal 就是审核 payload
    - 唤醒时从 Command(resume=...) 拿到人工修改后的 proposal dict

    人工可修改:
    - proposal.status: 'pending_review' → 'confirmed' 或 'reset'
    - proposal.force_confirmed: 是否强制通过
    - 各票级数据（ticket_no / items 归属调整等）
    """
    logger.info("🔴 挂起等待人工审核分票方案，split_thread_id=%s",
                state.get("split_thread_id"))

    # 🔴 在此挂起，proposal 作为审核负载抛给前端
    human_modified = interrupt(state.get("proposal", {}))

    if not isinstance(human_modified, dict):
        logger.warning("human_review: resume 数据非 dict，保留原 proposal")
        human_modified = state.get("proposal", {})

    new_status = human_modified.get("status", "pending_review")
    force_confirmed = human_modified.get("force_confirmed", False)

    logger.info("✅ 人工审核完成：status=%s, force_confirmed=%s",
                new_status, force_confirmed)

    return {
        "proposal": human_modified,
        "status": new_status,
        "force_confirmed": force_confirmed,
    }


# ===================================================================
# Node 4
# ===================================================================

def persist_split(state: dict) -> dict:
    """确认落库。

    从 state['proposal'] 读取确认后的方案：
    - 若 status == 'reset'：旧版本 Declaration 置 reset，version + 1
    - 若 status == 'confirmed'：先删该 split_thread_id 旧记录再 insert 新记录，
      version 递增，confirmed_at = func.now()
    """
    proposal_dict = state.get("proposal", {})
    split_tid = state["split_thread_id"]

    if not proposal_dict:
        logger.warning("persist_split: proposal 为空，跳过落库")
        return {"status": "completed", "errors": ["empty_proposal"]}

    new_version = state.get("version", 0) + 1

    with get_session() as sess:
        # 删除该 split_thread_id 下所有旧 Declaration
        sess.query(Declaration).filter(
            Declaration.split_thread_id == split_tid
        ).delete()

        proposal_status = proposal_dict.get("status", "pending_review")
        if proposal_status == "reset":
            sess.commit()
            logger.info("persist_split: 方案被 reset，旧记录已删除，version→%d",
                        new_version)
            return {"status": "reset", "version": new_version}

        # status == 'confirmed'：逐票落库
        # PortGroup.groups 是裸 list，内部 dict 非 Pydantic 对象，直接用 dict 键取值
        total_decls = 0
        for port_group_dict in proposal_dict.get("ports", []):
            for ticket_dict in port_group_dict.get("groups", []):
                items_data = ticket_dict.get("items", [])
                warnings_data = ticket_dict.get("warnings") or None

                decl = Declaration(
                    split_thread_id=split_tid,
                    ticket_no=ticket_dict.get("ticket_no", ""),
                    port=ticket_dict.get("port", ""),
                    container_type=ticket_dict.get("container_type", ""),
                    items=items_data,
                    sj_factories=ticket_dict.get("sj_factories", []),
                    status="confirmed",
                    version=new_version,
                    force_confirmed=state.get("force_confirmed", False),
                    warnings=warnings_data,
                    confirmed_at=datetime.now(timezone.utc),
                )
                sess.add(decl)
                total_decls += 1
        sess.commit()

    logger.info("persist_split: 落库 %d 条 Declaration, version=%d",
                total_decls, new_version)

    result = {
        "status": "confirmed",
        "version": new_version,
    }
    # confirm 时人工输入的发票号码段（可选）：resume 的 proposal dict 带
    # invoice_number 字段时存入 state，供 generate_docs 节点使用
    invoice_number = proposal_dict.get("invoice_number")
    if invoice_number:
        result["invoice_number"] = str(invoice_number).strip()
    return result


# ===================================================================
# Node 5
# ===================================================================

def generate_docs(state: dict) -> dict:
    """生成全部报关单（declare.service.generate_declarations）。

    - state['invoice_number'] 为空：跳过生成（confirm 时未带号码段），
      declare_result 注明 skipped，后续走 POST /{id}/generate 手动生成；
    - 生成成功：declare_result 写入文件列表/warnings，status='completed'；
    - 生成失败：不炸图——异常写入 errors，status='declare_failed'。
    """
    split_tid = state["split_thread_id"]
    invoice_number = (state.get("invoice_number") or "").strip()

    if not invoice_number:
        logger.info(
            "generate_docs: split_thread_id=%s 无 invoice_number，跳过生成",
            split_tid,
        )
        return {
            "status": "completed",
            "declare_result": {
                "skipped": "no invoice_number",
                "generated": [],
                "count": 0,
                "warnings": [],
            },
        }

    try:
        from app.declare.service import generate_declarations

        result = generate_declarations(split_tid, invoice_number)
    except Exception as e:  # noqa: BLE001 生成失败不炸图，状态标记供人工排查
        logger.exception(
            "generate_docs: split_thread_id=%s 报关单生成失败", split_tid
        )
        return {
            "status": "declare_failed",
            "errors": [f"declare_failed: {e}"],
            "declare_result": {
                "error": str(e), "generated": [], "count": 0, "warnings": [],
            },
        }

    logger.info(
        "generate_docs: split_thread_id=%s 生成 %d 票", split_tid, result["count"]
    )
    return {"status": "completed", "declare_result": result}