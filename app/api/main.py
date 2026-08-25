"""FastAPI 入口：路由层极薄，业务逻辑集中在 app.api.service。

接口（按《api接口以及异步机制.md》第 1、2 节形状，同步实现）：
- POST /api/v1/orders/process              启动任务，跑到人工审核点挂起
- POST /api/v1/orders/{thread_id}/resume   人工反馈并继续（写 Excel + 落库）
- GET  /api/v1/orders/{thread_id}/state    查询任务当前状态

注意：graph.stream 为阻塞调用，全部经 asyncio.to_thread 放入线程池，
避免阻塞事件循环。Celery 迁移点见 service.py 顶部注释。
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api import service
from app.logging_config import _takeover_uvicorn, setup_logging
from app.review.router import configure_review
from app.review.router import router as review_router
from app.split.router import router as split_router
from app.ui.mappings_api import router as mappings_router
from app.ui.router import router as ui_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn.run(app 对象) 方式启动时，uvicorn 会在模块导入（setup_logging）
    # 之后才用自带 dictConfig 重配 uvicorn 系列 logger，把接管结果覆盖掉；
    # startup 阶段再接管一次，确保 uvicorn 日志统一走 root 的 handler/格式
    _takeover_uvicorn()
    # 启动幂等迁移：product_mappings.sku_code 旧列 → product_mapping_skus 子表
    # （函数内部已兜底：失败只记 warning，绝不阻断启动）
    from app.db.sync import ensure_mapping_skus_migrated
    ensure_mapping_skus_migrated()
    yield


app = FastAPI(title="供应链单证自动化 API", version="0.1.0", lifespan=lifespan)

# 中央日志配置（幂等）：控制台 + app.log/error.log 滚动文件，接管 uvicorn
# 必须放在 import 链（内部已 load_dotenv）之后调用，否则 .env 的 LOG_LEVEL 不生效
setup_logging()

# 人工双屏审核界面（/review + 单据查看 + payload 读取）
app.include_router(review_router)
# 单据文件访问白名单：默认限制在 settings.upstream_root（工厂文件夹）内
configure_review()

# UI 包：工作台/批次详情/对话页 + UI 专用 API（/api/v1/batches 等）
app.include_router(ui_router)
# UI 共享静态资产（ui.css / ui.js）

# 分票 API：启动/查看/确认/重置（与提取图共用 checkpoints.db，split- 前缀区分）
app.include_router(split_router)
# 主数据维护 API：产品映射 + 品名组（/api/v1/mappings）
app.include_router(mappings_router)
app.mount(
    "/ui/static",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "ui" / "static"),
    name="ui-static",
)


# ---------- 请求日志中间件 ----------
# 每个请求记一行「method path → 状态码 耗时ms」；4xx/5xx 升级为 WARNING
# 并尽力带出响应体中的 detail（读不到就省略，绝不影响请求本身）。
# 注意：SSE 流式端点 /api/v1/dispatcher/chat/stream 的耗时是整个流的持续
# 时间（流关闭才返回），不是首字节时间——其响应体不读取、原样透传。
@app.middleware("http")
async def request_logging_middleware(request, call_next):
    start = time.perf_counter()
    try:
        # 下游异常照常上抛（交 FastAPI/uvicorn 默认处理），本中间件只记日志
        response = await call_next(request)
    except Exception:
        logger.exception("[HTTP] %s %s → 未捕获异常（%.0fms）",
                         request.method, request.url.path,
                         (time.perf_counter() - start) * 1000)
        raise

    elapsed_ms = (time.perf_counter() - start) * 1000
    status = response.status_code

    if status >= 400:
        # 尽力从 JSON 响应体取 detail：读完原地替换 body_iterator（不重建
        # Response，headers/background 等属性全保留），取不到不勉强
        detail = None
        if "application/json" in (response.headers.get("content-type") or ""):
            body = None
            try:
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            except Exception:  # noqa: BLE001 消费中途失败无法补救，原样返回
                logger.warning("[HTTP] %s %s → %d（%.0fms）响应体读取异常",
                               request.method, request.url.path, status, elapsed_ms)
                return response

            async def _replay():
                yield body
            response.body_iterator = _replay()

            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    detail = data.get("detail")
            except Exception:  # noqa: BLE001 非合法 JSON 只影响 detail 提取
                detail = None
        logger.warning("[HTTP] %s %s → %d（%.0fms）detail=%s",
                       request.method, request.url.path, status, elapsed_ms,
                       detail if detail is not None else "-")
    elif request.url.path.startswith("/ui/static") or request.url.path == "/health":
        # 静态资产/健康检查属高频噪音，降为 DEBUG
        # no-cache：静态文件无版本号，不加则浏览器启发式缓存旧 JS
        # （曾导致新功能上线后点击无反应）；配合 ETag 走 304，性能无损
        if request.url.path.startswith("/ui/static"):
            response.headers["Cache-Control"] = "no-cache"
        logger.debug("[HTTP] %s %s → %d（%.0fms）",
                     request.method, request.url.path, status, elapsed_ms)
    else:
        logger.info("[HTTP] %s %s → %d（%.0fms）",
                    request.method, request.url.path, status, elapsed_ms)

    return response


# ---------- Pydantic 请求模型 ----------

class ProcessRequest(BaseModel):
    thread_id: str                              # 批次号，如 "ETD0725-中地"
    downstream_file_path: Optional[str] = None  # 缺省用 .env 配置
    upstream_root: Optional[str] = None
    factory_filter: Optional[List[str]] = None  # 只处理指定工厂（调试用）


class ReviewSubmitRequest(BaseModel):
    approved: bool = True
    skipped: bool = False                   # 「跳过本工厂」：不写 Excel、不落库、批次照常推进
    items: List[Dict[str, Any]] = []            # 人工修改后的完整 items


class AgentChatRequest(BaseModel):
    """提取 Agent 对话（路径配置指令）。

    两段式：先发 message 拿解析预览（pending_confirmation）；
    确认后带 confirm=true + action（原样回传上一步的 action）执行。
    session_id 启用 L1 会话记忆（多轮补充信息用），缺省为无状态单轮。
    """
    thread_id: Optional[str] = None             # 携带时当前批次用新路径重跑
    session_id: Optional[str] = None            # L1 会话记忆标识（前端生成并持久）
    message: Optional[str] = None               # 自然语言指令（确认前）
    confirm: bool = False
    action: Optional[Dict[str, Any]] = None     # 确认执行时回传的解析结果


class DispatcherChatRequest(BaseModel):
    """调度 Agent 对话（批次管理智能体系统前台）。

    两段式：先发 message，调度循环调只读工具直接回答；遇到写操作返回
    pending_confirmation（action 信封 kind="dispatcher_tool"）；
    确认后带 confirm=true（action 可省略——服务端 session 留存优先）执行。

    文件选择：request_file_selection 工具挂起后，用户通过界面选择文件/目录，
    前端发 file_selection 字段（路径字符串），Agent 在下一轮看到所选路径。
    """
    session_id: Optional[str] = None            # 会话标识（服务端留存 pending action）
    message: Optional[str] = None               # 自然语言指令（确认前）
    confirm: bool = False
    action: Optional[Dict[str, Any]] = None     # 无 session 时的降级回传
    file_selection: Optional[str] = None        # 用户通过文件浏览器选择的路径


# ---------- 路由 ----------

@app.post("/api/v1/orders/process")
async def start_processing(request: ProcessRequest):
    """启动流程：Node1-4 执行完，在 Node5 interrupt 处挂起并返回审核数据。"""
    result = await asyncio.to_thread(
        service.run_until_interrupt,
        request.thread_id,
        request.downstream_file_path,
        request.upstream_root,
        request.factory_filter,
    )
    return result


@app.post("/api/v1/orders/{thread_id}/resume")
async def resume_processing(thread_id: str, request: ReviewSubmitRequest):
    """注入人工审核结果，唤醒图执行 Node6/7；多工厂时可能返回下一个审核包。"""
    try:
        result = await asyncio.to_thread(
            service.resume_order,
            thread_id,
            {"approved": request.approved, "skipped": request.skipped,
             "items": request.items},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"恢复执行时发生错误: {e}")
    return result


@app.get("/api/v1/orders/{thread_id}/state")
async def get_state(thread_id: str):
    """查询任务状态：next_nodes 非空表示挂起等待人工审核。"""
    return await asyncio.to_thread(service.get_order_state, thread_id)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------- 提取 Agent 对话（路径配置，2026-07-28 用户授权）----------

@app.post("/api/v1/agent/chat")
async def agent_chat(request: AgentChatRequest):
    """与提取 Agent 对话修改路径。

    - 确认前：{"message": "工厂文件夹改到 /xxx"} → LLM 解析+校验+预览；
    - 确认执行：{"confirm": true, "action": <上一步返回的 action>,
      "thread_id": <可选，当前批次立即重跑>} → 写 .env + 刷新运行时 + 重跑。
    """
    from app import agent_chat as chat

    if request.confirm:
        if not request.action or not isinstance(request.action.get("paths"), dict):
            raise HTTPException(status_code=400,
                                detail="confirm=true 时必须回传上一步的 action（含 paths）")
        try:
            result = await asyncio.to_thread(
                chat.apply_paths, request.action["paths"], request.thread_id
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # 确认结果记入会话历史（L1），已应用路径移出待归类槽位
        chat.record_apply(request.session_id, request.action["paths"])
        return {"status": "applied", **result}

    if not request.message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    return await asyncio.to_thread(
        chat.handle_message, request.message, None, request.session_id
    )


# ---------- 调度 Agent 对话（批次管理前台，只读一期已开通）----------

@app.post("/api/v1/dispatcher/chat")
async def dispatcher_chat(request: DispatcherChatRequest):
    """与调度 Agent 对话：查批次、解释错误、发起/重跑/审核/改路径。

    - 确认前：{"message": "现在有哪些批次待审核？"} → tool-calling 循环，
      只读工具直接回答；写操作返回 pending_confirmation（含预览）；
    - 确认执行：{"confirm": true, "session_id": <同前>} → 执行服务端留存的
      pending action（无 session 时需回传 action 信封，且会再过一次校验）。
    """
    from app import dispatcher

    if request.confirm:
        result = await asyncio.to_thread(
            dispatcher.confirm, request.session_id, request.action
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("message"))
        return result

    if not request.message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    return await asyncio.to_thread(
        dispatcher.handle_message, request.message, request.session_id
    )


# ---------- 调度 Agent 对话（SSE 流式，实时展示工具调用进度）----------

@app.post("/api/v1/dispatcher/chat/stream")
async def dispatcher_chat_stream(request: DispatcherChatRequest):
    """调度 Agent 对话 SSE 流式端点。

    与 /api/v1/dispatcher/chat 功能相同，但通过 Server-Sent Events 实时推送
    工具调用进度（llm_thinking → tool_call → tool_result → final /
    pending_confirmation），前端可展示"正在思考…""正在调用 xxx…"等动态状态。

    用法：前端用 fetch + ReadableStream 消费 SSE 事件流，每个事件一行 JSON。
    """
    from app import dispatcher

    if request.confirm:
        # 确认执行流式化（W4a）：与非 confirm 分支同构——后台线程跑
        # dispatcher.confirm，跑图节点进度经 on_progress → asyncio.Queue
        # 实时推送 exec_progress 事件；结束推送 applied / error（事件名不变，
        # 前端已有分支）。error 不再走 HTTPException（流已开始无法改状态码，
        # 且前端按 error 事件渲染）。
        confirm_queue: asyncio.Queue = asyncio.Queue()

        def on_confirm_progress(event: dict) -> None:
            """同步回调（在 asyncio.to_thread 线程中调用），把事件放入异步队列。"""
            try:
                confirm_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 队列满了就丢弃（不应该发生，但防御性处理）

        async def confirm_generator():
            task = asyncio.create_task(
                asyncio.to_thread(
                    dispatcher.confirm,
                    request.session_id,
                    request.action,
                    on_progress=on_confirm_progress,
                )
            )

            # 逐个推送进度事件（exec_progress 等）
            while True:
                try:
                    done, _ = await asyncio.wait(
                        [asyncio.create_task(confirm_queue.get()),
                         task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except Exception:
                    break

                event = None
                for d in done:
                    # 优先消费已取出的事件，再判断 task——否则 get() 已从队列
                    # 取出的事件会随 task 完成被静默丢弃（确定性丢首事件）
                    if d is task:
                        continue
                    if not d.exception():
                        event = d.result()

                if event is not None:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    continue

                if task.done():
                    break

            # 消费队列中剩余事件（进度回调可能在 task 完成后才到达）
            while not confirm_queue.empty():
                try:
                    event = confirm_queue.get_nowait()
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.QueueEmpty:
                    break

            # 发送最终结果：applied / error（事件名保持兼容）
            try:
                result = task.result()
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
                return

            if result.get("status") == "error":
                yield f"data: {json.dumps({'type': 'error', **result}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'applied', **result}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            confirm_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
            },
        )

    if not request.message and request.file_selection is None:
        raise HTTPException(status_code=400, detail="message 或 file_selection 不能同时为空")

    # asyncio.Queue 线程安全：同步 on_progress 回调 → queue.put() → 异步 get()
    progress_queue: asyncio.Queue = asyncio.Queue()

    def on_progress(event: dict) -> None:
        """同步回调（在 asyncio.to_thread 线程中调用），把事件放入异步队列。"""
        try:
            progress_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 队列满了就丢弃（不应该发生，但防御性处理）

    async def event_generator():
        """异步生成器：从队列取事件 → SSE 格式输出。"""
        # 文件选择回复走独立分支（与 message/confirm 互斥）
        if request.file_selection is not None:
            task = asyncio.create_task(
                asyncio.to_thread(
                    dispatcher.handle_message,
                    "",  # message 为空，file_selection 优先
                    request.session_id,
                    on_progress=on_progress,
                    file_selection=request.file_selection,
                )
            )
        else:
            # 先在后台线程启动 dispatch
            task = asyncio.create_task(
                asyncio.to_thread(
                    dispatcher.handle_message,
                    request.message,
                    request.session_id,
                    on_progress=on_progress,
                )
            )

        # 逐个推送进度事件
        while True:
            try:
                # 等待事件或任务完成，取先到者
                done, _ = await asyncio.wait(
                    [asyncio.create_task(progress_queue.get()),
                     task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                break

            # 检查是否有进度事件（优先消费已取出的事件，再判断 task——
            # 否则 get() 已从队列取出的事件会随 task 完成被静默丢弃）
            event = None
            for d in done:
                if d is task:
                    continue
                if not d.exception():
                    event = d.result()

            if event is not None:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                continue

            # 任务完成：发送最终结果
            if task.done():
                break

        # 消费队列中剩余事件（进度回调可能在 task 完成后才到达）
        while not progress_queue.empty():
            try:
                event = progress_queue.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.QueueEmpty:
                break

        # 发送最终结果
        try:
            result = task.result()
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
            return

        # 文件选择挂起：推送 file_selection_request 事件（前端弹出浏览器）
        # 注意：必须把 fs 的字段放在 fs_type/fs_extensions/fs_title 下，避免
        # 覆盖 SSE 事件本身的 type 字段（前端按 event.type 路由）
        if result.get("status") == "pending_file_selection":
            fs = result.get("file_selection", {})
            yield f"data: {json.dumps({
                'type': 'file_selection_request',
                'fs_type': fs.get('type'),
                'fs_extensions': fs.get('extensions'),
                'fs_title': fs.get('title'),
            }, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


# ---------- 调度 Agent 开场提示（上次操作摘要，纯确定性拼装，不经 LLM）----------

_OP_NAME_CN = {
    "create_batch": "创建",
    "rerun": "重跑",
    "submit_review": "提交审核",
    "set_paths": "改路径",
    "curate_kb": "策展知识库",
}


@app.get("/api/v1/dispatcher/last_operation")
async def dispatcher_last_operation(session_id: str):
    """对话开场提示：上次操作摘要（哪个批次、什么工厂）。

    纯确定性拼装——L2 记忆（dispatcher_memory 表）取 last_thread_id/最近操作，
    再由 service.get_order_state 反查 current_factory。任何异常都返回
    {"has_history": False}，前端回退静态开场白，绝不让页面加载挂掉。
    """
    try:
        from app.dispatcher.memory import OperationMemory, fmt_ago

        summary = OperationMemory(session_id).get_last_operation_summary()
        if not summary["has_history"]:
            return {"has_history": False, "text": None,
                    "thread_id": None, "factory": None}

        thread_id = summary["thread_id"]
        factory = None
        if thread_id:
            try:
                factory = service.get_batch_summary(thread_id).get("current_factory")
            except Exception:  # noqa: BLE001 批次被删/读库失败 → 无工厂不阻塞
                factory = None

        ago = fmt_ago(summary["ts"]) if summary["ts"] else ""
        op_cn = _OP_NAME_CN.get(summary["tool"], summary["tool"] or "")

        if thread_id and factory:
            text = (f"上次你在处理批次 {thread_id}（工厂：{factory}），"
                    f"{ago}{op_cn}。直接告诉我你想做什么。")
        elif thread_id:
            text = (f"上次处理批次 {thread_id}（{ago}{op_cn}）。"
                    "直接告诉我你想做什么。")
        else:
            text = f"上次操作：{ago}{op_cn}。直接告诉我你想做什么。"

        return {"has_history": True, "text": text,
                "thread_id": thread_id, "factory": factory}
    except Exception:  # noqa: BLE001 开场提示是辅助，绝不影响页面加载
        return {"has_history": False, "text": None,
                "thread_id": None, "factory": None}


# ---------- 调度 Agent 会话管理（CRUD + 左侧 sidebar）----------

class CreateSessionRequest(BaseModel):
    """创建新会话请求体。"""
    title: str | None = None
    pinned_thread_id: str | None = None


class UpdateSessionRequest(BaseModel):
    """更新会话请求体（仅传需要改的字段）。"""
    title: str | None = None
    pinned_thread_id: str | None = None
    is_pinned: bool | None = None  # 置顶标记


@app.get("/api/v1/dispatcher/sessions")
async def list_dispatcher_sessions(
    q: str | None = None,
    pinned_thread_id: str | None = None,
):
    """列出所有会话，按 is_pinned DESC + updated_at DESC 排序（左侧 sidebar 数据源）。

    支持：
    - 按标题模糊搜索：q=关键词 过滤 title ILIKE %关键词%
    - 按关联批次精确过滤：pinned_thread_id=xxx（工作台"对话"复用已有会话）
    对每个有 pinned_thread_id 的会话，额外批量查询批次状态（batch_status）。
    """
    try:
        from app.db.models import ChatSession as _ChatSessionOrm
        from app.db.session import get_session as _get_db_session

        with _get_db_session() as db:
            query = db.query(_ChatSessionOrm)
            if pinned_thread_id and pinned_thread_id.strip():
                query = query.filter(
                    _ChatSessionOrm.pinned_thread_id == pinned_thread_id.strip()
                )
            if q and q.strip():
                query = query.filter(_ChatSessionOrm.title.ilike(f"%{q.strip()}%"))
            rows = query.order_by(_ChatSessionOrm.is_pinned.desc(),
                                  _ChatSessionOrm.updated_at.desc()).all()

            # 批量查询批次状态（针对有 pinned_thread_id 的 session）
            pinned_ids = {r.pinned_thread_id for r in rows if r.pinned_thread_id}
            batch_statuses: dict[str, str] = {}
            for tid in pinned_ids:
                try:
                    summary = service.get_batch_summary(tid)
                    batch_statuses[tid] = summary.get("status", "unknown")
                except Exception:
                    batch_statuses[tid] = "unknown"

            return [
                {
                    "session_id": r.session_id,
                    "title": r.title,
                    "pinned_thread_id": r.pinned_thread_id,
                    "is_pinned": r.is_pinned,
                    "title_source": r.title_source,
                    "batch_status": batch_statuses.get(r.pinned_thread_id) if r.pinned_thread_id else None,
                    "updated_at": r.updated_at.timestamp() if r.updated_at else None,
                    "created_at": r.created_at.timestamp() if r.created_at else None,
                    "has_pending": r.pending_action_json is not None,
                }
                for r in rows
            ]
    except Exception:
        logger.exception("[HTTP] dispatcher/sessions 列表异常")
        return []


@app.post("/api/v1/dispatcher/sessions")
async def create_dispatcher_session(request: CreateSessionRequest):
    """创建新会话（session_id 由服务端 UUID 生成）。"""
    import uuid
    from app.db.models import ChatSession as _ChatSessionOrm
    from app.db.session import get_session as _get_db_session

    session_id = str(uuid.uuid4())
    try:
        with _get_db_session() as db:
            row = _ChatSessionOrm(
                session_id=session_id,
                title=request.title,
                pinned_thread_id=request.pinned_thread_id,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return {
                "session_id": row.session_id,
                "title": row.title,
                "pinned_thread_id": row.pinned_thread_id,
                "is_pinned": row.is_pinned,
                "title_source": row.title_source,
                "updated_at": row.updated_at.timestamp(),
                "created_at": row.created_at.timestamp(),
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建会话失败: {exc}")


@app.get("/api/v1/dispatcher/sessions/{session_id}")
async def get_dispatcher_session(session_id: str):
    """获取单个会话详情（含消息历史 + 工具审计流水 + pending_action）。"""
    try:
        from app.db.models import (ChatSession as _ChatSessionOrm,
                                   ChatMessage as _ChatMessageOrm,
                                   ChatToolHistory as _ChatToolHistoryOrm)
        from app.db.session import get_session as _get_db_session

        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if not row:
                raise HTTPException(status_code=404, detail="会话不存在")

            messages = (db.query(_ChatMessageOrm)
                        .filter(_ChatMessageOrm.session_id == session_id)
                        .order_by(_ChatMessageOrm.ts.asc())
                        .all())
            tools = (db.query(_ChatToolHistoryOrm)
                     .filter(_ChatToolHistoryOrm.session_id == session_id)
                     .order_by(_ChatToolHistoryOrm.ts.asc())
                     .all())

            pending = None
            if row.pending_action_json:
                try:
                    pending = json.loads(row.pending_action_json)
                except json.JSONDecodeError:
                    pending = None

            return {
                "session_id": row.session_id,
                "title": row.title,
                "pinned_thread_id": row.pinned_thread_id,
                "is_pinned": row.is_pinned,
                "title_source": row.title_source,
                "messages": [
                    {"role": m.role, "content": m.content, "ts": m.ts}
                    for m in messages
                ],
                "tool_history": [
                    {"tool": t.tool, "args_summary": t.args_summary,
                     "result_summary": t.result_summary,
                     "confirmed": t.confirmed, "ts": t.ts}
                    for t in tools
                ],
                "pending_action": pending,
            }
    except HTTPException:
        raise
    except Exception:
        logger.exception("[HTTP] dispatcher/sessions/{id} 详情异常")
        raise HTTPException(status_code=500, detail="获取会话详情失败")


@app.patch("/api/v1/dispatcher/sessions/{session_id}")
async def update_dispatcher_session(session_id: str, request: UpdateSessionRequest):
    """更新会话（title / pinned_thread_id）。"""
    try:
        from app.db.models import ChatSession as _ChatSessionOrm
        from app.db.session import get_session as _get_db_session

        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if not row:
                raise HTTPException(status_code=404, detail="会话不存在")

            if request.title is not None:
                row.title = request.title
                row.title_source = "manual"  # 手动改的标记为 manual
            if request.pinned_thread_id is not None:
                row.pinned_thread_id = request.pinned_thread_id
            if request.is_pinned is not None:
                row.is_pinned = request.is_pinned
            db.commit()
            db.refresh(row)

            return {
                "session_id": row.session_id,
                "title": row.title,
                "pinned_thread_id": row.pinned_thread_id,
                "is_pinned": row.is_pinned,
            }
    except HTTPException:
        raise
    except Exception:
        logger.exception("[HTTP] dispatcher/sessions/{id} 更新异常")
        raise HTTPException(status_code=500, detail="更新会话失败")


@app.delete("/api/v1/dispatcher/sessions/{session_id}")
async def delete_dispatcher_session(session_id: str):
    """删除会话（cascade 删除 messages + tool_history）。"""
    try:
        from app.db.models import ChatSession as _ChatSessionOrm
        from app.db.session import get_session as _get_db_session

        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if not row:
                raise HTTPException(status_code=404, detail="会话不存在")
            db.delete(row)
            db.commit()

        # 内存 dict 也清（若有）
        from app.dispatcher import sessions as _sessions
        with _sessions._SESSIONS_LOCK:
            _sessions._SESSIONS.pop(session_id, None)

        return {"deleted": session_id}
    except HTTPException:
        raise
    except Exception:
        logger.exception("[HTTP] dispatcher/sessions/{id} 删除异常")
        raise HTTPException(status_code=500, detail="删除会话失败")


# ---------- 调度 Agent 对话历史（W3：切页刷新恢复对话）----------

# pending_action 回传白名单：绝不回传 args（submit_review 的 items 巨大，
# 且确认执行以服务端留存为准，前端不需要参数本体）
_PENDING_ACTION_KEYS = ("tool", "summary", "preview_lines",
                        "warnings", "created_at", "factory_scan")


@app.get("/api/v1/dispatcher/history")
async def dispatcher_history(session_id: str = ""):
    """拉取对话历史 + 活着的待确认操作 + 待处理的文件选择（前端切页/刷新后恢复对话用）。

    - session_id 空 / 超长（>128）→ 400；
    - peek_session 只读不创建、不续 TTL：peek 不到则从 DB 兜底；
    - found：返回 history + 裁剪后 pending_action（白名单字段，绝不回传 args）；
      pending 超 ACTION_TTL_SEC 顺手 clear_pending 清尸，按 None 返回；
    - 同时返回 pending_file_selection（如有），供前端恢复文件浏览器模态框；
    - 任何意外异常兜底返回 found:false，绝不 500（页面加载不能挂）。
    """
    try:
        if not session_id or not session_id.strip():
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        if len(session_id) > 128:
            raise HTTPException(status_code=400,
                                detail="session_id 过长（上限 128 字符）")

        from app.dispatcher import sessions as _sessions
        from app.dispatcher.loop import ACTION_TTL_SEC

        # 先 peek 内存（快路径）
        sess = _sessions.peek_session(session_id)
        if sess is not None:
            pending = None
            action = sess.pending_action
            if isinstance(action, dict):
                age = time.time() - float(action.get("created_at", 0) or 0)
                if age > ACTION_TTL_SEC:
                    # 陈旧 pending 顺手清尸：恢复了也不能执行，不给前端假希望
                    _sessions.clear_pending(sess)
                else:
                    pending = {k: action.get(k) for k in _PENDING_ACTION_KEYS}

            # 文件选择挂起状态（TTL 同 ACTION_TTL_SEC）
            fs_pending = None
            fs = sess.pending_file_selection
            if isinstance(fs, dict):
                age = time.time() - float(fs.get("created_at", 0) or 0)
                if age > ACTION_TTL_SEC:
                    _sessions.clear_file_selection_request(sess)
                else:
                    fs_pending = {
                        "type": fs.get("type", "dir"),
                        "extensions": fs.get("extensions"),
                        "title": fs.get("title"),
                    }

            return {"found": True, "history": list(sess.history),
                    "pending_action": pending,
                    "pending_file_selection": fs_pending}

        # 内存 miss：从 DB 兜底（重启后恢复 / 进程内 TTL 过期但 DB 仍在）
        try:
            from app.db.models import (ChatSession as _ChatSessionOrm,
                                       ChatMessage as _ChatMessageOrm)
            from app.db.session import get_session as _get_db_session

            with _get_db_session() as db:
                row = db.get(_ChatSessionOrm, session_id)
                if not row:
                    return {"found": False, "history": [],
                            "pending_action": None,
                            "pending_file_selection": None}

                messages = (db.query(_ChatMessageOrm)
                            .filter(_ChatMessageOrm.session_id == session_id)
                            .order_by(_ChatMessageOrm.ts.asc())
                            .all())
                history = [{"role": m.role, "content": m.content} for m in messages]

                pending = None
                if row.pending_action_json:
                    try:
                        action = json.loads(row.pending_action_json)
                        age = time.time() - float(action.get("created_at", 0) or 0)
                        if age <= ACTION_TTL_SEC:
                            pending = {k: action.get(k) for k in _PENDING_ACTION_KEYS}
                        else:
                            # 陈旧 pending 清 DB
                            row.pending_action_json = None
                            db.commit()
                    except json.JSONDecodeError:
                        pass

                return {"found": True, "history": history,
                        "pending_action": pending,
                        "pending_file_selection": None}
        except Exception:
            logger.exception("[HTTP] dispatcher/history DB 兜底异常")
            return {"found": False, "history": [], "pending_action": None,
                    "pending_file_selection": None}
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 历史拉取是辅助，绝不让页面加载 500
        logger.exception("[HTTP] dispatcher/history 拉取异常，降级 found:false")
        return {"found": False, "history": [], "pending_action": None,
                "pending_file_selection": None}
