# 雅玛多供应链单证自动化

基于 LangGraph 状态机 + FastAPI + SQLite 的供应链单证自动化系统：从五花八门的
上游工厂单据中提取 SKU 总件数/总净重/总毛重，纯 Python 计算单件重量，经人工
双屏审核后精准写回下游买家的装箱明细表，并将 SKU 主数据（多语言品名、HS 编码、
单件重量）沉淀进数据库。

系统对操作员暴露**两个自然语言入口**：

- **调度 Agent**（`app/dispatcher/`）：系统大脑/前台——查批次、解释错误、发起与
  重跑批次、提交审核、改路径、操作指导问答，全部经对话完成，写操作一律人工确认。
  当前处于**双引擎并存期**：生产默认 ReAct 引擎（`create_react_agent`），
  旧手写循环引擎保留作回退（见「调度 Agent 工作方式」一节）；
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
│  /api/v1/dispatcher/chat(+stream|last_operation|history)       │
│  /api/v1/agent/chat  /api/v1/orders/process|resume|state       │
│  /api/v1/review/*  /health                                     │
└───────────────┬───────────────────────────────┬────────────────┘
                │                               │
   ┌────────────▼─────────────┐   ┌─────────────▼──────────────────┐
   │  调度 Agent (dispatcher/) │   │   LangGraph 状态机 (graph.py)   │
   │  DISPATCHER_ENGINE 分流   │   │                                │
   │  ┌ react（生产默认）      │   │  Node1 解析下游装箱单按工厂分组 │
   │  │ create_react_agent    │   │    ↓                           │
   │  │ lc_llm + lc_tools     │   │  ┌→ Node2 匹配工厂文件夹        │
   │  │ 影子写工具/黄灯反问   │   │  │   ↓                         │
   │  └ legacy（回退保留）     │   │  │  Node3 提取引擎              │
   │  │ triage 分诊 + loop    │   │  │   ↓                         │
   │  13 工具注册表 (tools.py) │   │  │  Node4 纯Python计算+DB联查   │
   │  确认门：preview → 人工   │──▶│  │   ↓                         │
   │  confirm → execute       │   │  │  Node5 🔴 interrupt 人工审核 │
   │                          │   │  │   ↓ Command(resume=修改数据) │
   │  L1 会话槽位 (sessions)  │   │  └─ Node6 写Excel副本+DB upsert │
   │  L2 操作记忆 (memory.py) │   │    ↓ 队列清空                  │
   │  RAG 知识库 (rag/guide)  │   │  Node7 终态导出                │
   └──────────────────────────┘   └───────┬──────────┬───────────┘
                                ┌──────────▼───────┐  ┌▼───────────────┐
                                │ master.db        │  │ checkpoints.db │
                                │ 工厂/SKU/审核审计 │  │ 挂起/恢复持久化 │
                                └──────────────────┘  └────────────────┘
```

## 目录结构

注意：**仓库根就是 `app/`**，Python 主包再嵌套一层为 `app/app/`；`.env` 与
相对路径配置均以仓库根（`app/`）为基准。

```
app/                            # 仓库根（.env 相对路径以此为基准）
├── app/                        # 主包（import 路径 app.*）
│   ├── config.py               # pydantic-settings 配置（读 .env）
│   ├── state.py                # AgentState TypedDict
│   ├── graph.py                # StateGraph 编排 + SqliteSaver 编译
│   ├── agent_chat.py           # 提取 Agent 对话式路径配置（生产授权通道）
│   ├── factory_match.py        # 工厂名模糊匹配 + 别名表持久化
│   ├── logging_config.py       # 全链路日志（app.log / error.log / dispatcher.log）
│   ├── alias_map.json          # 工厂别名映射（下游日文名 → 本地中文文件夹名）
│   ├── nodes/                  # LangGraph 7 个节点
│   ├── extraction/             # 提取引擎（excel/pdf_text/vision/doc 四通道 + LLM 客户端）
│   ├── db/                     # SQLAlchemy 模型 + 会话（master.db）
│   ├── dispatcher/             # 调度 Agent（见下节）
│   ├── review/                 # 人工双屏审核页（/review + /api/v1/review/*）
│   ├── ui/                     # 生产前端页面（/dashboard、/chat、/batch/{id}）
│   ├── api/                    # FastAPI 路由（薄）+ service 层（逻辑集中）
│   ├── data/                   # 运行后生成：master.db / checkpoints.db / logs / KB 数据
│   └── output/                 # 运行后生成：写回的 Excel 副本
├── scripts/
│   ├── run_cli.py              # CLI 冒烟驱动：启动→interrupt→resume 全流程
│   ├── sync_kb.py              # RAG 知识库幂等灌库（KB 变更后重跑）
│   └── install_libreoffice.ps1 # Windows 一键装 LibreOffice（doc 通道依赖）
├── validation/                 # 验证脚本（独立运行，非 pytest；dispatcher 测试主体）
├── tests/                      # 独立测试脚本（非 pytest；冒烟/日志/分诊路由回归）
├── setup.bat / start.bat       # Windows 初始化 / 启动脚本
├── requirements.txt
└── .env.example
```

### 调度 Agent 包（app/dispatcher/）

```
dispatcher/
├── __init__.py      # 入口编排：handle_message（按 DISPATCHER_ENGINE 分流双引擎）
│                    #  + confirm（确认执行，两引擎共用同一通道）
├── react_engine.py  # react 引擎：langgraph.prebuilt.create_react_agent 薄封装
├── lc_llm.py        # Qwen BaseChatModel 适配器（复用 llm_client 重试/用量/mock 剧本）
├── lc_tools.py      # 影子工具层（闭包工厂每请求建工具）：写工具只 preview 存
│                    #  pending_action 绝不执行；request_clarification 黄灯反问
├── triage.py        # legacy 引擎：Triage 结构化分诊（失败一律降级旧循环）
├── loop.py          # legacy 引擎：手写 tool-calling 循环（双适配器 + 确认门）
├── prompts.py       # 各引擎 prompt：react_prompt / _TRIAGE_PROMPT / system_prompt /
│                    #  executor_prompt（legacy 三个保留至迁移完成）
├── tools.py         # 13 工具注册表（7 只读 + 6 写），写工具 preview/execute 分离
├── sessions.py      # L1 进程内记忆：history + pending_action + 槽位 + soft_pending
├── memory.py        # L2 跨会话记忆：SQLite dispatcher_memory 表（按 session_id 分区）
├── guide.py         # 操作指导问答（GUIDE_KB + LLM 润色 + 模板降级）
├── explain.py       # 批次错误翻译（ISSUE_KB 规则表 + LLM 翻译 + 模板降级）
├── rag.py           # RAG 检索后端（Pinecone 向量 / 关键词双后端 + 待策展队列）
├── summarize.py     # 写操作执行后的确定性中文摘要
└── debug_log.py     # 专用调试日志（app/data/logs/dispatcher.log，JSONL，脱敏）
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
GET  /api/v1/dispatcher/last_operation # 最近一次写操作摘要（L2 记忆）
GET  /api/v1/dispatcher/history        # 会话对话历史
POST /api/v1/agent/chat                # 提取 Agent 路径配置对话（生产授权通道）
GET  /api/v1/review/{thread_id}/payload    # 审核页数据包
GET  /api/v1/review/{thread_id}/document   # 审核页原始单据
GET  /health
```

thread_id 建议使用批次号，如 `ETD0725`。同一 thread_id 的挂起状态由
`data/checkpoints.db` 持久化，进程重启后仍可 resume。

## 调度 Agent 工作方式（双引擎并存期）

调度 Agent 当前有**两套引擎**，由环境变量 `DISPATCHER_ENGINE` 选择
（`handle_message` 入口处分流）：

- **`react`（生产默认）**：基于 `langgraph.prebuilt.create_react_agent` 的
  ReAct 引擎。意图判别、参数收集、多轮协商全部交还单循环模型自主完成，
  不再设独立分诊层。生产 `.env` 已切到 `react`。
- **`legacy`（回退保留）**：`triage.py` 结构化分诊 + `loop.py` 手写
  tool-calling 循环（分诊三分支路由 qa/clarify/action，降级铁律：分诊任何
  失败一律回落旧循环）。迁移验证完成后 legacy 路径将整体删除。

**切换/回退**：改 `.env`（或环境变量）`DISPATCHER_ENGINE=legacy|react` 重启
即可，代码零改动；两引擎对外的返回契约、SSE 事件口径完全一致，前端无感知。

### react 引擎数据流

用户消息 → `react_engine.run_dispatch_react`（组装 `react_prompt` + L2 记忆
上下文 + 会话历史）→ `create_react_agent` 单循环：

- **LLM 适配**（`lc_llm.py`）：Qwen 走 OpenAI 兼容端点，只实现
  BaseChatModel 薄接口（bind_tools/_generate），重试/退避/用量记录复用
  `llm_client`；不引 langchain-openai 等厂商包。
- **影子工具层**（`lc_tools.py`）：闭包工厂每次请求现建一套工具，
  session / session_id / on_progress 全部绑进闭包（LLM 无法伪造或越权跨
  会话）。只读工具直接执行；**写工具是影子工具**——与注册表同名同 schema
  但绝不执行，只生成预览、把 action 信封存进服务端 `session.pending_action`
  后硬停循环（`return_direct`），等人工确认。
- **黄灯反问**：模型推测用户想做写操作但不确定时调 `request_clarification`
  工具，代码生成确认式反问并存 soft_pending 软挂起；用户下一条回「是」
  **不进 LLM**，由入口短路用布防时已校验的参数直接出确认卡，「算了」全清。
- **超轮兜底**：recursion_limit=12 打满返回拆分提示文案（与 legacy 超
  MAX_ROUNDS 一致）。

### 安全模型（两引擎共用，不因引擎切换而变化）

- **唯一执行通道**：写操作只能经 `confirm()` → `execute_confirmed` 执行；
  confirm 前三道防线——TTL 30 分钟过期 + `validate_args` 参数复核 +
  `execute` 内部二次业务校验；
- **pending_action 服务端持有**：confirm 端点优先用服务端留存的信封，
  客户端回传仅作降级通道，防确认间隙被篡改；**一次一确认**（lc_tools 侧
  有模块级锁防并行双写出两张确认卡）；
- **工具注册表单一事实来源**（`tools.py`，13 个）：7 只读
  （list_batches / get_batch_status / get_batch_detail / get_review_payload /
  explain_errors / get_usage / ask_guide）+ 6 写（create_batch / rerun /
  retry_factory / submit_review / set_paths / curate_kb），写工具一律
  preview/execute 分离；react 引擎在此之上多一个 `request_clarification`；
- 调度 Agent 对用户只回**纯文本自然语言**，不暴露内部配置与路径细节。

### 记忆分层

**L1** 进程内会话（history + pending_action + 参数槽位 + soft_pending，
重启即丢）；**L2** SQLite 操作记忆（last_thread_id / recent_paths / 最近
10 次操作摘要，跨重启延续，注入两引擎的 prompt）；**L3** RAG 知识库
（操作指引 + 错误案例，Pinecone 向量 / 关键词双后端，未命中问题进待策展
队列，经 curate_kb 人工确认后才入库）。

## 提取引擎接口契约

`app/extraction/pipeline.py` 对外暴露：

```python
extract_folder(folder_path: str) -> ExtractionReport
# ExtractionReport 是 list[dict] 的子类（附加 unsupported_files /
# file_errors / stats 属性）；dict 字段: sku_name / total_quantity /
# total_net_weight / total_gross_weight / weight_unit / source_file /
# needs_human_review
```

按文件类型四通道路由：xlsx/xls/csv → excel 文本通道；pdf → 先探文本层走
pdf_text 快速路，扫描件回退视觉通道；jpg/png → vision 视觉通道；doc/docx →
soffice(LibreOffice) 转 PDF 路由（不可用回退 doc_channel 文本通道）。

Node3 用 try/except ImportError 包装：提取线未就绪或 `.env` 中
`EXTRACTION_MOCK=1` 时使用 mock 数据，骨架可独立端到端跑通。

## 测试

`validation/` 与 `tests/` 下均为**独立运行的 Python 脚本**（非 pytest），
mock 环境变量在脚本内或命令行设置，无需 API key；每个文件 docstring 有
自己的用法行。标准跑法：

```bash
python3 validation/<file>     # 或 python3 tests/<file>
```

调度 Agent 测试**双引擎可跑**（迁移期回归保障）：

- `validation/_dual_engine.py` 把同一份 mock 剧本同时注入两条引擎通道
  （legacy 的 `loop._MOCK_SCRIPT` 与 react 的 `lc_llm.set_script`），
  同一套断言在两种引擎下各跑一遍，任何一边行为漂移立刻变红；
- 用 `DISPATCHER_ENGINE` 环境变量切换被测引擎：

```bash
# 同一测试，两种引擎各跑一遍
DISPATCHER_MOCK=1 python3 validation/dispatcher_read_test.py                    # legacy（缺省）
DISPATCHER_ENGINE=react DISPATCHER_MOCK=1 python3 validation/dispatcher_read_test.py  # react

# react 引擎专属端到端（影子写确认门 / 黄灯三轮 / 并行双写一卡 / 超轮兜底）
python3 validation/dispatcher_react_engine_test.py

# 其余 dispatcher_*_test.py 同模式；guide 测试另加 GUIDE_MOCK=1
```

- **6 个 triage 专属测试硬钉 legacy**（文件内置 `DISPATCHER_ENGINE=legacy`，
  防外导 react 误跑）：`validation/dispatcher_triage_test.py`、
  `dispatcher_executor_prompt_test.py`、`triage_fewshot_test.py`、
  `triage_soft_confirm_test.py`、`next_step_routing_test.py`、
  `tests/dispatcher_triage_routing_test.py`。legacy 引擎删除后这些测试随
  `_dual_engine.py` 一起退役。

其他常用：

```bash
python3 tests/smoke_test.py                    # CLI 全流程冒烟（子进程驱动 run_cli.py）
python3 validation/run_validation.py --mock --all   # 提取线假数据自检（真实提取需 API key）
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
  TTL + 二次校验，LLM 绝无直接落库通道（双引擎同一安全模型）；
- **计算隔离**：单件重量 = 总重 / 总件数 全部由 Node4 纯 Python 执行，禁止 LLM 计算；
- **零容错人机协同**：Node5 `interrupt()` 强制挂起，人类数据 resume 时**强制覆写** state；
- **降级不劣化**：Triage 分诊、RAG 检索、LLM 润色等增强层任何失败都回落到
  无增强的可用路径（旧循环 / 关键词匹配 / 模板回答）；引擎级回退 =
  `DISPATCHER_ENGINE=legacy`；
- **原件保护**：Excel 只写 `output/` 目录下的副本；
- **主数据沉淀**：新 SKU 人工补录中文品名/HS 编码后 INSERT，老 SKU 人工微调后 UPDATE，
  单重与历史差异 >5% 自动标 Warning。

## 更多文档

- `PROGRESS.md`：实施日志与关键设计决策记录（含各阶段完成状态，双引擎迁移
  细节见 6.17 节）
- `agent设计/`（仓库外层目录）：阶段设计文档（含分诊软路由、react 迁移等
  专项设计）
