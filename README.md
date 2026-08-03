# 雅玛多供应链单证自动化

基于 LangGraph 状态机 + FastAPI + SQLite 的供应链单证自动化系统：从五花八门的
上游工厂单据中提取 SKU 总件数/总净重/总毛重，纯 Python 计算单件重量，经人工
双屏审核后精准写回下游买家的装箱明细表，并将 SKU 主数据（多语言品名、HS 编码、
单件重量）沉淀进数据库。

系统对操作员暴露**两个自然语言入口**：

- **调度 Agent**（`app/dispatcher/`）：系统大脑/前台——查批次、解释错误、发起与
  重跑批次、提交审核、改路径、操作指导问答，全部经对话完成，写操作一律人工确认；
- **提取 Agent 对话通道**（`app/agent_chat.py`）：生产授权的路径配置对话修改
  （LLM 解析 + 校验 + 人工确认 + .env 持久化）。

提取流水线（LangGraph worker）角色不变，被调度 Agent 调用真正干活。

## 架构图

```text
 操作员（浏览器：/chat 调度对话页、/review 审核页、/dashboard 批次看板）
        │
        ▼
┌────────────────────────────────────────────────────────────────┐
│                     FastAPI (app/api/main.py)                  │
│  /api/v1/dispatcher/chat(+stream)  /api/v1/agent/chat          │
│  /api/v1/orders/process|resume|state  /api/v1/review/* 等      │
└───────────────┬───────────────────────────────┬────────────────┘
                │                               │
   ┌────────────▼─────────────┐   ┌─────────────▼──────────────────┐
   │  调度 Agent (dispatcher/) │   │   LangGraph 状态机 (graph.py)   │
   │                          │   │                                │
   │  Triage 分诊 (triage.py) │   │  Node1 解析下游装箱单按工厂分组 │
   │   ↓ qa / clarify / action│   │    ↓                           │
   │  Executor 循环 (loop.py) │   │  ┌→ Node2 匹配工厂文件夹        │
   │   ↓ 12 工具 (tools.py)   │   │  │   ↓                         │
   │  确认门：写工具 preview → │   │  │  Node3 提取引擎              │
   │  人工 confirm → execute  │──▶│  │   ↓                         │
   │                          │   │  │  Node4 纯Python计算+DB联查   │
   │  L1 会话槽位 (sessions)  │   │  │   ↓                         │
   │  L2 操作记忆 (memory.py) │   │  │  Node5 🔴 interrupt 人工审核 │
   │  RAG 知识库 (rag/guide)  │   │  │   ↓ Command(resume=修改数据) │
   └──────────────────────────┘   │  └─ Node6 写Excel副本+DB upsert │
                                   │    ↓ 队列清空                  │
                                   │  Node7 终态导出                │
                                   └───────┬──────────┬───────────┘
                                ┌──────────▼───────┐  ┌▼───────────────┐
                                │ master.db        │  │ checkpoints.db │
                                │ 工厂/SKU/审核审计 │  │ 挂起/恢复持久化 │
                                └──────────────────┘  └────────────────┘
```

## 目录结构

```
app/                            # 仓库根（Python package 根，.env 相对路径以此为基准）
├── app/                        # 主包
│   ├── config.py               # pydantic-settings 配置（读 .env）
│   ├── state.py                # AgentState TypedDict
│   ├── graph.py                # StateGraph 编排 + SqliteSaver 编译
│   ├── agent_chat.py           # 提取 Agent 对话式路径配置（生产授权通道）
│   ├── factory_match.py        # 工厂名模糊匹配 + 别名表持久化
│   ├── logging_config.py       # 全链路日志（app.log / error.log 等）
│   ├── alias_map.json          # 工厂别名映射（下游日文名 → 本地中文文件夹名）
│   ├── nodes/                  # LangGraph 7 个节点
│   ├── extraction/             # 提取引擎（excel/doc/pdf_text/vision 四通道 + LLM 客户端）
│   ├── db/                     # SQLAlchemy 模型 + 会话（master.db）
│   ├── dispatcher/             # 调度 Agent（见下节）
│   ├── review/                 # 人工双屏审核页（/review）
│   ├── ui/                     # 生产前端页面（/dashboard、/chat、/batch/{id}）
│   ├── api/                    # FastAPI 路由（薄）+ service 层（逻辑集中）
│   ├── data/                   # 运行后生成：master.db / checkpoints.db / logs / KB 数据
│   └── output/                 # 运行后生成：写回的 Excel 副本
├── scripts/
│   ├── run_cli.py              # CLI 冒烟驱动：启动→interrupt→resume 全流程
│   └── sync_kb.py              # RAG 知识库幂等灌库（KB 变更后重跑）
├── validation/                 # 验证脚本（独立运行，非 pytest；含全部 dispatcher_*_test.py）
├── requirements.txt
└── .env.example
```

### 调度 Agent 包（app/dispatcher/）

```
dispatcher/
├── __init__.py   # 入口编排：handle_message（Triage 三分支路由）/ confirm（确认执行）
├── triage.py     # Triage 分诊层：TriageResult 结构化输出，失败一律降级旧循环
├── loop.py       # Executor tool-calling 循环：llm_step 双适配器 + 确认门 + hint 注入
├── prompts.py    # 三层 prompt：_TRIAGE_PROMPT / system_prompt（全量）/ executor_prompt（瘦身）
├── sessions.py   # L1 进程内记忆：history + pending_action + current_slots 槽位
├── memory.py     # L2 跨会话记忆：SQLite dispatcher_memory 表（按 session_id 分区）
├── tools.py      # 12 工具注册表（7 只读 + 5 写），写工具 preview/execute 分离
├── guide.py      # 操作指导问答（GUIDE_KB + LLM 润色 + 模板降级）
├── explain.py    # 批次错误翻译（ISSUE_KB 规则表 + LLM 翻译 + 模板降级）
├── rag.py        # RAG 检索后端（Pinecone 向量 / 关键词双后端 + 待策展队列）
├── summarize.py  # 写操作执行后的确定性中文摘要
└── debug_log.py  # 专用调试日志（app/data/logs/dispatcher.log，JSONL，脱敏）
```

## 启动方式

```bash
cd app/
pip3 install --user -r requirements.txt
cp .env.example .env        # 按需修改路径与 API key

# 方式一：CLI 冒烟（mock 提取数据，不依赖真实 LLM）
python3 scripts/run_cli.py --reset

# 方式二：FastAPI 服务
python3 -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

启动后页面入口：

- `/chat`：调度 Agent 对话页（自然语言管理批次，SSE 流式进度）
- `/dashboard`：批次看板；`/batch/{thread_id}`：批次详情
- `/review?thread_id=...`：人工双屏审核页

常用 API：

```bash
POST /api/v1/orders/process            {"thread_id": "ETD0725", "factory_filter": ["山東中地"]}
POST /api/v1/orders/{thread_id}/resume {"approved": true, "items": [...]}
GET  /api/v1/orders/{thread_id}/state
POST /api/v1/dispatcher/chat           {"message": "发起批次 ETD0725", "session_id": "..."}
POST /api/v1/dispatcher/chat/stream    # 同上，SSE 流式
POST /api/v1/agent/chat                # 提取 Agent 路径配置对话（生产授权通道）
```

thread_id 建议使用批次号，如 `ETD0725`。同一 thread_id 的挂起状态由
`data/checkpoints.db` 持久化，进程重启后仍可 resume。

## 调度 Agent 工作方式

对话数据流：**用户消息 → Triage 分诊（结构化 JSON）→ 三分支路由 → Executor → 确认门**：

1. **Triage 分诊**（`triage.py`）：零样本 LLM 调用输出 `TriageResult`
   （intent=qa/action/clarify + target_tool + extracted_args + confidence）。
   - `qa`：直接调 `guide.ask_guide` 走知识库回答，不进执行循环；
   - `clarify`：直接反问用户（已提取参数存入 L1 槽位，多轮自动补齐，
     「算了/取消」清空槽位）；
   - `action` 且 confidence>0.8：带 `triage_hint` 进执行循环；
   - **降级铁律**：分诊任何失败（LLM 异常、JSON 坏、`DISPATCHER_TRIAGE=off`）
     一律走旧执行循环，系统行为不劣于无分诊时代。
2. **Executor 循环**（`loop.py`）：tool-calling 循环，hint 仅作提示注入
   （LLM 仍自己发 tool_call）；有 hint 时用瘦身后的 `executor_prompt`，
   无 hint 时用全量 `system_prompt`。
3. **确认门**：写工具（create_batch / rerun / submit_review / set_paths /
   curate_kb）绝不直接执行——先 preview 生成人读预览存 `pending_action`，
   循环终止等人工 confirm；confirm 前再过 TTL（30 分钟）+ 参数复核 +
   execute 内部二次校验三道防线。一次一确认。

记忆分层：**L1** 进程内会话（history + pending_action + 参数槽位，重启即丢）；
**L2** SQLite 操作记忆（last_thread_id / recent_paths / 最近 10 次操作摘要，
跨重启延续，注入分诊与执行 prompt）；**L3** RAG 知识库（操作指引 + 错误案例，
Pinecone 向量 / 关键词双后端，未命中问题进待策展队列，人工确认后才入库）。

## 提取引擎接口契约

`app/extraction/pipeline.py` 对外暴露：

```python
extract_folder(folder_path: str) -> ExtractionReport
# ExtractionReport 是 list[dict] 的子类（附加 unsupported_files /
# file_errors / stats 属性）；dict 字段: sku_name / total_quantity /
# total_net_weight / total_gross_weight / weight_unit / source_file /
# needs_human_review
```

Node3 用 try/except ImportError 包装：提取线未就绪或 `.env` 中
`EXTRACTION_MOCK=1` 时使用 mock 数据，骨架可独立端到端跑通。

## 测试

验证脚本在 `validation/` 下，均为**独立运行的 Python 脚本**（非 pytest），
mock 环境变量在脚本内或命令行设置，无需 API key：

```bash
# 调度 Agent 全量（mock 剧本驱动，确定性）
DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/dispatcher_triage_test.py
DISPATCHER_MOCK=1 python3 validation/dispatcher_read_test.py
DISPATCHER_MOCK=1 python3 validation/dispatcher_write_test.py
# ... 其余 dispatcher_*_test.py 同模式；每个文件 docstring 有自己的用法行
```

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

- **LLM 只解析不做决策**：批不批、改不改、跑不跑永远人工说了算；调度 Agent
  只负责听懂话、调对工具、讲清结果；
- **写操作确认门**：写工具 preview/execute 分离 + 服务端留存 pending_action +
  TTL + 二次校验，LLM 绝无直接落库通道；
- **计算隔离**：单件重量 = 总重 / 总件数 全部由 Node4 纯 Python 执行，禁止 LLM 计算；
- **零容错人机协同**：Node5 `interrupt()` 强制挂起，人类数据 resume 时**强制覆写** state；
- **降级不劣化**：Triage 分诊、RAG 检索、LLM 润色等增强层任何失败都回落到
  无增强的可用路径（旧循环 / 关键词匹配 / 模板回答）；
- **原件保护**：Excel 只写 `output/` 目录下的副本；
- **主数据沉淀**：新 SKU 人工补录中文品名/HS 编码后 INSERT，老 SKU 人工微调后 UPDATE，
  单重与历史差异 >5% 自动标 Warning。

## 更多文档

- `PROGRESS.md`：实施日志与关键设计决策记录（含各阶段完成状态）
- `agent设计/`（仓库外层目录）：阶段设计文档
