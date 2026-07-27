# 供应链单证自动化

基于 LangGraph 状态机 + FastAPI + SQLite 的供应链单证自动化系统：从五花八门的上游工厂单据中提取 SKU 总件数/总净重/总毛重，纯 Python 计算单件重量，经人工双屏审核后精准写回下游买家的装箱明细表，并将 SKU 主数据（多语言品名、HS 编码、单件重量）沉淀进数据库。

## 架构图

```text
                    ┌──────────────────────────────────────────────┐
                    │               FastAPI (api/main.py)          │
                    │  POST /process   POST /resume   GET /state   │
                    └───────────────┬──────────────────────────────┘
                                    │ asyncio.to_thread（预留 Celery 接缝）
                    ┌───────────────▼──────────────────────────────┐
                    │            LangGraph 状态机 (graph.py)        │
                    │                                              │
                    │  Node1 解析下游装箱单，按工厂分组              │
                    │    ↓                                         │
                    │  ┌→ Node2 模糊匹配工厂文件夹（rapidfuzz+别名表）│
                    │  │   ↓                                       │
                    │  │  Node3 提取引擎（薄封装 app.extraction）    │
                    │  │   ↓                                       │
                    │  │  Node4 纯Python计算单重 + 公式 + DB联查     │
                    │  │   ↓                                       │
                    │  │  Node5 🔴 interrupt() 人工双屏审核         │
                    │  │   ↓ Command(resume=人类修改数据)           │
                    │  └─ Node6 写Excel副本 + DB upsert（队列循环）  │
                    │    ↓ 队列清空                                 │
                    │  Node7 终态导出                               │
                    └───────┬──────────────────┬───────────────────┘
                            │                  │
                 ┌──────────▼───────┐  ┌───────▼──────────┐
                 │ master.db        │  │ checkpoints.db   │
                 │ factories        │  │ LangGraph 状态   │
                 │ factory_skus     │  │ 挂起/恢复持久化  │
                 └──────────────────┘  └──────────────────┘
```

## 目录结构

```
app/
├── app/
│   ├── config.py            # pydantic-settings 配置（读 .env）
│   ├── state.py             # AgentState TypedDict（第一阶段.md 第 3 节）
│   ├── graph.py             # StateGraph 编排 + SqliteSaver 编译
│   ├── alias_map.json       # 工厂别名映射（下游日文名 -> 本地中文文件夹名）
│   ├── nodes/               # 7 个节点
│   ├── db/                  # SQLAlchemy 模型 + 会话（第三阶段.md）
│   ├── extraction/          # 【并行开发线】提取引擎（本骨架仅薄封装调用）
│   ├── validation/          # 【并行开发线】校验模块
│   └── api/                 # FastAPI 路由（薄）+ service 层（逻辑集中）
├── scripts/run_cli.py       # CLI 冒烟驱动：启动→interrupt→resume 全流程
├── tests/smoke_test.py      # 冒烟测试（子进程驱动 run_cli）
└── app/data/  app/output/   # 运行后生成：master.db / checkpoints.db / Excel 副本
```

> 注：`.env` 中的相对路径（如 `app/data/master.db`）以本目录（外层的 `app/`）为基准解析。

## 启动方式

```bash
cd app/
pip3 install --user -r requirements.txt
cp .env.example .env        # 按需修改路径与 API key

# 方式一：CLI 冒烟（mock 提取数据，不依赖真实 LLM）
python3 scripts/run_cli.py --reset

# 方式二：FastAPI 服务
python3 -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# POST /api/v1/orders/process  {"thread_id": "ETD0725-中地", "factory_filter": ["山東中地"]}
# POST /api/v1/orders/{thread_id}/resume  {"approved": true, "items": [...]}
# GET  /api/v1/orders/{thread_id}/state
```

thread_id 建议使用批次号，如 `ETD0725-中地`。同一 thread_id 的挂起状态由
`data/checkpoints.db` 持久化，进程重启后仍可 resume。

## 提取引擎接口契约

`app/extraction/pipeline.py`（并行开发线）对外暴露：

```python
extract_folder(folder_path: str) -> list[dict]
# dict 字段: sku_name / total_quantity / total_net_weight /
#            total_gross_weight / weight_unit / source_file / needs_human_review
```

Node3 用 try/except ImportError 包装：提取线未就绪或 `.env` 中
`EXTRACTION_MOCK=1` 时使用 mock 数据，骨架可独立端到端跑通。

## Celery + Redis 迁移预留说明

当前为**同步接口**（`graph.stream` 经 `asyncio.to_thread` 放入线程池，不阻塞
事件循环）。真实 LLM 提取耗时不可控，生产环境建议迁移 Celery+Redis 防 HTTP 超时：

1. **迁移点**：`app/api/service.py` 的 `run_until_interrupt()` 整体下沉为
   Celery task（函数签名不变，worker 内直接调用）；
2. **路由改造**：`POST /process` 改为 `task.delay(...)` 立即返回 task_id，
   新增 `GET /task-status/{task_id}` 轮询接口查 `AsyncResult`；
3. **resume 保持同步**：`resume_order()` 只做写 Excel + 落库，耗时短，无需走队列；
4. **状态记忆不依赖 Celery**：挂起状态在 LangGraph checkpointer（SQLite）里，
   Celery 任务到 interrupt 即结束，操作员半小时后 resume 时从 checkpoint 复活。

## 关键设计原则

- **计算隔离**：单件重量 = 总重 / 总件数 全部由 Node4 纯 Python 执行，禁止 LLM 计算；
- **零容错人机协同**：Node5 `interrupt()` 强制挂起，人类数据 resume 时**强制覆写** state；
- **原件保护**：Excel 只写 `output/` 目录下的副本；
- **主数据沉淀**：新 SKU 人工补录中文品名/HS 编码后 INSERT，老 SKU 人工微调后 UPDATE，
  单重与历史差异 >5% 自动标 Warning。
