"""FastAPI 入口：路由层极薄，业务逻辑集中在 app.api.service。

接口（按《api接口以及异步机制.md》第 1、2 节形状，同步实现）：
- POST /api/v1/orders/process              启动任务，跑到人工审核点挂起
- POST /api/v1/orders/{thread_id}/resume   人工反馈并继续（写 Excel + 落库）
- GET  /api/v1/orders/{thread_id}/state    查询任务当前状态

注意：graph.stream 为阻塞调用，全部经 asyncio.to_thread 放入线程池，
避免阻塞事件循环。Celery 迁移点见 service.py 顶部注释。
"""
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.api import service
from app.review.router import configure_review
from app.review.router import router as review_router

app = FastAPI(title="供应链单证自动化 API", version="0.1.0")

# 人工双屏审核界面（/review + 单据查看 + payload 读取）
app.include_router(review_router)
# 单据文件访问白名单：默认限制在 settings.upstream_root（工厂文件夹）内
configure_review()


# ---------- Pydantic 请求模型 ----------

class ProcessRequest(BaseModel):
    thread_id: str                              # 批次号，如 "ETD0725-中地"
    downstream_file_path: Optional[str] = None  # 缺省用 .env 配置
    upstream_root: Optional[str] = None
    factory_filter: Optional[List[str]] = None  # 只处理指定工厂（调试用）


class ReviewSubmitRequest(BaseModel):
    approved: bool = True
    items: List[Dict[str, Any]] = []            # 人工修改后的完整 items


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
