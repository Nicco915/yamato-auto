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
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api import service
from app.review.router import configure_review
from app.review.router import router as review_router
from app.ui.router import router as ui_router

app = FastAPI(title="供应链单证自动化 API", version="0.1.0")

# 人工双屏审核界面（/review + 单据查看 + payload 读取）
app.include_router(review_router)
# 单据文件访问白名单：默认限制在 settings.upstream_root（工厂文件夹）内
configure_review()

# UI 包：工作台/批次详情/对话页 + UI 专用 API（/api/v1/batches 等）
app.include_router(ui_router)
# UI 共享静态资产（ui.css / ui.js）
app.mount(
    "/ui/static",
    StaticFiles(directory=Path(__file__).resolve().parents[1] / "ui" / "static"),
    name="ui-static",
)


# ---------- Pydantic 请求模型 ----------

class ProcessRequest(BaseModel):
    thread_id: str                              # 批次号，如 "ETD0725-中地"
    downstream_file_path: Optional[str] = None  # 缺省用 .env 配置
    upstream_root: Optional[str] = None
    factory_filter: Optional[List[str]] = None  # 只处理指定工厂（调试用）


class ReviewSubmitRequest(BaseModel):
    approved: bool = True
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
    """
    session_id: Optional[str] = None            # 会话标识（服务端留存 pending action）
    message: Optional[str] = None               # 自然语言指令（确认前）
    confirm: bool = False
    action: Optional[Dict[str, Any]] = None     # 无 session 时的降级回传


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
            {"approved": request.approved, "items": request.items},
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
        # 确认执行不流式（瞬间完成），直接返回 JSON
        result = await asyncio.to_thread(
            dispatcher.confirm, request.session_id, request.action
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("message"))

        async def confirm_generator():
            yield f"data: {json.dumps({'type': 'applied', **result}, ensure_ascii=False)}\n\n"
        return StreamingResponse(confirm_generator(), media_type="text/event-stream")

    if not request.message:
        raise HTTPException(status_code=400, detail="message 不能为空")

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

            # 检查是否有进度事件
            event = None
            for d in done:
                if d is not task and not d.exception():
                    event = d.result()
                elif d is task:
                    break

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
