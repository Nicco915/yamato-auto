"""UI 路由：工作台 / 批次详情 / Agent 对话页 + UI 专用 API。

页面路由（读 static/*.html，文件由前端线并行开发，缺失返回 503）：
- GET  /、/dashboard        工作台页
- GET  /chat                Agent 对话页
- GET  /batch/{thread_id}   批次详情页

API（路由极薄，逻辑全在 app.api.service；全部 asyncio.to_thread 防阻塞）：
- GET  /api/v1/batches              批次列表
- POST /api/v1/batches              发起批次（409 重名 / 422 路径无效）
- GET  /api/v1/batches/{thread_id}  批次详情（404 不存在）
- GET  /api/v1/usage                全局 LLM 用量（scope=process_lifetime）
- GET  /api/v1/config/defaults      路径默认值 + 单重预警阈值
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api import service
from app.config import get_settings

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _read_page(filename: str) -> HTMLResponse:
    """读 static 下的页面文件（显式 utf-8，Windows 兼容）；缺失返回 503。"""
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(
            status_code=503, detail=f"页面文件尚未部署: ui/static/{filename}"
        )
    return HTMLResponse(path.read_text(encoding="utf-8"))


# ---------- 页面路由 ----------


@router.get("/", response_class=HTMLResponse)
async def index_page() -> HTMLResponse:
    """工作台页（批次列表 + 发起批次）。"""
    return _read_page("dashboard.html")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """工作台页（/ 的别名）。"""
    return _read_page("dashboard.html")


@router.get("/chat", response_class=HTMLResponse)
async def chat_page() -> HTMLResponse:
    """Agent 对话配置页。用法：/chat?thread_id=xxx（可选）"""
    return _read_page("chat.html")


@router.get("/batch/{thread_id}", response_class=HTMLResponse)
async def batch_page(thread_id: str) -> HTMLResponse:  # noqa: ARG001
    """批次详情页（前端从路径自取 thread_id）。"""
    return _read_page("batch.html")


# ---------- UI 专用 API ----------


class CreateBatchRequest(BaseModel):
    """发起批次请求：路径缺省时由 service 取 settings 默认值。"""

    thread_id: str
    downstream_file_path: Optional[str] = None
    upstream_root: Optional[str] = None


@router.get("/api/v1/batches")
async def list_batches():
    """批次列表：checkpoint 只读枚举 + 状态/进度推导。"""
    return await asyncio.to_thread(service.list_batches)


@router.post("/api/v1/batches")
async def create_batch(request: CreateBatchRequest):
    """发起批次：重名 409，路径无效 422，成功后跑到审核点挂起。"""
    try:
        return await asyncio.to_thread(
            service.create_batch,
            request.thread_id,
            request.downstream_file_path,
            request.upstream_root,
        )
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/v1/batches/{thread_id}")
async def batch_detail(thread_id: str):
    """批次详情：状态/进度 + factories[] + audit[] + usage。"""
    try:
        return await asyncio.to_thread(service.get_batch_detail, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/api/v1/usage")
async def usage():
    """全局 LLM token 用量（进程内累计，重启清零，无 thread 标签）。"""
    return service._usage_with_scope()


@router.get("/api/v1/config/defaults")
async def config_defaults():
    """工作台预填默认值：路径 + 单重差异预警阈值。"""
    s = get_settings()
    return {
        "upstream_root": s.upstream_root,
        "downstream_file_path": s.downstream_file_path,
        "weight_diff_warn_ratio": s.weight_diff_warn_ratio,
    }
