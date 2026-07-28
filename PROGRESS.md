# 供应链单证自动化 — 项目进度总览

> 最后更新：2026-07-28（提取 Agent 三层构建完成：批处理 145/146 + 增量重放 9/10 全绿）
> 设计文档：`../agent设计/`（第一/二/三阶段、api接口以及异步机制、人工审核界面设计、提取agent背景prompt）

## 1. 项目目标

从上游工厂格式混乱的箱单/发票（xlsx/xls/pdf/doc/图片）中提取各 SKU 的总件数/总净重/总毛重，
**纯 Python 计算**单件净重/毛重（大模型禁止做任何数学计算），人工双屏审核后填入下游买家
装箱表（`96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx`，812 行 × 60 列），
并将 SKU 主数据（多语言品名/HS 编码/单重）沉淀入 SQLite。零容错，Human-in-the-Loop。

## 2. 技术栈

- **状态机**：LangGraph（7 节点 + 条件边工厂循环），SqliteSaver checkpointer（`app/data/checkpoints.db`）
- **LLM**：阿里云百炼 qwen3.7-plus（多模态，文本/视觉统一），OpenAI 兼容端点，key 在 `.env`（不入库）
- **服务**：FastAPI 同步接口（Celery+Redis 接缝已预留，暂未启用）
- **数据库**：SQLite（`app/data/master.db`，factories + factory_skus 两表）
- **前端**：原生 HTML/JS 单页（无构建链），`/review` 双屏审核界面

## 3. 完成状态总表

| 模块 | 状态 | 说明 |
|---|---|---|
| 骨架线：7 节点状态机 | ✅ 完成 | 冒烟测试全通过（mock 提取，端到端：挂起→覆写→resume→写Excel+落库→多工厂循环） |
| 骨架线：FastAPI 三接口 | ✅ 完成 | process / resume / state，实测通过 |
| 骨架线：DB 主数据 | ✅ 完成 | 新老 SKU 双路径（INSERT/UPDATE）实测正确 |
| 提取引擎：四通道 | ✅ 完成 | 见第 4 节（2026-07-27 工具层 6 项修复后） |
| 验证线：人工逐个核对 | ✅ 10/10 | 全部工厂全字段 100%（146 SKU），见第 5 节 |
| 提取 Agent：三层结构 | ✅ 完成 | 目标识别器 10/10；批处理 extract_factory 145/146；增量 FactorySession 重放 9/10 全绿（见第 5.1 节） |
| 审核界面：双屏 UI | ✅ 完成 | 已并入主服务，集成实测 8/8 通过（见第 6 节） |
| Node3 真实联调 | ⏳ 待办 | 去 mock 接真实 qwen3.7-plus 端到端（等人工核对完成后进行） |

## 4. 提取引擎（app/extraction/）

四通道按文件类型路由（`pipeline.extract_folder(folder_path)` 唯一入口，返回 ExtractionReport）：

```
.xlsx/.xls/.csv  → excel_channel      openpyxl 拆合并单元格 → Markdown → 文本LLM
.pdf 有文本层     → pdf_text_channel   PyMuPDF 抽文本 → 文本LLM（快速路，成本≈视觉1/10）
.pdf 无文本层     → vision_channel     200DPI 渲染 → 视觉LLM
.jpg/.png        → vision_channel
.doc/.docx       → soffice 转 PDF 优先；本机无 soffice 时 textutil 转 HTML → 文本LLM（doc_channel）
```

关键机制：
- **防幻觉 Prompt 十条铁律**：只抄原文、禁止计算、缺失留 null、毛净重同格拆分特例
- **`enable_thinking` 开关**（`LLM_ENABLE_THINKING=0`）：关闭推理后响应从几十秒降至约 1 秒，
  token 消耗大降；正达 28 分钟"假死"实为推理模式太慢
- **本批 30 个 PDF 全部有文本层**（100%），视觉通道实际只留给图片
- Pydantic 强制 Schema + `needs_human_review` 防跌落字段 + JSON 解析失败重试 2 次

2026-07-27 工具层修复（人工核对正达时发现，全部已单测/回归验证）：
1. **sheet 级目标识别**（`excel_channel.select_pl_sheets`）：只喂箱单 sheet
   （PL/Packing/箱单/装箱单），排除 INV/发票/合同/报关 sheet——正达 INV+PL 双 sheet
   导致输入被 `MAX_MARKDOWN_CHARS` 截断、后半 29 个 SKU 丢失的根因
2. **`MAX_MARKDOWN_CHARS` 24000→100000**（兜底防更大的表）
3. **`max_tokens` 默认 4096→16384**（llm_client）：93 行输出被截断 `finish_reason:length`，
  JSON 重试 3 次全败；修复前超时重试叠加是最长 30 分钟假死的原因
4. **全 0 占位行预过滤**（`excel_channel._drop_zero_rows`）：含 8–14 位条码且其余数值
  全 0 的行不送 LLM（正达 PL 约 42 行占位行），输入/输出双双减半，77s 完成
5. **输出顺序刚性保证**（`sort_by_source_order`，excel + pdf_text 通道）：按 sku_code
  在源文本首次出现位置稳定排序，输出顺序恒等于单据行序（人工核对习惯）
6. **重量口径标注 + 纯 Python 换算**（`weight_basis` 字段 + `apply_weight_basis`，四通道）：
  LLM 只做语义判断（该行重量是"合计"还是"每箱"），per_carton 时数值照抄、
  乘法（单箱重×件数）由纯 Python 完成——亿钻通用箱单只印每箱毛/净重
  （如 13.00/12.00 × 50 CTNS），32 SKU 全部此类；件数缺失无法换算时强制
  needs_human_review，绝不静默丢数。注意：模型对换算项的 review 标记有随机性
  （同文件两次运行一次全标一次全不标），数值不受影响

## 5. 验证线：人工逐个核对（ground truth：`96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx`
「バンニングリスト」sheet，2026-07-27 用户明确以此为准，已与报关匹配.xlsx 交叉核对一致）

方法：逐工厂人工确定提取目标文件（只读箱单 Packing List，含 SKU/件数/净重/毛重；
品名可有可无，必要时可从发票等其他单据补）→ 实跑对应通道 → 打印与 GT 逐格对照表，用户确认。

| 工厂 | 目标文件 | 结果 |
|---|---|---|
| 贝来（10 SKU） | 2 个「清关单据」xls 的 Packing List sheet（按票） | 10/10 全字段 100% |
| 达安（1 SKU） | `DA26461 箱单.xls`（条码在数据行下方的独立文本行） | 1/1 100% |
| 东基恒（9 SKU） | `请款资料/PACKING.pdf`（文本层；报关资料 PDF 为无条码汇总版） | 9/9 100% |
| 中地（14 SKU） | `XD-269760PackingList.xlsx` | 14/14 100% |
| 兆丰（12 SKU） | `总 清款资料/Packing…7.13.xlsx`（"总"=全量版；按日期报关资料为拆分版） | 12/12 100%（提取值精度高于 GT，GT 为 2 位小数舍入） |
| 华旭阳（6 SKU） | `XD269764-001 pl&ci.xls`（报关用单据为无条码汇总版） | 6/6 100% |
| 益尚（10 SKU） | 2 个「清关资料」PDF 第 2 页（"箱单"PDF 反而是品类汇总版！） | 10/10 100% |
| TOP（9 SKU） | `XD-269766-----请款资料.xls` 的 TOP PL sheet（毛重列在净重列左） | 9/9 100% |
| 正达（43 SKU） | `XD INV PL 请款用.xls` 的 PL sheet | 43/43 100%（修复后，77s） |
| 亿钻（32 SKU） | `装箱单通用RWS261233@.doc`（30 SKU，每箱重口径）+ `装箱单通用RWS261232@.pdf`（2 SKU）；报关版为无条码汇总 | 32/32 100%（weight_basis 修复后） |

**验证线总结（2026-07-27）**：10/10 工厂、146 个 SKU 全字段 100%。
所有提取值=单据原文（亿钻经纯 Python 单箱重×件数换算）。过程中修复工具层 6 项缺陷
（见第 4 节），每一项都是自动验证时代码"碰巧没踩到"的真实投产风险。

### 5.1 提取 Agent（2026-07-28 构建完成）

三层结构（设计：《提取agent如何识别文件.md》《提取agent背景prompt.md》）：

| 层 | 组件 | 验证 |
|---|---|---|
| ① 目标识别 | `target_identifier.py`（纯规则：13 位条码+净/毛/件数信号；路径负向信号硬否决） | `validate_targets.py` 10/10 |
| ② 四通道提取 | excel / pdf_text / doc / vision（+`verify.py` 重量口径 Total 行交叉校验） | 见下 |
| ③ 编排 | 批处理 `agent.py::extract_factory`；增量 `session.py::FactorySession` | 见下 |

- **批处理端到端**（`validate_agent.py`）：145/146。唯一未命中=TOP 4549509766544
  的 GT 分歧（Contents 第 112 行 3 箱 vs 箱单原文/报关匹配 60 箱，**用户决定暂时忽略**，待业务裁决）
- **增量模式**（`replay_session.py`，用户 2026-07-28 设定：按单据逐个处理）：
  - 负向候选（报关/通関先到）→ 暂缓 + 提示「暂无箱单」，**不拿汇总版凑合**；提示一次后静默
  - 完整性双轨：expected_skus 覆盖率 + mark_complete() 人工宣告，10 工厂全部 complete_auto
  - 改单语义：同 SKU 新值覆盖旧值入 history + needs_human_review
  - 人工告知通道：force_extract() 强制提取任何文件（含被暂缓的）
  - 成本控制：13 次 LLM 调用全部是真题目标；重复文件 already_processed 拦截
- **verify.py 重量口径交叉校验**（2026-07-27 两轮实测救回 2 类模型误标）：
  达安把合计重误标 per_carton（×75 倍）、亿钻 pdf 把每箱重误标 total（漏乘 40 倍）——
  用单据 Total 行数字做确定性翻正；Total 行解析按**非空行**前瞻（textutil HTML 单元格
  独占一行且行间有空行）；doc/pdf 校验用**未截断**全文（Total 行在文档末尾）
- **GT 构建修正**（ground_truth.py）：主源 ContentsOfTheContainer，空单元格（TOP KOPH
  21 行真空=系统待填行）用报关匹配.xlsx 行级填补
- **已知缺口**：纯图片箱单（本批未遇到，届时 vision 通道+人工确认）；条码≠13 位的工厂

**目标识别规律（目标识别器设计依据）**：
1. 不能按文件名/扩展名路由——「报关」文件多为品类汇总版（无条码），但益尚的「箱单」也是汇总版；
   必须按**内容特征**判定：有 13 位条码列 + SKU 级明细行（件数/净重/毛重）
2. sheet 级同理（已实现 `select_pl_sheets`）
3. 「总」字文件=全量版；按日期/票拆分的多为子集
4. 工厂文件夹常按票（PO 号）分子目录，每票一份箱单，合票文件（请款用/总）通常最全
5. 中文文件名标注类型（箱单/发票/清关资料/报关资料）只是先验，内容检测才是裁决
6. 标色可能有业务语义（正达送仓文件的红/黄行=分票归属标记），提取时应忽略颜色、按数值行判断

## 6. 人工双屏审核界面（app/review/）

- 启动 `uvicorn app.api.main:app` → 浏览器 `http://localhost:8000/review?thread_id=<批次号>`
- 左屏：原始单据查看器（PDF→PNG 页图带缓存可翻页 / Excel→HTML 表格快照 / 图片直出；
  路径白名单限制在工厂文件夹内，防目录穿越已实测 403）
- 右屏：SKU 卡片（状态着色、公式即时重算、新 SKU 强制补录 name_cn/hs_code/商检、
  error_msg/unexpected_sku 原因透传、missing_skus 红条、点击行左屏跳转 source_file）
- 提交 → `POST /api/v1/orders/{thread_id}/resume` → 多工厂循环自动进入下一工厂审核
- 集成实测 8/8 通过；payload 从 checkpoint `tasks[].interrupts` 读取，刷新页面可恢复现场

## 7. 关键设计决策记录

1. **计算隔离**：LLM 只提取，所有除法/对齐/写回均为纯 Python，从根源杜绝计算幻觉
2. **别名映射表**（`app/alias_map.json`）：下游工厂名是全角日文（`山東中地`），与本地中文文件夹名
   （中地）字符零重叠，模糊匹配无效，12 条人工映射，新工厂需维护此表
3. **同步先行**：FastAPI 同步接口 + `asyncio.to_thread`，Celery 接缝在 `service.py` 顶部注释
4. **写回不碰原件**：Node6 写入 `app/output/` 副本
5. **resume 请求体以代码为准**：`{"approved": bool, "items": [...]}`（设计文档的 modified_items 已过时）
6. **生产三层路径策略**（2026-07-28 用户定，审计确认骨架线已符合）：
   - 层1·根目录进配置：`settings.upstream_root`（env `UPSTREAM_ROOT` 可覆盖，
     请求体可按批次再覆盖），Node2 路由与审核界面白名单共用同一棵树
   - 层2·下游表随批次传：`ProcessRequest.downstream_file_path` → state，Node1/Node6/GT 全链路从 state 取
   - 层3·单文件随事件给：`process_file(session, path)` 由调用方递入，agent 不"找"文件
   - 别名映射表（alias_map.json）为持久配置，不随批次变；validation/ 内硬编码路径仅作测试基准
   - 禁止按显示名硬编码任何文件名（U+00A0 陷阱），定位一律走 glob/扫描/内容判定

## 8. 待办清单（按优先级）

1. **TOP 4549509766544 业务裁决**（用户已定暂时忽略）：Contents 第 112 行 3 箱 vs
   箱单原文/报关匹配 60 箱，确认后修正 GT 或标记例外
2. Node3 真实联调：把 extraction.agent / session 接入 LangGraph Node2/3
   （去 EXTRACTION_MOCK，山東中地端到端；增量模式与 Node1 expected_skus 对接）
3. `--consolidate` 更新 10 工厂汇总报告（用修复后的通道重跑或基于人工核对结论更新）
4. **业务确认**：下游表净重/毛重列多为公式单元格（仅 TOP KOPH 21 行真空），当前按
   "覆写=单重×行数量"实现，是否符合填表习惯待确认
5. 按需：Celery 迁移、LibreOffice 安装（brew 网络停滞，非必需，textutil 已兜底）

## 9. 环境备忘

- Python 3.13.5 系统环境；本机 pip 直连 PyPI 证书校验失败，需
  `pip3 install --user --trusted-host pypi.org --trusted-host files.pythonhosted.org <pkg>`
- **真实文件名普遍含不间断空格 U+00A0**（如 `ZDA26-0882A 家盈知 ….pdf`），禁止按显示名硬编码路径
- `.env` 变量名沿用 SILICONFLOW_API_KEY/BASE_URL（历史命名），实际指向百炼官方推荐端点
- thread_id 约定：`ETD0725-<工厂名>`；测试用 `INTEG-*` / `SMOKE-*`
- 下游表 SHOHIN_CD 为 13 位数字条码，必须按 str 读取（防科学计数法/丢前导零）
