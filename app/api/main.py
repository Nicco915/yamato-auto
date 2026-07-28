"""FastAPI 入口：路由层极薄，业务逻辑集中在 app.api.service。

接口（按《api接口以及异步机制.md》第 1、2 节形状，同步实现）：
- POST /api/v1/orders/process              启动任务，跑到人工审核点挂起
- POST /api/v1/orders/{thread_id}/resume   人工反馈并继续（写 Excel + 落库）
- GET  /api/v1/orders/{thread_id}/state    查询任务当前状态

注意：graph.stream 为阻塞调用，全部经 asyncio.to_thread 放入线程池，
避免阻塞事件循环。Celery 迁移点见 service.py 顶部注释。
"""
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
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
