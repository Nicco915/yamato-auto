# -*- coding: utf-8 -*-
"""分票 API 路由

端点：
- POST /api/v1/split/start            启动分票，invoke split 图到 interrupt 挂起
- GET  /api/v1/split/{id}/proposal    读取当前 proposal（挂起中或已确认）
- POST /api/v1/split/{id}/confirm     确认方案，Command(resume=...) 唤醒图落库
- POST /api/v1/split/{id}/reset       推翻已确认方案，清理后从 START 重跑推荐
"""

from __future__ import annotations

import logging
import shutil
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from langgraph.graph import START
from langgraph.types import Command
from pydantic import BaseModel

from app.config import get_settings
from app.declare.service import declarations_dir, generate_declarations
from app.split.graph import get_split_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/split", tags=["split"])


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------

class StartSplitRequest(BaseModel):
    thread_id: str
    source_file_path: Optional[str] = None


class StartSplitResponse(BaseModel):
    split_thread_id: str
    status: str  # "pending_review" | "completed"


class ConfirmSplitRequest(BaseModel):
    proposal: dict  # 人工改后的完整 proposal
    force: bool = False


class GenerateRequest(BaseModel):
    invoice_number: str  # 人工输入的号码段，如 '656'


class ProposalResponse(BaseModel):
    split_thread_id: str
    source_file: str
    status: str  # "pending_review" | "confirmed" | "reset" | "completed"
    proposal: dict  # SplitProposal.model_dump()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("/start", response_model=StartSplitResponse)
def start_split(req: StartSplitRequest):
    """启动分票：以上游批次 thread_id 构造 split_thread_id，invoke 分票图直到 interrupt。

    1. 构造 split_thread_id = f"split-{req.thread_id}"
    2. 校验 source_file_path（必填）
    3. 若该 split_thread_id 已有 checkpoint，返回现有状态
    4. 否则 invoke get_split_graph()，在 human_review 节点 interrupt 挂起
    """
    if not req.source_file_path:
        raise HTTPException(status_code=400, detail="source_file_path 不能为空")

    split_thread_id = f"split-{req.thread_id}"
    graph = get_split_graph()
    cfg = _config(split_thread_id)

    # 幂等：已有 checkpoint 直接返回现有状态
    snap = graph.get_state(cfg)
    if snap.values:
        status = snap.values.get("status", "unknown")
        if any(t.interrupts for t in snap.tasks):
            return StartSplitResponse(
                split_thread_id=split_thread_id, status="pending_review"
            )
        return StartSplitResponse(split_thread_id=split_thread_id, status=status)

    initial_state = {
        "source_file_path": req.source_file_path,
        "split_thread_id": split_thread_id,
    }
    initial_state["batch_id"] = req.thread_id  # 父批次 ID

    for event in graph.stream(initial_state, cfg, stream_mode="updates"):
        if "__interrupt__" in event:
            logger.info(
                "start_split: 分票图已挂起，split_thread_id=%s", split_thread_id
            )
            return StartSplitResponse(
                split_thread_id=split_thread_id, status="pending_review"
            )

    # 理论上 human_review 必定 interrupt，走到这里是异常
    return StartSplitResponse(split_thread_id=split_thread_id, status="completed")


@router.get("/{split_thread_id}/proposal", response_model=ProposalResponse)
def get_proposal(split_thread_id: str):
    """读取当前的 proposal（interrupt 挂起时或已确认后）。

    优先从 tasks[].interrupts 读取（挂起中的 payload），
    否则从 state.values 读取（已确认/已完成的方案）。
    """
    graph = get_split_graph()
    cfg = _config(split_thread_id)
    snap = graph.get_state(cfg)

    if not snap.values:
        raise HTTPException(
            status_code=404, detail=f"分票任务不存在: {split_thread_id}"
        )

    # 优先取 interrupt payload（挂起中的实时数据）
    for task in snap.tasks:
        if task.interrupts:
            value = task.interrupts[0].value
            if isinstance(value, dict):
                return ProposalResponse(
                    split_thread_id=split_thread_id,
                    source_file=snap.values.get("source_file_path", ""),
                    status="pending_review",
                    proposal=value,
                )

    # 已确认/已完成：从 state.values 读取
    proposal = snap.values.get("proposal", {})
    status = snap.values.get("status", "unknown")
    return ProposalResponse(
        split_thread_id=split_thread_id,
        source_file=snap.values.get("source_file_path", ""),
        status=status,
        proposal=proposal,
    )


@router.post("/{split_thread_id}/confirm")
def confirm_split(split_thread_id: str, req: ConfirmSplitRequest):
    """确认分票方案（或强制确认带 warning 的方案）。

    用 Command(resume=req.proposal) 唤醒 human_review 节点，
    而后 persist_split 落库 Declaration 记录、generate_docs 走完。
    req.force 时在 proposal 中标记 force_confirmed=true。
    """
    graph = get_split_graph()
    cfg = _config(split_thread_id)

    snap = graph.get_state(cfg)
    if not snap.values:
        raise HTTPException(
            status_code=404, detail=f"分票任务不存在: {split_thread_id}"
        )
    if not any(t.interrupts for t in snap.tasks):
        raise HTTPException(status_code=400, detail="该任务未处于等待审核状态")

    proposal = req.proposal
    proposal["status"] = "confirmed"
    if req.force:
        proposal["force_confirmed"] = True

    logger.info(
        "confirm_split: 确认方案 split_thread_id=%s, force=%s",
        split_thread_id,
        req.force,
    )

    for event in graph.stream(
        Command(resume=proposal), cfg, stream_mode="updates"
    ):
        if "__interrupt__" in event:
            pass  # confirm 不应再次 interrupt

    final = graph.get_state(cfg)
    return {
        "split_thread_id": split_thread_id,
        "status": final.values.get("status", "confirmed"),
    }


@router.post("/{split_thread_id}/reset")
def reset_split(split_thread_id: str):
    """重置分票：推翻当前方案（待审或已确认均可），从 START 重跑推荐。

    步骤因当前状态而异：
    - pending_review（有 interrupt 挂起）：先 resume 走完 reset 清理流程，
      再 update_state(as_node=START) 重跑；
    - completed / confirmed（图已结束）：直接 update_state(as_node=START) 重跑，
      旧 Declaration 记录由重跑后 persist_split 自然清理。

    重跑产出新的 proposal，在 human_review 再次 interrupt，返回 pending_review。
    """
    graph = get_split_graph()
    cfg = _config(split_thread_id)

    snap = graph.get_state(cfg)
    if not snap.values:
        raise HTTPException(
            status_code=404, detail=f"分票任务不存在: {split_thread_id}"
        )

    logger.info(
        "reset_split: 重置方案 split_thread_id=%s, 当前状态=%s",
        split_thread_id,
        snap.values.get("status", "unknown"),
    )

    # 清理已生成的报关单文件（DB 记录由 persist_split 清理，文件在这里清理）
    batch_id = split_thread_id.removeprefix("split-")
    decl_dir = get_settings().batch_declarations_dir(batch_id) / split_thread_id
    if decl_dir.exists():
        shutil.rmtree(decl_dir)
        logger.info("reset_split: 已清理报关单目录 %s", decl_dir)

    has_interrupt = any(t.interrupts for t in snap.tasks)

    if has_interrupt:
        # 挂起中：先 resume reset 通过 persist_split 清理旧 Declaration
        for event in graph.stream(
            Command(resume={"status": "reset"}), cfg, stream_mode="updates"
        ):
            if "__interrupt__" in event:
                pass

    # 两种路径汇聚：作废旧现场，从 START 重跑
    # state 中 source_file_path / split_thread_id 仍保留，无需重新传入
    graph.update_state(cfg, {}, as_node=START)

    # 重跑整条管线，在 human_review 再次挂起
    for event in graph.stream(None, cfg, stream_mode="updates"):
        if "__interrupt__" in event:
            logger.info(
                "reset_split: 已重置并重新挂起，split_thread_id=%s",
                split_thread_id,
            )
            return {
                "split_thread_id": split_thread_id,
                "status": "pending_review",
                "message": "方案已重置，已重新生成推荐方案",
            }

    return {
        "split_thread_id": split_thread_id,
        "status": "completed",
    }


# ---------------------------------------------------------------------------
# 报关单生成 / 文件列表 / 下载
# ---------------------------------------------------------------------------

@router.post("/{split_thread_id}/generate")
def generate(split_thread_id: str, req: GenerateRequest):
    """手动触发生成（不经过图的 confirm 流程也能生成）。

    直接调 declare.service.generate_declarations。要求该 split_thread_id
    已有 confirmed 的 Declaration，否则 400。
    """
    invoice_number = req.invoice_number.strip()
    if not invoice_number:
        raise HTTPException(status_code=400, detail="invoice_number 不能为空")
    try:
        result = generate_declarations(split_thread_id, invoice_number)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "generate: split_thread_id=%s 报关单生成失败", split_thread_id
        )
        raise HTTPException(status_code=500, detail=f"报关单生成失败: {e}")
    return {"split_thread_id": split_thread_id, **result}


@router.get("/{split_thread_id}/files")
def list_files(split_thread_id: str):
    """列出该任务输出目录下的 xlsx 文件（文件名 + 大小 + 修改时间）。"""
    out_dir = declarations_dir(split_thread_id)
    files = []
    if out_dir.is_dir():
        for p in sorted(out_dir.glob("*.xlsx")):
            st = p.stat()
            files.append({
                "name": p.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    return {
        "split_thread_id": split_thread_id,
        "out_dir": str(out_dir),
        "count": len(files),
        "files": files,
    }


@router.get("/{split_thread_id}/download/{filename}")
def download(split_thread_id: str, filename: str):
    """下载单个报关单文件。防目录穿越：filename 不得含 '/'、'\\\\'、'..'。"""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    path = declarations_dir(split_thread_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {filename}")
    return FileResponse(path, filename=filename)