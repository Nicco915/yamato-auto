# 供应链单证自动化 — 项目进度总览

> 最后更新：2026-07-27（人工核对验证线前存档点）
> 设计文档：`../agent设计/`（第一/二/三阶段、api接口以及异步机制、人工审核界面设计）

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
| 提取引擎：四通道 | ✅ 完成 | 见第 4 节 |
| 验证线：9/10 工厂 | 🟡 暂停 | 8 工厂 100%；亿钻通道已修好待重跑；正达待补测（见第 5 节） |
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

## 5. 验证线结果（ground truth：`96/报关匹配.xlsx`，人工报关产出，已抽样核对一致）

| 工厂 | 结果 |
|---|---|
| 中地、达安、东基恒、贝来、兆丰、华旭阳、益尚、TOP（71 SKU） | **全字段正确率 100%** |
| 中地（文本快速路复测，8 文件） | 100%，全部仅 4.7 万 token，PDF 超时问题消失 |
| 亿钻 | 🟡 通道已修复待重跑：重量数据在 4 个 .doc 内（textutil 已解析确认 32 行完整），非数据缺失 |
| 正达（43 SKU，10 文件） | ⏳ 未测，计划用 `LLM_ENABLE_THINKING=0` 提速模式 |

**⚠️ 用户决定：验证线转入人工逐个核对提取结果（2026-07-27），自动验证暂停。**

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

## 8. 待办清单（按优先级）

1. **人工逐个核对提取结果**（用户指定，当前最高优先级）——可复用 /review 双屏界面做核对工具
2. 亿钻重跑（命令：`python3 validation/run_validation.py --factory 亿钻`）
3. 正达补测（`LLM_ENABLE_THINKING=0 python3 validation/run_validation.py --factory 正达`）
4. `--consolidate` 更新 10 工厂汇总报告
5. Node3 真实联调（去 EXTRACTION_MOCK，山東中地端到端）
6. **业务确认**：下游表净重/毛重列多为公式单元格（仅 TOP KOPH 21 行真空），当前按
   "覆写=单重×行数量"实现，是否符合填表习惯待确认
7. 按需：Celery 迁移、LibreOffice 安装（brew 网络停滞，非必需，textutil 已兜底）

## 9. 环境备忘

- Python 3.13.5 系统环境；本机 pip 直连 PyPI 证书校验失败，需
  `pip3 install --user --trusted-host pypi.org --trusted-host files.pythonhosted.org <pkg>`
- **真实文件名普遍含不间断空格 U+00A0**（如 `ZDA26-0882A 家盈知 ….pdf`），禁止按显示名硬编码路径
- `.env` 变量名沿用 SILICONFLOW_API_KEY/BASE_URL（历史命名），实际指向百炼官方推荐端点
- thread_id 约定：`ETD0725-<工厂名>`；测试用 `INTEG-*` / `SMOKE-*`
- 下游表 SHOHIN_CD 为 13 位数字条码，必须按 str 读取（防科学计数法/丢前导零）
