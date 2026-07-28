# 供应链单证自动化 — 项目进度总览

> 最后更新：2026-07-28（对话 Agent L1 会话记忆：多轮补充信息可合并，A+B 双层实现）
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
- **前端**：原生 HTML/JS（无构建链），工作台 `/` + 批次详情 `/batch/{id}` + 对话 `/chat` + `/review` 双屏审核界面（见第 6.5 节）

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
| Node3 真实联调 | ✅ 完成 | 去 mock 接真实 qwen3.7-plus，山東中地端到端 14/14 全绿（见第 6.1 节） |
| 生产前端：工作台/详情/对话/审核加固 | ✅ 完成 | 4 页面 + 批次管理 API + review_audits 审计落库，全链路测试通过（见第 6.5 节） |
| 对话 Agent：L1 会话记忆 | ✅ 完成 | 多轮补充信息（先路径后类别）可合并，A+B 双层实现，实测通过（见第 6.6 节） |
| 调度 Agent：智能体系统前台 | ✅ 完成 | 11 工具注册表 + 双适配器循环 + 确认门 + 操作指导，测试 21/21（见第 6.7 节） |

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

## 6.1 Node3 真实联调（2026-07-28 完成）

Node3 真实分支改为以**增量会话 FactorySession** 驱动（与生产"单据陆续到达"同语义）：

- `set_expected_skus()` 直接对接 Node1 的下游期望 SKU → 每次处理产出覆盖率，
  状态自动进 `complete_auto`；负向候选（报关 PDF）只登记不调 LLM；
- 会话 JSON 落盘 `app/data/sessions/<工厂名>.json` 供审计；
- `extraction_issues` / `extraction_coverage` 写入 state 并透传进 Node5 审核 payload；
- 提取为空时全量占位交人工补录（零容错，不空转）。

配套契约对齐：
- Node4 sku 键改 `sku_code or sku_name`（条码优先，无条码条目落品名交人工），
  extracted_data 透传品名原文与 review_reason；
- Node2 SUPPORTED_EXTS 补 `.doc/.docx`（亿钻 doc 箱单进审核界面文件列表）。

联调脚本 `validation/integ_graph_real.py`（`LLM_ENABLE_THINKING=0 python3 validation/integ_graph_real.py --reset`）：
山東中地端到端——8 个文件仅 1 次 LLM 调用（PackingList.xlsx），发票/6 份报关 PDF
全部规则拦截；14/14 SKU 对 GT 全字段一致；resume approved 后写 Excel 124 行 +
落库 INSERT 14，落库单重 = GT 总重/件数 逐项断言通过。mock 冒烟（tests/smoke_test.py）
同步回归通过。

**Node5 计算隔离加固**（2026-07-28）：人工在审核界面改总量（如净重）后，单重不再
依赖人工/前端提交 calculation——Node5 合并覆写后一律用 `_safe_div` 纯 Python 重算
（公式字符串同步刷新），改坏数值（如件数 0）自动回落 Error。前端提交的 calculation
变为纯展示参考，服务端不再采信。实测：改净重 250→300 且故意提交错误单重 999，
被重算覆盖为 6.0，Excel 写回 6.0×行数量（隔离环境验证，真实 master.db 未受影响）。

## 6.2 下游表写回规则（2026-07-28 用户定，已实现+实测）

- **原文件**：`96/ContentsOfTheContainer_202624_青島XD_20260708_原文件.xlsx` 是客户
  最原始文件（57 列，无 中文品名/净重/毛重）。生产输入**不**改配置默认值，
  按批次用请求体 `downstream_file_path` 传入（层2 路径策略不变）；
- **首次写入加列**：Node6 `_ensure_three_columns` 在 SHOHIN_MEI_E 后插入
  中文品名(32)/净重(33)/毛重(34)，表头样式照搬锚点列；已存在则跳过（幂等，
  多工厂循环复用同一副本安全）；三列部分缺失属异常布局，直接报错交人工；
- **写入值**：净重 = 单件净重 × SOTOBAKO_D_HACCHU_SU，毛重同理，**2 位小数**；
  中文品名 = 主数据 name_cn（新 SKU 经 Node5 人工补录）；
- **格式严格不变**：writer 全程 openpyxl（pandas 只读不写），实测其余 57 列
  与原文件逐格一致；`col_qty` 配置默认已从 D_HACCHU_SU 改为 SOTOBAKO_D_HACCHU_SU；
- 联调脚本 `validation/integ_graph_real.py` 同步覆盖以上全部断言（124 行中地数据）。

## 6.3 提取 Agent 对话式路径配置（2026-07-28 用户授权，已实现+实测）

生产环境人工指令通道：**操作员与提取 Agent 对话改路径，Agent 被授权直接修改**。

- **API**：`POST /api/v1/agent/chat`，两段式——
  ① `{"message": "工厂文件都在 /xxx 下面"}` → LLM 解析（只解析不决策）+
  纯 Python 校验 + 旧值→新值预览（`pending_confirmation`）；
  ② `{"confirm": true, "action": <回传>, "thread_id": <可选>}` → 执行。
- **授权白名单**（仅此三项，其他一律拒绝）：`upstream_root`（工厂文件夹，目录须存在）/
  `downstream_file_path`（下游表，文件须存在）/`gt_source`（GT 基准，文件须存在）；
  相对路径、不存在路径、白名单外配置项（如 api_key）全部被拦截。
- **生效范围**（用户定：持久+当前批次）：写回 .env（先备份 .env.bak，行级 upsert
  不动其他行）+ 刷新运行时配置 + 携带 thread_id 时当前批次从 Node1 重跑。
  重跑机制（langgraph 1.2.9 实测关键）：挂起线程有未完成 interrupt 任务，
  `Command(goto=)` 会被旧任务卡死，必须 `update_state(as_node=START)` 作废旧现场
  再 `invoke(None)`。
- 实测 `validation/chat_paths_test.py`：错路径启动→占位挂起→自然语言对话→确认→
  .env 更新+当前批次重跑命中中地；负例（不存在路径/闲聊/相对路径/白名单外）全拒。

## 6.4 Windows 平台兼容性改造（2026-07-28 完成，sub-agent 并行实施）

对全项目（重点 agent 工具链）做 Windows 兼容性检测与修复，macOS 行为零回归
（全模块 import + mock 冒烟全绿）：

- **对话改路径 Agent 支持 Windows 路径**（agent_chat.py，原为阻塞 bug）：
  prompt 从"只认 / 开头"改为认可三类绝对路径——Unix（/ 开头）、Windows 盘符
  （`X:\`/`X:/`）、UNC（`\\server\share`）；新增 `_is_absolute_path()` 跨平台判定；
  prompt 改 raw string 修复 `\f` 转义腐蚀；补 JSON 反斜杠转义规则。
- **异平台路径处理策略**（用户拍板：拒绝但提示跨机确认）：macOS 网关为 Windows
  生产机配 `D:\` 路径时，跳过本机存在性校验（不硬拒），由
  `cross_platform_warnings()` 产出中文警告、走既有 pending_confirmation 人工确认
  兜底；同平台不存在路径仍零容错硬拒。LLM reply 须注明识别到的路径风格
  （Unix / Windows 盘符 / UNC），防手滑反斜杠与 UNC 误判。
- **doc 通道 Windows 加固**（pipeline.py/doc_channel.py）：`SOFFICE_PATH` 环境变量
  最高优先级覆盖；Windows 候选补 `soffice.com`（控制台附着变体）与 Scoop 路径；
  subprocess 加 `-env:UserInstallation`（独立 profile，防与用户运行中的
  LibreOffice 实例/残留锁冲突——Windows 常见静默失败原因）；失败分三类 print 日志
  （soffice 不存在 / 调用失败含退出码 / 未产出 PDF），不再静默。textutil 兜底
  报错文案给出 Windows 三种 LibreOffice 安装方式。macOS 真实转换冒烟通过。
- **去硬编码 macOS 路径**：llm_client 的 .env 路径改 `Path(__file__).parents[2]`
  推导（与 PROJECT_ROOT 一致）；ground_truth / integ_graph_real / demo_server /
  chat_paths_test 的业务路径全部支持环境变量覆盖（默认值不变，macOS 现状不受影响）。
- **杂项**：folder_router alias 精确匹配失败后追加大小写不敏感兜底（带日志，
  Windows/macOS 大小写不敏感文件系统场景）；review 图片 MIME 用硬编码映射表优先
  （不依赖 Windows 注册表）；export_node `datetime.utcnow()` →
  `datetime.now(timezone.utc)`（输出格式逐字节一致，消除 3.12+ 弃用警告）；
  demo_server os.path 统一为 pathlib。
- **用户拍板暂不做**：python-docx 第三兜底（不支持老式 .doc——亿钻装箱单正是
  .doc，仍需 LibreOffice；复杂表格降维质量不如 PDF 通道）。
- **Windows 部署清单**：①.env 覆盖 UPSTREAM_ROOT / DOWNSTREAM_FILE_PATH
  （验证脚本另需 GT_SOURCE/GT_FALLBACK）；②安装 LibreOffice 或设 SOFFICE_PATH。
- 回归验证：compileall 全项目 + 关键模块 import + mock 冒烟（scripts/run_cli.py
  --reset）全绿；chat_paths_test 第 5 节 Windows 正例用例已写好，待真实 LLM 实跑。

## 6.5 生产前端（2026-07-28 完成，T1-T5 sub-agent 并行实施 + T6 全链路测试）

内网 1-2 操作员、无登录，延续原生 HTML/JS 零构建链。新包 `app/ui/`（页面路由极薄，
逻辑全在 service.py），共享资产 `ui.css`（设计令牌）/`ui.js`（api 封装/toast/topbar）。

**4 个页面**：
- `/`（/dashboard 别名）：工作台——发起批次卡（路径预填 defaults）+ 批次列表
  （状态 badge/当前工厂/进度/创建时间，10s 静默刷新），发起成功直接跳审核页
- `/batch/{thread_id}`：批次详情——批次头 + LLM 用量卡 + 工厂会话折叠卡
  （coverage/issues 按 level 着色/改单 history/file_records/deferred）+ 审核审计表
- `/chat`：Agent 对话配置页——包装 /api/v1/agent/chat 两段式
  （预览卡旧值→新值 + 确认执行/取消 + 重跑后去审核链接），Enter 发送
- `/review`：审核页生产加固（在原有文件上改，自包含不引 ui 资产，
  demo_server 不挂 /ui/static 仍可用）

**5 个新 API**（ui/router.py → service.py）：
- `GET /api/v1/batches`：checkpoint 只读连接（URI mode=ro）枚举 thread →
  逐个 get_state 推导三态 status（pending_review/running/completed）+ 进度 +
  创建时间（checkpoint blob msgpack ts 解出）；单线程异常降级 status="error" 不拖垮整表
- `POST /api/v1/batches`：thread_id 查重 409 → 路径存在性校验 422（指明哪个路径）
  → 复用 run_until_interrupt；请求体仅 thread_id/downstream_file_path/upstream_root
- `GET /api/v1/batches/{thread_id}`：state 摘要 + factories[]（factory/role:
  current·pending·done + session 摘要）+ audit[] + usage；不存在 404
- `GET /api/v1/usage`：usage_tracker.summary() + scope=process_lifetime 标注
- `GET /api/v1/config/defaults`：upstream_root/downstream_file_path/weight_diff_warn_ratio

**审核审计落库**：`review_audits` 表进 master.db（create_all 零迁移）。写入点在
`service.resume_order`——stream 前 `_prepare_audit`（get_review_payload 取原始
payload diff 出数值改动与新 SKU 补录），返回前 `_write_audit`；try/except 包死，
审计失败只警告绝不阻塞已成功的 resume。

**review.html 4 项加固**：①单重对照列（历史单件净重/毛重 → 本次值，diff 超
payload.weight_diff_warn_ratio（fallback 0.05）高亮，编辑实时重算）；
②键盘流（↑/↓ 切卡片、Enter blur+跳下一条、`/` 聚焦搜索）；
③SKU 搜索框（includes 过滤 + 计数 + Enter 跳下一命中）；
④提交前差异摘要弹窗（改动项 diff + 新 SKU 补录 + 未逐条过目 X/Y，有改动才弹）。
另兼容第四种状态 Needs_Review 的样式。

**契约要点**（前后端冻结）：
- 批次列表信封 `{"batches": [...]}`；详情工厂列表 `factories[].factory` +
  `progress.current_factory`；
- 审计 changes 为扁平结构 `[{"sku","field","old","new"}]`（None 与数值严格区分，
  提交 None 视为未改动）；
- Node5 payload 新增 `weight_diff_warn_ratio`（get_settings()，默认 0.05）。

**已知边界**：
- usage 为进程内累计、重启清零、无 thread 标签（全局用量，UI 已标注）；
- factories[].session 语义是"该工厂最近一次提取会话"，不专属本批次（UI 固定文案标注）；
- 无认证（内网 1-2 人，真正防线是 Node5 人工审核；Basic auth middleware 留 backlog）；
- 全新部署 checkpoints.db 不存在（或文件在但无表）时，`_open_checkpoint_ro` 自动
  `SqliteSaver.setup()` 建库建表后重开只读连接（2026-07-28 主 agent 审核 T6 时修复，
  两种场景实测）；
- `app.extraction.llm_client` import 时 `load_dotenv(override=True)` 会盖回
  os.environ 中 .env 已有的同名变量——测试脚本用 env 隔离 db 时必须在 import app
  之后重设 env + `get_settings.cache_clear()`（图/引擎为惰性单例，首个请求才创建）。

**全链路测试** `validation/ui_api_test.py`（`python3 validation/ui_api_test.py`，
mock 提取不调 LLM，秒级）：checkpoint/master db + output 目录全部隔离到临时目录
（绝不碰 app/data/ 生产 db，脚本开头有隔离断言兜底），下游装箱单复制到临时目录后
按批次传入。TestClient 断言流：defaults → 发起批次（首工厂挂起）→ 重名 409 →
坏路径 422 → 批次列表（信封/状态/进度/created_at）→ 详情（factories 角色/audit 空/
usage scope）→ 审核 payload（weight_diff_warn_ratio=0.05）→ resume（改总净重 +50 +
新 SKU 补录）→ 审计落库（扁平 changes old→new、edited_count、new_skus）→ usage →
4 页面路由 200 + 特征字符串 → 详情 404。全绿。

## 6.6 对话 Agent L1 会话记忆（2026-07-28 完成）

**问题**：/api/v1/agent/chat 原为无状态单轮解析——操作员分轮补充信息必然失败
（第 1 轮给绝对路径没说类别 → 问类别；第 2 轮只说"是下游装箱表"没给路径 → 问路径；
服务器从未合并两轮信息）。

**方案共识**（用户讨论拍板）：提取 LangGraph 工作流保持确定性状态机不动（要可复现
可审计），agentic 只放对话/运维层；"总 agent"等对话通道长出多能力后再演进，现在不建。
记忆分三级：L1 会话（本节实现）→ L2 批次（checkpointer 已有）→ L3 长期跨批次（未做）。

**A+B 双层实现**（agent_chat.py）：
- **路线 A**：`session_id` 标识的会话历史（最近 10 轮）随请求发给解析器，prompt 新增
  多轮合并规则（历史中未归类路径 + 本轮类别 → 填入 paths；反向同理）；
- **路线 B**：`category_hint` + `unclassified` 待归类槽位由**代码**持有（唯一事实来源），
  LLM 没合并时 `_merge_with_session()` 兜底：hint + 唯一待归类路径 → 合并为 set_paths；
  **多条待归类不猜**，保持 unknown 让操作员指明。白名单 + 绝对路径在 parse 层和
  merge 层双重设防——记忆只填槽位，绝不绕过校验/人工确认。
- **存储**：进程内 dict（TTL 2h、上限 500、threading.Lock）；重启即丢（可接受，
  确认前状态本就短命），需跨重启持久时再迁 app/db。缺省 session_id 保持无状态
  （向后兼容）。
- **前端**（chat.html）：session_id 存 localStorage（刷新后 Agent 仍记得上下文），
  chat/confirm 请求都携带；新增「开始新会话」链接。拒绝应答会主动提醒
  「我记得你给过路径：…，告诉我它属于哪一类即可」。
- confirm 后 `record_apply()` 把确认结果记入历史、已应用路径移出待归类槽位。

**实测**（chat_paths_test.py 第 6 节）：纯 Python 合并/多路径不猜/白名单防线单测 +
真实 LLM 两轮端到端（裸路径 → 拒绝但记住 → "刚才那个是工厂文件夹" → 合并出
pending_confirmation）+ 缺省 session_id 无状态兼容，全绿；FastAPI TestClient
冒烟（confirm/message 分支 session_id 透传）通过。

## 6.7 调度 Agent（2026-07-28 完成，sub-agent 并行实施）

**演进**：原系统全靠 HTTP 端点/UI 直接操作（发动批次/查状态/改路径/改数审核/重跑），
操作员必须逐个点按钮。新增调度 Agent 作为系统大脑/前台，操作员自然语言对话完成
所有操作；提取流水线（LangGraph 7 节点）不变作为 worker role 被调用。原路径配置
Agent（6.3 节）的 `set_paths` 作为调度 Agent 的一个工具整合进来。

**包结构** `app/app/dispatcher/`：
- `tools.py`：声明式 `Tool` dataclass + `TOOLS` 注册表（11 工具：7 只读 + 4 写）。
  `visible_tools(phase)` / `openai_tool_defs(phase)` / `validate_args(args, schema)`。
  只读 func 内部不抛异常（错误走 `{"error": ...}`），写工具带 preview + execute。
- `loop.py`：`llm_step` 双适配器（`DISPATCHER_STEP_MODE=native|json` 环境变量一键切），
  `run_dispatch(message, session, phase)`、`execute_confirmed(session, client_action)`。
  `DISPATCHER_MOCK=1` 时走 `_MOCK_SCRIPT` 剧本队列（确定性测试关键口）。
  `MAX_ROUNDS=6` / `TOOL_RESULT_CAP=6000` / `ACTION_TTL_SEC=30min`。
- `sessions.py`：`DispatcherSession`（独立于 `agent_chat._SESSIONS`，
  模式复制后扩展 `pending_action` / `tool_history`），`get_session` / `record_turn`
  / `record_tool` / `clear_pending`。
- `prompts.py`：`system_prompt(phase)`，phase>=2 包含写工具段落，
  含"操作指导 vs 数据查询"说明。
- `explain.py`：`explain_errors(thread_id, factory=None)` → 数据收集 + `ISSUE_KB`
  规则表（建议动作只来自代码规则表，LLM 只负责措辞，**禁止发明动作**）+ LLM
  翻译 + 模板降级（`EXPLAIN_MOCK=1` 跳过 LLM）。
- `guide.py`：`ask_guide(question, thread_id=None)` → 操作指导问答。`GUIDE_KB`
  知识库（8 场景：新手引导、改路径、解释错误、重跑、**发起批次**、改数审核、
  最佳实践、FAQ）+ 上下文收集 + LLM 问答 + 模板降级（`GUIDE_MOCK=1`）。

**端点**：`POST /api/v1/dispatcher/chat`（`api/main.py`），请求
`DispatcherChatRequest{session_id, message, confirm, action}`。
现有 `/api/v1/agent/chat` **一字未动**（生产授权通道保持独立）。

**11 个工具**：
| 工具 | 类型 | 作用 |
|------|------|------|
| `list_batches` | 查数据 | 批次列表（可选 status_filter） |
| `get_batch_status` | 查数据 | 单批次轻量摘要 |
| `get_batch_detail` | 查数据 | 批次详情（factories/session/audit/usage） |
| `get_review_payload` | 查数据 | Node5 挂起审核包 |
| `explain_errors` | 解释 | 批次错误翻译（LLM+规则表） |
| `get_usage` | 查数据 | LLM 用量 |
| `ask_guide` | 指导 | 操作指导问答（知识库+LLM） |
| `create_batch` | 写操作 | 发起批次（路径展开+查重预检→confirm→跑图） |
| `rerun` | 写操作 | 重跑挂起批次（状态检查+路径 diff→confirm→重跑） |
| `submit_review` | 写操作 | 改数审核（代码侧 diff 预览→confirm→resume+审计落库） |
| `set_paths` | 写操作 | 改路径配置（复用 agent_chat.validate/preview/apply） |

**确认门**（铁律：LLM 只解析不做决策，写操作必须人工确认）：
- 写工具拦截 → `session.pending_action` 存信封（`kind:dispatcher_tool`）→ 循环立即终止
- confirm 优先用 session 留存版本（防客户端伪造），TTL 30 分钟，`validate_args` 复核，
  `execute` 内部二次校验
- 一次一确认：一条消息最多产出一个 pending action

**双适配器实测**（qwen3.7-plus via DashScope token-plan 端点）：
| 模式 | 耗时 | tokens | 特点 |
|------|------|--------|------|
| native（OpenAI tool_calls） | 23.6s | 5421 | 符合标准，多工具并行结构清晰，换模型容易 |
| json（自解析协议） | 18.4s | 5535 | 更快，可控性强，调试容易 |
**建议**：保持 native 为默认，保留 json 切换能力（环境变量一键切）。

**测试矩阵**：
- `validation/dispatcher_read_test.py`（8/8，`DISPATCHER_MOCK=1 EXPLAIN_MOCK=1`）：
  查询问答/多轮工具/未知工具/坏参数/轮数上限/explain 降级/json 适配器/端点冒烟
- `validation/dispatcher_write_test.py`（8/8，含 chat_paths 回归）：
  拦截未执行/confirm 执行/篡改防护/过期拒绝/diff 预览+审计落库/rerun/set_paths/回归
- `validation/dispatcher_guide_test.py`（5/5）：
  接口正常/知识库匹配/未知问题兜底/调度 Agent 调用 ask_guide/真实 LLM
- 现有 `chat_paths_test.py`、`ui_api_test.py` 全绿（零回归）
- **真实 LLM 调用验证**：
  - 调度循环（native 模式）：操作员问"现在有哪些批次？" → agent 调 list_batches →
    工具返回 → 继续推理生成人话回复（含表格）
  - 操作指导：操作员问"怎么发起新批次？" → agent 调 ask_guide → 返回知识库匹配 +
    LLM 润色后的操作指引

## 7. 关键设计决策记录

0. **路径集中配置 + agent 对话可改**（2026-07-28 用户授权）：`app/.env`「业务路径」节
   统一管理 `UPSTREAM_ROOT`（工厂文件夹）/`DOWNSTREAM_FILE_PATH`（下游表默认）/
   `GT_SOURCE`/`GT_FALLBACK`（GT 主源/兜底）；validation 各脚本硬编码路径已收编到
   该文件（ground_truth.FACTORY_FOLDER = UPSTREAM_ROOT）；GT 缓存带 _meta 来源校验，
   改路径自动作废重建；联调脚本下游表用 `INTEG_DOWNSTREAM_FILE` 覆盖（默认_原文件）。
   生产侧的对话修改通道见第 6.3 节（/api/v1/agent/chat，LLM 解析+人工确认）

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

1. **全量真实批次跑通（投产前最后一块）**：真实图端到端目前只走过山東中地
   （14 SKU）。下一步对其余 9 个工厂（正达 43 / 亿钻 32 / 中地外全部）
   走真实流程：process → /review 逐厂人工审核 → resume → 写回+落库，
   重点观察亿钻（doc 通道+每箱重口径）、TOP（毛净列倒置+GT 分歧 SKU）。
   可整批一次 process（多工厂循环）或按 factory_filter 逐个来
2. **TOP 4549509766544 业务裁决**（用户已定暂时忽略）：Contents 第 112 行 3 箱 vs
   箱单原文/报关匹配 60 箱，确认后修正 GT 或标记例外——全量跑到 TOP 前必须有结论
3. **审核界面渲染 extraction_issues/coverage**：Node5 payload 已透传（6.1 节），
   前端 /review 尚未展示「暂无箱单/改单/冲突」等 Agent 反馈条
4. `--consolidate` 更新 10 工厂汇总报告（用修复后的通道重跑或基于人工核对结论更新）
5. 按需：Celery 迁移（Windows 部署注意：Redis 需 WSL2/Docker 或 Memurai 替代）、
   UI Basic auth（内网认证，backlog）、对话会话跨重启持久化（L1 现为进程内
   dict，需要时迁 app/db）、L3 长期记忆（跨批次记工厂习惯，方向已讨论未定）。
   ~~LibreOffice 安装~~（2026-07-28 完成：清华镜像装 26.2.5，亿钻 doc 主路径实测通过；
   _find_soffice 三平台探测，textutil 降级纯兜底，commit c7583f8；
   sub-agent 复测：soffice→PDF 2.2s 带文本层，装箱单 doc GT 30/30、
   工厂级 extract_factory 32/32 全中，2 次 LLM 调用零解析失败，无 WARNING）

## 9. 环境备忘

- Python 3.13.5 系统环境；本机 pip 直连 PyPI 证书校验失败，需
  `pip3 install --user --trusted-host pypi.org --trusted-host files.pythonhosted.org <pkg>`
- **真实文件名普遍含不间断空格 U+00A0**（如 `ZDA26-0882A 家盈知 ….pdf`），禁止按显示名硬编码路径
- `.env` 变量名沿用 SILICONFLOW_API_KEY/BASE_URL（历史命名），实际指向百炼官方推荐端点
- thread_id 约定：`ETD0725-<工厂名>`；测试用 `INTEG-*` / `SMOKE-*`
- 下游表 SHOHIN_CD 为 13 位数字条码，必须按 str 读取（防科学计数法/丢前导零）
