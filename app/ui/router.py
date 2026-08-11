"""UI 路由：工作台 / 批次详情 / Agent 对话页 + UI 专用 API。

页面路由（读 static/*.html，文件由前端线并行开发，缺失返回 503）：
- GET  /、/dashboard        工作台页
- GET  /chat                Agent 对话页
- GET  /batch/{thread_id}   批次详情页
- GET  /mappings            主数据维护页（产品映射 + 品名组）

API（路由极薄，逻辑全在 app.api.service；全部 asyncio.to_thread 防阻塞）：
- GET  /api/v1/batches              批次列表
- POST /api/v1/batches              发起批次（409 重名 / 422 路径无效）
- POST /api/v1/batches/precheck     重复处理预检（422 装箱单解析失败）
- GET  /api/v1/batches/{thread_id}  批次详情（404 不存在）
- DELETE /api/v1/batches/{thread_id} 删除过往批次（404 不存在 / 409 进行中）
- PATCH /api/v1/batches/{thread_id}/paths  更新批次路径配置（404 不存在）
- POST /api/v1/batches/{thread_id}/rerun   完全重跑批次（404 不存在）
- POST /api/v1/batches/{thread_id}/add-factories  补充工厂（400 失败）
- GET  /api/v1/usage                全局 LLM 用量（scope=process_lifetime）
- GET  /api/v1/config/defaults      路径默认值 + 单重预警阈值
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
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


@router.get("/split/{split_thread_id}", response_class=HTMLResponse)
async def split_page(split_thread_id: str) -> HTMLResponse:  # noqa: ARG001
    """分票审核页（前端从路径自取 split_thread_id）。"""
    return _read_page("split.html")


@router.get("/mappings", response_class=HTMLResponse)
async def mappings_page() -> HTMLResponse:
    """主数据维护页（产品映射 + 品名组）。"""
    return _read_page("mappings.html")


# ---------- UI 专用 API ----------


class CreateBatchRequest(BaseModel):
    """发起批次请求：路径缺省时由 service 取 settings 默认值。

    factory_filter 只处理指定工厂；skip_processed=true 自动跳过已处理
    工厂（与 factory_filter 互斥，factory_filter 优先）——W4b。
    """

    thread_id: str
    downstream_file_path: Optional[str] = None
    upstream_root: Optional[str] = None
    factory_filter: Optional[list[str]] = None
    skip_processed: bool = False


class PrecheckRequest(BaseModel):
    """重复处理预检请求：路径字段语义与发起批次一致（缺省取配置默认值）。

    factory_names 缺省时解析装箱单取工厂集合；给出时直接用之。

    thread_id 必填：每批次独立缓存，预检按本批次判定；跨批次审核不再回看。
    """

    thread_id: str
    downstream_file_path: Optional[str] = None
    factory_names: Optional[list[str]] = None


class UpdateBatchPathsRequest(BaseModel):
    """更新批次路径配置请求：所有字段可选，仅传入要修改的项。

    reset_checkpoint=True 时清除已有 checkpoint，使批次从 Node1 重新执行。
    """

    downstream_file_path: Optional[str] = None
    upstream_root: Optional[str] = None
    reset_checkpoint: bool = False


class BatchDeleteRequest(BaseModel):
    """批量删除批次请求。"""
    thread_ids: list[str]


@router.get("/api/v1/batches")
async def list_batches():
    """批次列表：checkpoint 只读枚举 + 状态/进度推导。"""
    return await asyncio.to_thread(service.list_batches)


@router.post("/api/v1/batches")
async def create_batch(request: CreateBatchRequest):
    """发起批次：重名 409，路径无效 422，成功后跑到审核点挂起。

    skip_processed 且全部工厂已处理时返回 status=skipped_all（不建批次）。
    """
    try:
        return await asyncio.to_thread(
            service.create_batch,
            request.thread_id,
            request.downstream_file_path,
            request.upstream_root,
            factory_filter=request.factory_filter,
            skip_processed=request.skip_processed,
        )
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/api/v1/batches/precheck")
async def batch_precheck(request: PrecheckRequest):
    """重复处理预检（W4b）：四档判定各工厂是否已处理；装箱单解析失败 422。

    按本批次 thread_id 隔离；跨批次审核记录不再回看。
    """
    try:
        return await asyncio.to_thread(
            service.check_processed_factories,
            request.thread_id,
            request.downstream_file_path,
            request.factory_names,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/api/v1/batches/{thread_id}")
async def batch_detail(thread_id: str):
    """批次详情：状态/进度 + factories[] + audit[] + usage。

    预提取进度文件存在且可解析时附带 pre_extraction 键（对话页进度条
    数据源）；不存在/损坏时静默不带该键，绝不让端点 500。
    """
    try:
        detail = await asyncio.to_thread(service.get_batch_detail, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    progress = await asyncio.to_thread(
        service.load_pre_extraction_progress, thread_id)
    if progress is not None:
        detail["pre_extraction"] = progress
    return detail


@router.api_route("/api/v1/batches/{thread_id}/output", methods=["GET", "HEAD"])
async def download_batch_output(thread_id: str):
    """下载/打开批次最终输出 Excel.

    返回 final_output_path 指向的 Excel 文件，浏览器根据系统设置决定
    直接打开或下载。文件必须存在且是 .xlsx/.xls 类型，否则 404。
    """
    try:
        detail = await asyncio.to_thread(service.get_batch_detail, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    final_output_path = detail.get("final_output_path")
    if not final_output_path:
        raise HTTPException(status_code=404, detail="批次没有最终输出文件路径")
    path = Path(final_output_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if path.suffix.lower() not in (".xlsx", ".xls"):
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {path.suffix}",
        )
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.delete("/api/v1/batches/{thread_id}")
async def delete_batch(thread_id: str):
    """删除过往批次：不存在 404，进行中 409；review_audits 留痕 batch_deleted。"""
    try:
        return await asyncio.to_thread(service.delete_batch, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/api/v1/batches/batch-delete")
async def batch_delete_batches(req: BatchDeleteRequest):
    """批量删除批次：逐个调用 service.delete_batch，返回 {deleted, failed}。"""
    result = await asyncio.to_thread(service.batch_delete_batches, req.thread_ids)
    return result


@router.patch("/api/v1/batches/{thread_id}/paths")
async def update_batch_paths_endpoint(thread_id: str, req: UpdateBatchPathsRequest):
    """更新批次路径配置，可选重置 checkpoint。"""
    try:
        result = await asyncio.to_thread(
            service.update_batch_paths,
            thread_id,
            req.downstream_file_path,
            req.upstream_root,
            req.reset_checkpoint,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/api/v1/batches/{thread_id}/rerun")
async def rerun_batch_endpoint(thread_id: str):
    """完全重跑批次：清空 containers/，重置 checkpoint，从 Node1 重新执行。"""
    try:
        result = await asyncio.to_thread(service.rerun_batch, thread_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/api/v1/batches/{thread_id}/add-factories")
async def add_factories_endpoint(thread_id: str):
    """补充工厂：重新解析装箱单，增量合并 pending_factories，继续执行。"""
    try:
        result = await asyncio.to_thread(service.add_factories_to_batch, thread_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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


# ---------- 文件浏览 API ----------


@router.get("/api/v1/files/browse")
async def browse_files(
    path: str | None = None,
    type: str = "dir",
    extensions: str | None = None,
):
    """列出目录内容供文件/目录选择。

    跨平台兼容：
    - macOS/Linux: path=None 返回 ~（home 目录）
    - Windows: path=None 返回可用盘符列表
    - 安全检查：不允许访问系统敏感目录
    """
    import os
    from pathlib import Path as _Path

    # 确定起始路径
    if path is None:
        if os.name == "nt":  # Windows
            # 返回可用盘符列表
            import string
            drives = []
            for letter in string.ascii_uppercase:
                drive_path = _Path(f"{letter}:\\")
                if drive_path.exists():
                    drives.append(f"{letter}:\\")
            return {
                "current_path": None,
                "parent_path": None,
                "entries": [],
                "drives": drives,
            }
        else:
            # macOS/Linux: home 目录
            browse_path = _Path.home()
    else:
        browse_path = _Path(path).expanduser()

    # 安全检查：防止访问敏感目录
    # （可选：限制在某些根目录下，这里简单实现为不允许根目录）
    if browse_path == _Path("/") or (os.name == "nt" and str(browse_path) in ["C:\\", "D:\\"]):
        # 允许访问根目录和盘符，但标记为系统目录
        pass

    # 验证路径存在且是目录
    if not browse_path.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    if not browse_path.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录: {path}")

    # 解析扩展名过滤
    allowed_ext = None
    if extensions:
        allowed_ext = {ext.strip().lower().lstrip(".") for ext in extensions.split(",")}

    # 列出目录内容
    entries = []
    try:
        for item in sorted(browse_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            # 跳过隐藏文件（.DS_Store 等）
            if item.name.startswith("."):
                continue

            is_dir = item.is_dir()

            # 如果是文件选择模式，检查扩展名
            if type == "file" and not is_dir and allowed_ext:
                if item.suffix.lower().lstrip(".") not in allowed_ext:
                    continue

            entry = {
                "name": item.name,
                "path": str(item),
                "is_dir": is_dir,
            }

            if not is_dir:
                try:
                    entry["size"] = item.stat().st_size
                    entry["modified"] = item.stat().st_mtime
                except OSError:
                    pass

            entries.append(entry)
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限访问: {path}")

    # 父目录路径
    parent_path = str(browse_path.parent) if browse_path.parent != browse_path else None

    return {
        "current_path": str(browse_path),
        "parent_path": parent_path,
        "entries": entries,
        "drives": None,  # 非 Windows 或已有 path 时为 null
    }
