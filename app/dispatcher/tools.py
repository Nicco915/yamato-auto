"""调度 Agent 工具注册表（声明式）：包装 service / agent_chat 的现成函数。

设计要点：
- 同一份 parameters JSON Schema 三处复用：LLM tools 定义（openai_tool_defs）、
  代码侧入参校验（validate_args）、测试断言——改一处，三处同步；
- 铁律：LLM 只解析不做决策。只读工具（risk="read"）由 loop 直接执行 func；
  写工具（risk="write"）绝不直接执行，由 loop 拦截走确认门——先 preview
  生成人读确认依据，操作员 confirm 后才调 execute（execute 内部必须二次校验）；
- 所有 func/preview/execute 内部异常一律捕获并返回 {"error": "..."} dict，
  绝不向外抛出（loop 层会把 error 回喂 LLM 让它自我修正）；
- 一期（phase=1）只暴露只读工具；二期（phase=2）开放全部写工具。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.api import service
from app.config import get_settings


@dataclass
class Tool:
    """一个可注册工具：schema + 风险等级 + 执行体（只读 func / 写 preview+execute）。"""

    name: str
    description: str            # 中文，写给 LLM 的选用依据
    parameters: dict            # JSON Schema（LLM tools 定义 / 代码校验 / 测试断言共用）
    risk: str                   # "read" | "write"
    func: Callable | None = None      # 只读工具执行函数：func(args: dict) -> dict
    preview: Callable | None = None   # 写工具：preview(args) -> {"summary", "lines", "warnings"}
    execute: Callable | None = None   # 写工具：confirm 后执行 execute(args) -> dict，内部二次校验


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _err(e: Exception) -> dict:
    """统一错误返回格式（绝不抛出，loop 会把 error 回喂 LLM）。"""
    return {"error": f"{type(e).__name__}: {e}"}


def _preview(summary: str, lines: list[str] | None = None,
             warnings: list[str] | None = None) -> dict:
    """写工具 preview 的统一返回结构。"""
    return {"summary": summary, "lines": lines or [], "warnings": warnings or []}


# ---------------------------------------------------------------------------
# 一期只读工具实现（risk="read"，func 包装 service）
# ---------------------------------------------------------------------------

def _fn_list_batches(args: dict) -> dict:
    """批次列表，可按状态过滤（pending_review / running / completed）。"""
    try:
        result = service.list_batches()
        status_filter = args.get("status_filter")
        if status_filter:
            result["batches"] = [
                b for b in result["batches"] if b.get("status") == status_filter
            ]
        return result
    except Exception as e:  # noqa: BLE001 工具层绝不抛出
        return _err(e)


def _fn_get_batch_status(args: dict) -> dict:
    """单批次轻量摘要：list_batches 摘要 + next_nodes / validation_status。"""
    try:
        thread_id = args["thread_id"]
        batches = service.list_batches().get("batches", [])
        summary = next((b for b in batches if b.get("thread_id") == thread_id), None)
        if summary is None:
            return {"error": f"批次不存在: {thread_id}"}
        state = service.get_order_state(thread_id)
        values = state.get("values") or {}
        return {
            **summary,
            "next_nodes": state.get("next_nodes"),
            "validation_status": values.get("validation_status"),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _fn_get_batch_detail(args: dict) -> dict:
    """批次详情（裁剪版）：issues 每项 message 截 200 字符，audit 只留最近 10 条。"""
    try:
        detail = service.get_batch_detail(args["thread_id"])
        for factory in detail.get("factories") or []:
            session = factory.get("session")
            if not session:
                continue
            for issue in session.get("issues") or []:
                if isinstance(issue, dict):
                    msg = issue.get("message")
                    if isinstance(msg, str) and len(msg) > 200:
                        issue["message"] = msg[:200] + "…"
        audit = detail.get("audit")
        if isinstance(audit, list) and len(audit) > 10:
            detail["audit"] = audit[-10:]
        return detail
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _fn_get_review_payload(args: dict) -> dict:
    """挂起审核包；未挂起时返回明确的 not_suspended 状态而非 None。"""
    try:
        payload = service.get_review_payload(args["thread_id"])
        if payload is None:
            return {"status": "not_suspended",
                    "message": "该批次当前未挂起待审核"}
        result = dict(payload)
        keep = ("sku", "status", "error_msg", "extracted_data")
        result["items"] = [
            {k: item.get(k) for k in keep}
            for item in (payload.get("items") or [])
            if isinstance(item, dict)
        ]
        return result
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _fn_explain_errors(args: dict) -> dict:
    """错误归因解读（由 app.dispatcher.explain 并行实现，延迟 import）。"""
    try:
        from app.dispatcher.explain import explain_errors
        return explain_errors(args["thread_id"], factory=args.get("factory"))
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _fn_get_usage(args: dict) -> dict:
    """全局 LLM token 用量摘要（进程内累计）。"""
    try:
        from app.extraction.llm_client import usage_tracker
        return usage_tracker.summary()
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _ask_guide_wrapper(args: dict) -> dict:
    """操作指导问答（延迟 import guide.py，guide.py 可能尚未实现）。"""
    try:
        from app.dispatcher.guide import ask_guide
        return ask_guide(args["question"], args.get("thread_id"))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# 二期写工具实现（risk="write"，preview + execute，execute 内二次校验）
# ---------------------------------------------------------------------------

def _preview_create_batch(args: dict) -> dict:
    """create_batch 预览：展开实际路径（缺省取 settings，与 service 同逻辑）+ 查重预检。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        settings = get_settings()
        downstream = args.get("downstream_file_path") or settings.downstream_file_path
        upstream = args.get("upstream_root") or settings.upstream_root

        lines = [f"批次 thread_id: {thread_id}"]
        warnings: list[str] = []
        if not thread_id:
            warnings.append("thread_id 为空，确认后执行会失败")
        else:
            state = service.get_order_state(thread_id)
            if state.get("exists"):
                warnings.append(f"thread_id 已存在，确认后执行会报重名错误: {thread_id}")

        d_path = Path(downstream).expanduser()
        u_path = Path(upstream).expanduser()
        lines.append(f"下游装箱单: {d_path}"
                     + ("" if args.get("downstream_file_path") else "（缺省值）"))
        lines.append(f"上游工厂文件夹: {u_path}"
                     + ("" if args.get("upstream_root") else "（缺省值）"))
        if not d_path.is_file():
            warnings.append(f"下游装箱单路径不存在或不是文件: {downstream}")
        if not u_path.is_dir():
            warnings.append(f"上游工厂文件夹路径不存在或不是目录: {upstream}")

        return _preview(f"将创建新批次 {thread_id} 并开始提取（Node1 跑到 Node5 挂起）",
                        lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_create_batch(args: dict) -> dict:
    """create_batch 执行：service 内部已做二次校验，异常转 {"error": ...} 不抛出。"""
    try:
        return service.create_batch(
            args["thread_id"],
            downstream_file_path=args.get("downstream_file_path"),
            upstream_root=args.get("upstream_root"),
        )
    except (FileExistsError, ValueError) as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _preview_rerun(args: dict) -> dict:
    """rerun 预览：当前状态（next_nodes，未挂起明确警告）+ 路径旧→新 diff。"""
    try:
        thread_id = args["thread_id"]
        state = service.get_order_state(thread_id)
        if not state.get("exists"):
            return _preview("无法重跑", [],
                            [f"批次不存在: {thread_id}"])

        values = state.get("values") or {}
        next_nodes = state.get("next_nodes") or []
        lines = [f"批次 {thread_id} 当前下一节点: {next_nodes or '（无，已完成）'}"]
        warnings: list[str] = []
        if not next_nodes:
            warnings.append("该批次未处于挂起状态：仅挂起批次可重跑，确认后执行会报错")

        for key, label in (("upstream_root", "上游工厂文件夹"),
                           ("downstream_file_path", "下游装箱单")):
            old = values.get(key) or "（未设置，走 .env 缺省）"
            new = args.get(key)
            if new:
                marker = "不变" if old == new else "修改"
                lines.append(f"[{marker}] {label}:\n    {old}\n -> {new}")
            else:
                lines.append(f"[不变] {label}: {old}")

        return _preview(f"将带新路径从 Node1 重跑批次 {thread_id}（回到 Node5 挂起）",
                        lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_rerun(args: dict) -> dict:
    """rerun 执行：service.rerun_with_paths 内部校验挂起状态，异常转 {"error": ...}。"""
    try:
        return service.rerun_with_paths(
            args["thread_id"],
            upstream_root=args.get("upstream_root"),
            downstream_file_path=args.get("downstream_file_path"),
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _preview_submit_review(args: dict) -> dict:
    """submit_review 预览：复用 _prepare_audit 的 diff 结果生成人读确认依据。"""
    try:
        thread_id = args["thread_id"]
        resume_data = {"approved": args["approved"], "items": args["items"]}
        prepared = service._prepare_audit(thread_id, resume_data)
        if prepared is None:
            return _preview("无法提交审核",
                            [f"批次 {thread_id} 当前未挂起待审核（或无原始审核包），"
                             "确认后执行会报错"],
                            ["仅挂起待审核的批次可提交审核结果"])

        approved = prepared.get("approved", False)
        edited_count = prepared.get("edited_count", 0)
        changes = prepared.get("changes") or []
        new_skus = prepared.get("new_skus") or []

        lines = [
            f"批次: {thread_id}（工厂: {prepared.get('factory_name')}）",
            f"审核结论: {'批准' if approved else '驳回'}",
            f"修改过的 SKU 数: {edited_count}",
        ]
        if not changes and not new_skus:
            lines.append("无字段改动、无新 SKU 补录（按原提取结果提交）")
        for c in changes:
            lines.append(f"[改动] SKU {c.get('sku')} 字段 {c.get('field')}: "
                         f"{c.get('old')} -> {c.get('new')}")
        for s in new_skus:
            lines.append(f"[新SKU] {s.get('sku')}: 中文品名={s.get('name_cn')}, "
                         f"HS编码={s.get('hs_code')}, 商检={s.get('inspection_required')}")

        warnings: list[str] = []
        if not approved:
            warnings.append("审核结论为驳回：确认后该工厂数据不会落库")
        return _preview(
            f"将提交批次 {thread_id} 的审核结果（{'批准' if approved else '驳回'}，"
            f"改动 {edited_count} 个 SKU）", lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_submit_review(args: dict) -> dict:
    """submit_review 执行：service.resume_order 内部二次校验挂起状态并落审计。"""
    try:
        resume_data = {"approved": args["approved"], "items": args["items"]}
        return service.resume_order(args["thread_id"], resume_data)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _preview_set_paths(args: dict) -> dict:
    """set_paths 预览：硬错误列顶 + 旧→新变更预览 + 异平台警告。"""
    try:
        from app import agent_chat

        paths = args["paths"]
        errors = agent_chat.validate_paths(paths)
        lines = [f"[硬错误] {e}" for e in errors]
        lines += agent_chat.preview_changes(paths)
        warnings = agent_chat.cross_platform_warnings(paths)
        if args.get("thread_id"):
            lines.append(f"确认后当前批次 {args['thread_id']} 将立即用新路径重跑")
        summary = (f"将修改 {len(paths)} 项路径配置并写入 .env 持久生效"
                   if not errors else
                   f"存在 {len(errors)} 个硬错误，确认后执行会被拒绝")
        return _preview(summary, lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_set_paths(args: dict) -> dict:
    """set_paths 执行：apply_paths 内含二次校验 + .env 备份 + 可选当前批次重跑。"""
    try:
        from app import agent_chat

        return agent_chat.apply_paths(args["paths"],
                                      thread_id=args.get("thread_id"))
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------

_THREAD_ID_PROP = {
    "type": "string",
    "description": "批次线程 ID（创建批次时由操作员指定）",
}

TOOLS: dict[str, Tool] = {
    # ---- 一期只读工具 ----
    "list_batches": Tool(
        name="list_batches",
        description="列出全部提取批次的摘要（状态/进度/当前工厂/时间）。"
                    "操作员问“有哪些批次”“最近跑了什么”时使用；"
                    "可用 status_filter 只看某一状态。",
        parameters={
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["pending_review", "running", "completed"],
                    "description": "按状态过滤：pending_review=挂起待审核，"
                                   "running=提取中，completed=已完成；不传返回全部",
                },
            },
        },
        risk="read",
        func=_fn_list_batches,
    ),
    "get_batch_status": Tool(
        name="get_batch_status",
        description="查询单个批次的轻量状态摘要（状态/进度/下一节点/校验结果）。"
                    "比 get_batch_detail 快，操作员只问“某某批次现在怎么样了”时优先用它。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_get_batch_status,
    ),
    "get_batch_detail": Tool(
        name="get_batch_detail",
        description="查询单个批次的完整详情（各工厂会话、审计记录、用量）。"
                    "内容较大，仅在操作员明确要求看细节时使用；"
                    "只看状态请用 get_batch_status。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_get_batch_detail,
    ),
    "get_review_payload": Tool(
        name="get_review_payload",
        description="获取批次当前挂起的待审核数据包（各 SKU 提取结果与错误信息）。"
                    "操作员问“要我审什么”“这个批次提取出了什么”时使用；"
                    "批次未挂起时返回 not_suspended 说明。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_get_review_payload,
    ),
    "explain_errors": Tool(
        name="explain_errors",
        description="用中文人话解释批次的提取错误/问题项及处理建议。"
                    "操作员问“为什么报错”“这些问题什么意思”“该怎么处理”时使用；"
                    "可用 factory 只看某个工厂。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "factory": {
                    "type": "string",
                    "description": "可选，只看指定工厂的错误；不传则覆盖全部工厂",
                },
            },
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_explain_errors,
    ),
    "get_usage": Tool(
        name="get_usage",
        description="查询全局 LLM token 用量摘要（调用次数/成败/token 数）。"
                    "操作员问“用了多少 token”“LLM 调用情况”时使用。"
                    "注意：进程内累计，重启清零，无批次维度。",
        parameters={"type": "object", "properties": {}},
        risk="read",
        func=_fn_get_usage,
    ),
    "ask_guide": Tool(
        name="ask_guide",
        description="操作指导问答：操作员问'怎么用'、'为什么'、'最佳实践'等流程/操作相关问题时调用。"
                    "知识库覆盖新手引导、改路径、改数审核、重跑、解释错误、常见问题。",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "操作员的问题原文"},
                "thread_id": {"type": "string", "description": "可选：当前批次号（提供时自动收集该批次上下文）"},
            },
            "required": ["question"],
        },
        risk="read",
        func=lambda args: _ask_guide_wrapper(args),  # 延迟 import guide.py
    ),

    # ---- 二期写工具（preview + execute，loop 拦截走确认门）----
    "create_batch": Tool(
        name="create_batch",
        description="创建新提取批次并开始跑图（Node1 到 Node5 挂起待审）。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "路径缺省时取 .env 配置。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "新批次的线程 ID，必填且不能与已有批次重名",
                },
                "downstream_file_path": {
                    "type": "string",
                    "description": "可选，下游装箱单 xlsx 绝对路径；缺省取配置",
                },
                "upstream_root": {
                    "type": "string",
                    "description": "可选，上游工厂文件夹根目录绝对路径；缺省取配置",
                },
            },
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_create_batch,
        execute=_exec_create_batch,
    ),
    "rerun": Tool(
        name="rerun",
        description="让挂起中的批次带新路径从 Node1 重跑（回到 Node5 挂起）。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "仅挂起批次可重跑；已完成批次会报错。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "upstream_root": {
                    "type": "string",
                    "description": "可选，新的上游工厂文件夹根目录绝对路径",
                },
                "downstream_file_path": {
                    "type": "string",
                    "description": "可选，新的下游装箱单 xlsx 绝对路径",
                },
            },
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_rerun,
        execute=_exec_rerun,
    ),
    "submit_review": Tool(
        name="submit_review",
        description="提交挂起批次的人工审核结果（批准/驳回 + 修正后的 items），"
                    "唤醒流程继续执行写 Excel 落库。"
                    "写操作：preview 会列出全部字段改动 old→new 供操作员核对，"
                    "确认后才执行。仅挂起待审核批次可用。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "approved": {
                    "type": "boolean",
                    "description": "true=批准落库；false=驳回",
                },
                "items": {
                    "type": "array",
                    "description": "审核后的条目数组，每项含 sku 与 extracted_data"
                                   "（可含人工修正的数值字段）；新 SKU 另含补录字段",
                    "items": {"type": "object"},
                },
            },
            "required": ["thread_id", "approved", "items"],
        },
        risk="write",
        preview=_preview_submit_review,
        execute=_exec_submit_review,
    ),
    "set_paths": Tool(
        name="set_paths",
        description="修改路径配置（白名单三项：upstream_root 上游工厂文件夹 / "
                    "downstream_file_path 下游装箱表 / gt_source GT 基准文件），"
                    "写入 .env 持久生效；携带 thread_id 时当前批次立即用新路径重跑。"
                    "写操作：preview 展示旧→新变更与硬错误/异平台警告，确认后才执行。",
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "object",
                    "description": "要修改的路径，key 仅限 upstream_root / "
                                   "downstream_file_path / gt_source，值为绝对路径",
                    "properties": {
                        "upstream_root": {"type": "string"},
                        "downstream_file_path": {"type": "string"},
                        "gt_source": {"type": "string"},
                    },
                },
                "thread_id": {
                    "type": "string",
                    "description": "可选；提供时确认后当前批次立即用新路径重跑",
                },
            },
            "required": ["paths"],
        },
        risk="write",
        preview=_preview_set_paths,
        execute=_exec_set_paths,
    ),
}


# ---------------------------------------------------------------------------
# 注册表查询接口
# ---------------------------------------------------------------------------

def visible_tools(phase: int = 1) -> list[Tool]:
    """按阶段返回可见工具：phase=1 只读工具；phase=2 全部（含写工具）。"""
    if phase >= 2:
        return list(TOOLS.values())
    return [t for t in TOOLS.values() if t.risk == "read"]


def openai_tool_defs(phase: int = 1) -> list[dict]:
    """OpenAI tools 格式定义（发给 LLM 的 tools 参数）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in visible_tools(phase)
    ]


# JSON Schema 类型 → Python 类型（boolean 必须先于 integer/number 检查，
# 因为 bool 是 int 的子类）
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate_args(args: dict, schema: dict) -> tuple[dict, str | None]:
    """手写轻量入参校验（不引 jsonschema 依赖）。

    - required 检查：缺必填参数报错；
    - 类型检查：按 schema properties 声明的 type 校验（类型不符报错）；
    - 剔除 schema 未声明的多余 key（LLM 幻觉参数不进入执行层）。
    返回 (清洗后的 args, 错误信息|None)。
    """
    if not isinstance(args, dict):
        return {}, f"参数必须是 object，收到 {type(args).__name__}"

    properties = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []

    for key in required:
        if key not in args or args[key] is None:
            return {}, f"缺少必填参数: {key}"

    cleaned: dict = {}
    for key, value in args.items():
        if key not in properties:
            continue  # 剔除未声明的多余 key
        if value is None:
            if key in required:
                return {}, f"缺少必填参数: {key}"
            continue  # 可选参数传 None 视为未传
        expected = (properties[key] or {}).get("type")
        check = _TYPE_CHECKS.get(expected)
        if check is not None and not check(value):
            return {}, (f"参数 {key} 类型错误：期望 {expected}，"
                        f"收到 {type(value).__name__}")
        cleaned[key] = value
    return cleaned, None


__all__ = ["Tool", "TOOLS", "visible_tools", "openai_tool_defs", "validate_args"]
