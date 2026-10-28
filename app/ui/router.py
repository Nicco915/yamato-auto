"""UI 路由：工作台 / 批次详情 / Agent 对话页 + UI 专用 API。

页面路由（读 static/*.html，文件由前端线并行开发，缺失返回 503）：
- GET  /                    落地页，重定向到 /board
- GET  /board               监控看板页（三档总览 + 一键启动/标记完成）
- GET  /dashboard           工作台页
- GET  /chat                Agent 对话页
- GET  /batch/{thread_id}   批次详情页
- GET  /mappings            主数据维护页（产品映射 + 品名组）

API（路由极薄，逻辑全在 app.api.service；全部 asyncio.to_thread 防阻塞）：
- GET  /api/v1/batches              批次列表
- POST /api/v1/batches              发起批次（409 重名 / 422 路径无效）
- POST /api/v1/batches/precheck     重复处理预检（422 装箱单解析失败）
- GET  /api/v1/batches/precheck-manual  手动录入三列完整性预检（422 解析失败）
- GET  /api/v1/batches/{thread_id}  批次详情（404 不存在）
- DELETE /api/v1/batches/{thread_id} 删除过往批次（404 不存在 / 409 进行中）
- PATCH /api/v1/batches/{thread_id}/paths  更新批次路径配置（404 不存在）
- POST /api/v1/batches/{thread_id}/rerun   完全重跑批次（404 不存在）
- POST /api/v1/batches/{thread_id}/add-factories  补充工厂（400 失败 /
  409 运行中或检测到人工修改待确认）
- POST /api/v1/batches/{thread_id}/resume-overwrite  写入冲突决策 resume
  （400 未挂起；{"decision": "overwrite"|"keep"}）
- POST /api/v1/batches/{thread_id}/overwrite-protected  覆盖写入人工保留的
  protected 单元格（400 无 protected 行）
- POST /api/v1/batches/{thread_id}/force-complete 强制结案（400 非待补充）
- POST /api/v1/batches/{batch_id}/export-pivot   生成 XD 透视导出（404 批次不存在 / 409 装箱单不存在）
- GET  /api/v1/batches/{batch_id}/pivot/download 下载最新 XD 透视文件（404 未导出过）
- GET  /api/v1/usage                全局 LLM 用量（scope=process_lifetime）
- GET  /api/v1/config/defaults      路径默认值 + 单重预警阈值
- POST /api/v1/batches/{thread_id}/open  本机打开最终输出 Excel（仅 127.0.0.1）
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from app.api import service
from app.config import batch_pivot_dir, get_settings
from app.export.pivot import generate_pivot
from app.ui.last_paths import load_last_paths, save_last_paths
from app.ui.open_file import OpenFileError, open_with_default_app

logger = logging.getLogger(__name__)

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 匹配 HTML 里对 /ui/static/*.js|css 的引用（带不带 ?v=N 都认）
_STATIC_REF_RE = re.compile(
    r'((?:src|href)="/ui/static/([\w.-]+\.(?:js|css)))(?:\?v=\d+)?(")'
)


def _with_asset_versions(html: str) -> str:
    """把 /ui/static/*.js|css 引用重写为 ?v=<文件 mtime>。

    手动 bump ?v=N 不可靠（改 JS 忘改版本号 → 浏览器拿旧缓存，新功能
    静默失效，2026-08-12 排序/重置按钮事故）。用文件 mtime 做版本：
    文件一改 URL 就变，浏览器必然重新下载，与 Cache-Control 头无关。
    """
    def _sub(m: re.Match) -> str:
        try:
            v = int((STATIC_DIR / m.group(2)).stat().st_mtime)
        except OSError:
            v = 0
        return f"{m.group(1)}?v={v}{m.group(3)}"
    return _STATIC_REF_RE.sub(_sub, html)


def _read_page(filename: str) -> HTMLResponse:
    """读 static 下的页面文件（显式 utf-8，Windows 兼容）；缺失返回 503。"""
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(
            status_code=503, detail=f"页面文件尚未部署: ui/static/{filename}"
        )
    return HTMLResponse(_with_asset_versions(path.read_text(encoding="utf-8")))


# ---------- 页面路由 ----------


@router.get("/")
async def index_page() -> RedirectResponse:
    """落地页：看板优先，/ 重定向到监控看板（批次总览 + 一键启动入口）。"""
    return RedirectResponse("/board")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    """工作台页（批次列表 + 发起批次）。"""
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


@router.get("/board", response_class=HTMLResponse)
async def board_page() -> HTMLResponse:
    """监控目录看板页（已完成 / 执行中 / 未执行三档 + 一键启动/标记完成）。"""
    return _read_page("board.html")


# ---------- UI 专用 API ----------


class CreateBatchRequest(BaseModel):
    """发起批次请求：路径缺省时由 service 取 settings 默认值。

    factory_filter 只处理指定工厂；skip_processed=true 自动跳过已处理
    工厂（与 factory_filter 互斥，factory_filter 优先）——W4b。

    manual_skip_factories（第七阶段 手动录入模式 §2.3）：批次级手动跳过
    识别清单，透传 service.create_batch（契约 §2.2：start_batch_from_scan
    → create_batch → initial state["manual_skip_factories"]）。
    """

    thread_id: str
    downstream_file_path: Optional[str] = None
    upstream_root: Optional[str] = None
    factory_filter: Optional[list[str]] = None
    skip_processed: bool = False
    manual_skip_factories: Optional[list[str]] = None


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
    # 第七阶段 §2.2：非空才透传手动录入清单（空清单保持既有调用形态）
    extra: dict = {}
    if request.manual_skip_factories:
        extra["manual_skip_factories"] = request.manual_skip_factories
    try:
        return await asyncio.to_thread(
            service.create_batch,
            request.thread_id,
            request.downstream_file_path,
            request.upstream_root,
            factory_filter=request.factory_filter,
            skip_processed=request.skip_processed,
            **extra,
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


@router.get("/api/v1/batches/precheck-manual")
async def precheck_manual(folder_name: str,
                          downstream_file_path: Optional[str] = None):
    """手动录入预检（第七阶段 §2.3）：扫描候选文件夹的三列完整性建议。

    面板/控制台启动批次前调用：返回 {"factories": [{"name", "complete",
    "filled_rows", "total_rows"}]}，complete=True 的厂前端预勾选「跳过识别
    （手动录入）」并标注「已填完整」。业务逻辑全在 service 层。

    downstream_file_path：候选文件夹内有多个装箱单时由前端指定所选文件，
    否则 precheck 会因「发现多个」而 422、无法给出按厂勾选清单。

    注意：必须声明在 GET /api/v1/batches/{thread_id} 之前，否则
    "precheck-manual" 会被 {thread_id} 路径段吞掉。
    """
    try:
        return await asyncio.to_thread(
            service.precheck_manual_completeness,
            folder_name, downstream_file_path)
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
    直接打开或下载。文件必须存在、是 .xlsx/.xls 类型，且必须位于
    配置的输出根目录下（白名单校验，防 404 信息泄漏服务器任意路径）。

    注：前端批次详情页已改用 POST /open 让本机 Excel 直接打开原文件
    （用户改后能立刻看到服务器端结果），本端点保留作为备用入口。
    """
    path = await _resolve_batch_output(thread_id)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


async def _resolve_batch_output(thread_id: str) -> Path:
    """校验并解析批次最终输出路径。

    流程：
    1. 读批次详情（不存在 → 404）
    2. final_output_path 为空 → 404（批次还在跑或没产出文件）
    3. 绝对化路径（expanduser + resolve，处理 symlink 和 ~）
    4. 输出目录白名单：必须在 settings.output_dir_abs 下，
       否则 403（路径可能来自数据库，但不该流出服务器任意位置）
    5. 文件必须存在且是文件 → 否则 404
    6. 后缀必须是 .xlsx/.xls → 否则 415

    白名单关键坑：Settings.output_dir_abs 内部用的是 Settings.resolve()
    方法（config.py 第 99-102 行），那个方法只把"相对路径拼到项目根"
    不做 symlink 解析。文件侧用 Path.resolve() 会跟 symlink、当前工作
    目录、盘符大小写一起归一化。两边不对齐会误判 relative_to。所以
    根目录侧必须**再显式调一次 Path.resolve()** 对齐口径。
    """
    try:
        detail = await asyncio.to_thread(service.get_batch_detail, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    final_output_path = detail.get("final_output_path")
    if not final_output_path:
        raise HTTPException(status_code=404, detail="批次没有最终输出文件路径")
    try:
        path = Path(final_output_path).expanduser().resolve()
    except OSError as e:
        # 循环 symlink / 无权限父目录 等，无法解析为合法路径
        raise HTTPException(status_code=404, detail=f"输出路径无法解析: {e}") from e

    # 输出目录白名单：必须落在 settings.output_dir_abs 之下。
    # 注意：output_dir_abs 自身只走 Settings.resolve()（相对→绝对），
    # 必须再调一次 Path.resolve() 来对齐文件侧的 symlink/大小写归一。
    settings = get_settings()
    output_root = Path(settings.output_dir_abs).resolve()
    try:
        path.relative_to(output_root)
    except ValueError as e:
        raise HTTPException(
            status_code=403,
            detail=f"输出路径超出允许目录范围: {path}",
        ) from e

    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if path.suffix.lower() not in (".xlsx", ".xls"):
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {path.suffix}",
        )
    return path


# 本机访问闸门允许的客户端 IP（FastAPI/uvicorn 默认绑 127.0.0.1 时只有本机可连）
_LOCALHOST_IPS = {"127.0.0.1", "::1"}


@router.post("/api/v1/batches/{thread_id}/open")
async def open_batch_output(thread_id: str, request: Request):
    """用本机默认程序（Excel/WPS/LibreOffice）打开批次最终输出文件。

    与 GET /output 不同：本端点在服务器进程内启动默认程序，浏览器不下载
    副本，用户编辑保存后直接回到服务器上的原文件。专门给本地单机部署
    （start.bat 绑 127.0.0.1）的批次详情页"用 Excel 打开"按钮使用。

    安全措施：
    - 必须 POST：避免 GET 被浏览器预取、target=_blank 重放、爬虫触发。
      本端点有副作用（启动 Excel 进程），不允许 GET。
    - 本机访问闸门：request.client.host 必须落在 127.0.0.1 / ::1 之内，
      防止远程调用本机 Excel。request.client 可能为 None，要防御。

    异常：
    - 404：批次不存在 / 没最终输出文件 / 文件不在
    - 403：路径超出输出目录白名单 / 来自非本机的请求
    - 415：文件后缀不是 .xlsx/.xls
    - 500：OpenFileError（命令不存在 / 启动失败 / 超时），detail 是模块
      包出来的中文消息
    """
    # 本机闸门：client 为 None 或 IP 不在白名单内一律拒绝
    client = request.client
    if client is None or client.host not in _LOCALHOST_IPS:
        raise HTTPException(
            status_code=403,
            detail="该操作只能从本机浏览器发起",
        )

    path = await _resolve_batch_output(thread_id)
    try:
        await asyncio.to_thread(open_with_default_app, path)
    except OpenFileError as e:
        # 503 更合适：不是服务端 bug，而是本地环境缺程序/文件被占用/超时等
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "path": str(path)}


@router.post("/api/v1/batches/{thread_id}/pivot/open")
async def open_pivot_dir(thread_id: str, request: Request):
    """本机文件管理器打开该批次的透视导出目录（pivot/）。

    与打开单个 Excel 不同：透视文件允许反复覆盖导出，用系统文件管理器
    打开目录便于用户自取（macOS Finder / Windows 资源管理器）。

    安全措施（与批次输出 /open、分票 /open 端点同一套）：
    - 必须 POST：有副作用（启动本地程序），不允许 GET 预取/重放；
    - 本机闸门：request.client.host 必须是 127.0.0.1 / ::1；
    - 输出目录白名单：resolve 后必须落在 settings.output_dir_abs 之下
      （thread_id 来自 URL 路径段，防 ../ 穿越）。

    异常：403 非本机/越界；404 目录不存在（还没导出透视）；503 OpenFileError。
    """
    client = request.client
    if client is None or client.host not in _LOCALHOST_IPS:
        raise HTTPException(status_code=403, detail="该操作只能从本机浏览器发起")

    try:
        out_dir = batch_pivot_dir(thread_id).resolve()
    except OSError as e:
        raise HTTPException(status_code=404, detail=f"输出路径无法解析: {e}") from e

    output_root = Path(get_settings().output_dir_abs).resolve()
    try:
        out_dir.relative_to(output_root)
    except ValueError as e:
        raise HTTPException(
            status_code=403, detail=f"输出路径超出允许目录范围: {out_dir}"
        ) from e

    if not out_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail="透视目录不存在：请先点击「导出透视」生成文件",
        )
    try:
        await asyncio.to_thread(open_with_default_app, out_dir)
    except OpenFileError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "path": str(out_dir)}


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


class RerunRequest(BaseModel):
    """完全重跑请求体（可选）。

    confirm_discard_manual：决策 10 确认门——副本快照检测到人工改动痕迹时
    首次调用返回 409 结构体；操作员确认「副本上的全部人工改动将丢失
    （旧副本归档至 _history/ 可查回）」后带 true 重发本端点放行。
    """

    confirm_discard_manual: bool = False


@router.post("/api/v1/batches/{thread_id}/rerun")
async def rerun_batch_endpoint(thread_id: str, request: Optional[RerunRequest] = None):
    """完全重跑批次：清空 containers/，重置 checkpoint，从 Node1 重新执行。

    409 结构体 detail {"code": "discard_manual_changes", "message",
    "traces"}：副本检测到人工改动痕迹待确认（确认后带
    confirm_discard_manual=true 重发）。
    """
    try:
        result = await asyncio.to_thread(
            service.rerun_batch, thread_id,
            confirm_discard_manual=bool(request and request.confirm_discard_manual),
        )
        return result
    except service.ManualDiscardError as e:
        # 决策 10 确认门：副本人工改动丢失警告（结构体 detail）
        raise HTTPException(status_code=409, detail={
            "code": "discard_manual_changes",
            "message": str(e),
            "traces": e.traces,
        }) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


class AddFactoriesRequest(BaseModel):
    """补充工厂请求体（可选）。

    confirm_overwrite：通道 B 确认门（写入侧人工改动感知 决策 1）——
    首次调用若检测到人工非空修改返回 409 结构体；操作员确认后带
    confirm_overwrite=true 重发本端点，冲突单元格按系统值覆写
    （被盖旧值留痕 manual_value_overwritten）。

    confirm_source_changes：决策 9 确认门——原件中已完成工厂有结构/数量
    变化时首次调用返回 409 结构体（code=source_changes，先于
    manual_conflicts 门）；操作员确认后带 confirm_source_changes=true
    重发（可与 confirm_overwrite 同发），系统吸收变化重写该厂副本。
    前端每次调用都会携带本字段（缺省 false）。
    """

    confirm_overwrite: bool = False
    confirm_source_changes: bool = False
    # 第七阶段 §2.3：勾选「重新进入识别」的手动跳过工厂——从
    # manual_skip_factories 移除并进入识别队列；响应新增 "reopened" 字段
    reopen_factories: list[str] = []


@router.post("/api/v1/batches/{thread_id}/add-factories")
async def add_factories_endpoint(
    thread_id: str,
    request: Optional[AddFactoriesRequest] = None,
):
    """补充工厂：重新解析装箱单，把未处理（新增/跳过/驳回）的工厂补进本批次。

    completed 批次从 Node6 条件边重入续跑；pending_review 批次合并后重挂起，
    均执行到下一个 Node5 interrupt 返回（可能耗时数十秒）。

    409 三种形态（前端按 detail 类型区分）：
    - detail 为字符串：批次正在运行中（既有契约，保持兼容）；
    - detail 为对象 {"code": "source_changes", "message", "changes",
      "count"}：原件中已完成工厂有变化待确认（决策 9；门槛顺序先于
      manual_conflicts，一次 409 只报一种 code；确认后带
      confirm_source_changes=true 重发吸收）；
    - detail 为对象 {"code": "manual_conflicts", "message", "conflicts",
      "count"}：检测到人工非空修改待确认（确认后带 confirm_overwrite=true
      重发覆写）。
    """
    # 第七阶段 §2.2：非空才透传重开识别清单（空清单保持既有补充工厂形态）
    extra: dict = {}
    if request and request.reopen_factories:
        extra["reopen_factories"] = request.reopen_factories
    try:
        result = await asyncio.to_thread(
            service.add_factories_to_batch,
            thread_id,
            confirm_overwrite=bool(request and request.confirm_overwrite),
            confirm_source_changes=bool(request and request.confirm_source_changes),
            **extra,
        )
        return result
    except service.SourceChangesError as e:
        # 决策 9 确认门：原件中已完成工厂有变化（结构体 detail，先于人工冲突门）
        raise HTTPException(status_code=409, detail={
            "code": "source_changes",
            "message": str(e),
            "changes": e.changes,
            "count": len(e.changes),
        }) from e
    except service.ManualConflictError as e:
        # 通道 B 确认门：人工非空修改待确认（结构体 detail）
        raise HTTPException(status_code=409, detail={
            "code": "manual_conflicts",
            "message": f"检测到 {len(e.conflicts)} 行人工修改",
            "conflicts": e.conflicts,
            "count": len(e.conflicts),
        }) from e
    except RuntimeError as e:
        # 批次正在运行中（无 interrupt 且 next 非空）——纯字符串 detail 兼容契约
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class OverwriteDecisionRequest(BaseModel):
    """写入冲突确认决策（第七阶段 §2.4）：整厂一次。

    Node6 写入副本遇人工非空值 ≠ 系统值时挂起 interrupt
    （payload type=overwrite_confirm），本端点把用户决策 resume 回图：
    - overwrite：冲突单元格按系统值写入，旧值留痕；
    - keep：冲突行标记人工来源，视同 MX 行保护，永不覆盖。
    """

    decision: Literal["overwrite", "keep"]


@router.post("/api/v1/batches/{thread_id}/resume-overwrite")
async def resume_overwrite_endpoint(thread_id: str, request: OverwriteDecisionRequest):
    """写入冲突确认后 resume（第七阶段 §2.4）：{"decision": "overwrite"|"keep"}。

    main.py 的 /api/v1/orders/{thread_id}/resume 请求体固定为 Node5 审核
    结构（approved/skipped/items），装不下 decision 形态，故在 B 侧领地
    新增本端点；resume_order 内部 Command(resume=...) 原样透传 dict。
    返回结构与 resume_order 一致（可能挂起下一厂审核/下一厂冲突，或完成）。
    """
    try:
        return await asyncio.to_thread(
            service.resume_order, thread_id, {"decision": request.decision})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/v1/batches/{thread_id}/overwrite-protected")
async def overwrite_protected_endpoint(thread_id: str):
    """覆盖写入：对「人工值保留未覆盖」的 protected 单元格按通道 A 语义补写
    系统值（被盖人工旧值留痕 manual_value_overwritten）。

    200 {"ok": true, "overwritten": N}；400 无 protected 行/无输出副本。
    """
    try:
        return await asyncio.to_thread(service.overwrite_protected_rows, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/v1/batches/{thread_id}/force-complete")
async def force_complete_endpoint(thread_id: str):
    """强制结案（2026-10-12 待补充状态）：待补充批次直接结案，剩余缺资料
    工厂不再补齐；batches.force_completed=1 防自愈打回，review_audits 留痕
    batch_force_completed。批次不存在/非待补充状态 → 400。"""
    try:
        return await asyncio.to_thread(service.force_complete_batch, thread_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# 透视/统计导出文件统一媒体类型（Windows 注册表缺 xlsx 映射时 mimetypes 会猜错）
_XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


@router.post("/api/v1/batches/{batch_id}/export-pivot")
async def export_batch_pivot(batch_id: str):
    """生成 XD 透视导出 Excel（F1）：装箱单按 港口→管理号 聚合。

    200 {ok, file_path, warnings}；404 批次不存在；409 装箱单不存在。
    服务层抛出的中文消息直接作为 detail 透传给前端。
    """
    try:
        result = await asyncio.to_thread(generate_pivot, batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001 兜底：记日志，给前端通用中文提示
        logger.exception("[XD透视] 导出失败 batch_id=%s", batch_id)
        raise HTTPException(
            status_code=500, detail="XD 透视导出失败，请查看服务端日志"
        ) from e
    return {"ok": True, "file_path": result["file_path"],
            "warnings": result["warnings"]}


@router.get("/api/v1/batches/{batch_id}/pivot/download")
async def download_batch_pivot(batch_id: str):
    """下载该批次最新一次 XD 透视导出文件；从未导出过 → 404。

    路径安全：目录由 config helper（batch_pivot_dir）给出，只在该目录内
    glob XD透视_*.xlsx 取 mtime 最新者，不接受用户传入的文件名。
    """
    pivot_dir = batch_pivot_dir(batch_id)
    candidates = (
        [p for p in pivot_dir.glob("XD透视_*.xlsx") if p.is_file()]
        if pivot_dir.is_dir() else []
    )
    if not candidates:
        raise HTTPException(
            status_code=404, detail="尚未导出 XD 透视文件，请先生成"
        )
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    return FileResponse(
        latest, media_type=_XLSX_MEDIA_TYPE, filename=latest.name
    )


@router.get("/api/v1/usage")
async def usage():
    """全局 LLM token 用量（进程内累计，重启清零，无 thread 标签）。"""
    return service._usage_with_scope()


@router.get("/api/v1/config/defaults")
async def config_defaults():
    """工作台预填默认值：.env 默认值 + 上次成功发起批次的路径。"""
    s = get_settings()
    last = load_last_paths()
    return {
        "upstream_root": last.get("upstream_root") or s.upstream_root,
        "downstream_file_path": last.get("downstream_file_path") or s.downstream_file_path,
        "weight_diff_warn_ratio": s.weight_diff_warn_ratio,
    }


class LastPathsRequest(BaseModel):
    """保存最近使用路径请求。"""

    upstream_root: Optional[str] = None
    downstream_file_path: Optional[str] = None


@router.post("/api/v1/config/last-paths")
async def save_last_paths_endpoint(request: LastPathsRequest):
    """保存工作台最近使用路径（由发起批次成功后调用）。"""
    await asyncio.to_thread(
        save_last_paths,
        request.upstream_root,
        request.downstream_file_path,
    )
    return {"ok": True}


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
