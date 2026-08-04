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
    execute: Callable | None = None   # 写工具：confirm 后执行 execute(args, on_progress=None) -> dict，内部二次校验


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _err(e: Exception) -> dict:
    """统一错误返回格式（绝不抛出，loop 会把 error 回喂 LLM）。"""
    return {"error": f"{type(e).__name__}: {e}"}


def _preview(summary: str, lines: list[str] | None = None,
             warnings: list[str] | None = None, **extra) -> dict:
    """写工具 preview 的统一返回结构；**extra 透传结构化附加字段
    （如 create_batch 的 factory_scan，由 loop 转交前端确认卡）。"""
    return {"summary": summary, "lines": lines or [],
            "warnings": warnings or [], **extra}


def _wrap_on_progress(tool_name: str, args: dict,
                      on_progress: Callable[[dict], None] | None):
    """把 service 的节点级进度事件包装成 exec_progress（W4a）：
    补 type/tool/thread_id 字段后透传给 loop 层回调。
    on_progress 为 None 时返回 None（service 侧零开销）。"""
    if on_progress is None:
        return None
    thread_id = args.get("thread_id")

    def emit(event: dict) -> None:
        on_progress({"type": "exec_progress", "tool": tool_name,
                     "thread_id": thread_id, **event})
    return emit


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
    """create_batch 预览：展开实际路径 + 查重预检 + 工厂名对照预扫（W5 三档）。

    轮1（无 alias_decisions）：预扫结果分「确定命中/低置信推荐/无候选」三档
    进 lines，结构化结果放 factory_scan 供确认卡渲染；
    轮2（带 alias_decisions）：先做 _validate_alias_decisions 硬校验，
    失败返回 blocked=True（loop 层转 clarify，不出确认卡）；
    校验通过后展示每条决定 [仅本次]/[永久保存]，
    永久保存且覆盖既有 alias key 时给覆盖警告。
    """
    try:
        thread_id = (args.get("thread_id") or "").strip()
        settings = get_settings()
        downstream = args.get("downstream_file_path") or settings.downstream_file_path
        upstream = args.get("upstream_root") or settings.upstream_root
        factory_filter = args.get("factory_filter")
        alias_decisions = args.get("alias_decisions") or []

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
        if factory_filter:
            lines.append(f"只处理指定工厂: {factory_filter}")
        if not d_path.is_file():
            warnings.append(f"下游装箱单路径不存在或不是文件: {downstream}")
        if not u_path.is_dir():
            warnings.append(f"上游工厂文件夹路径不存在或不是目录: {upstream}")

        # ---- W5 工厂名对照预扫（纯 CPU 零成本，一次问清）----
        scan = service.prescan_factory_aliases(
            str(d_path), str(u_path), factory_filter=factory_filter)
        resolved = scan.get("resolved") or {}
        candidates = scan.get("candidates") or {}
        unmatched = scan.get("unmatched") or []
        warnings.extend(scan.get("warnings") or [])

        if resolved:
            lines.append("工厂对照·确定命中（无需确认）:")
            for factory, hit in resolved.items():
                lines.append(f"  {factory} -> {hit['folder']}"
                             f"（置信度{hit['score']:.0f}%）")
        if candidates:
            lines.append("工厂对照·低置信推荐（需逐个确认）:")
            for factory, cands in candidates.items():
                cand_text = " / ".join(
                    f"{c['folder']}(置信度{c['score']:.0f}%)" for c in cands)
                lines.append(f"  {factory} -> 候选: {cand_text}")
        if unmatched:
            lines.append("工厂对照·无候选（需人工指定文件夹）:")
            for factory in unmatched:
                lines.append(f"  {factory}")

        # ---- W4b 重复处理预检（sessions 完成态 + 审核落库四档判定）----
        # 解析失败只进 warning 不阻断预览；重复工厂分行标注 + 汇总警告
        try:
            precheck = service.check_processed_factories(
                str(d_path),
                factory_names=list(factory_filter) if factory_filter else None)
        except ValueError as e:
            warnings.append(str(e))
        else:
            repeated = [f for f in precheck["factories"] if f["processed"]]
            for f in repeated:
                if f["level"] == "audited":
                    audit = f.get("last_audit") or {}
                    ts_text = f"（{audit['ts']}）" if audit.get("ts") else ""
                    lines.append(
                        f"[重复] 工厂 {f['factory']} 已于批次 "
                        f"{audit.get('thread_id')} 审核落库{ts_text}")
                else:
                    lines.append(
                        f"[重复] 工厂 {f['factory']} 已于 "
                        f"{f.get('session_updated_at') or '未知时间'} "
                        "提取完成")
            if repeated:
                warnings.append(
                    f"{len(repeated)} 个工厂已处理过，确认将重复提取；"
                    "如需跳过已处理工厂，请告知")
                if precheck["processed_count"] == precheck["total_count"] \
                        and precheck["total_count"] > 0:
                    warnings.append(
                        "全部工厂均已处理：跳过已处理工厂后将不会创建新批次")

        # ---- 轮2：用户已给出 alias_decisions，展示决定清单 ----
        if alias_decisions:
            # 先做硬校验（preview 期拦截坏工厂名/坏文件夹）：
            # 失败返回 blocked=True，loop 层直接转 clarify，不出确认卡
            _, _, err = _validate_alias_decisions(
                alias_decisions, downstream, upstream, factory_filter)
            if err is not None:
                warnings.append(err)
                return _preview("工厂对照校验未通过", lines, warnings,
                                factory_scan=scan, blocked=True)
            from app.factory_match import load_alias_map
            existing_alias = load_alias_map()
            lines.append("本次工厂对照决定:")
            for d in alias_decisions:
                if not isinstance(d, dict):
                    continue
                factory = d.get("factory", "")
                folder = d.get("folder", "")
                if d.get("save"):
                    lines.append(f"  [永久保存] {factory} -> {folder}")
                    old = existing_alias.get(factory)
                    if old is not None and old != folder:
                        warnings.append(
                            f"覆盖警告：永久对照「{factory}」已存在映射 "
                            f"-> {old}，确认后将被覆盖为 -> {folder}")
                else:
                    lines.append(f"  [仅本次] {factory} -> {folder}")

        summary = f"将创建新批次 {thread_id} 并开始提取"
        if candidates or unmatched:
            summary += (f"；工厂对照存疑 {len(candidates)} 家、无候选 "
                        f"{len(unmatched)} 家，请确认存疑工厂的对照关系后再发起")
        return _preview(summary, lines, warnings, factory_scan=scan)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _validate_alias_decisions(
    alias_decisions: list,
    downstream: str,
    upstream: str,
    factory_filter: list[str] | None,
) -> tuple[dict[str, str], dict[str, str], str | None]:
    """alias_decisions 二次校验（execute 侧，防注入/防幻觉参数）。

    校验规则：
    - factory 必须在装箱单工厂集合内（带 factory_filter 时还需在过滤集内）；
    - folder 拒绝路径分隔符与「..」，且必须是生效 upstream_root 下
      现存的一级子目录。
    返回 (overrides, to_save, 错误信息|None)：overrides 为全部决定的
    {factory: folder}；to_save 为其中 save=true 的子集。
    """
    from app.factory_match import validate_subfolder
    from app.nodes.parse_downstream import parse_requirements

    overrides: dict[str, str] = {}
    to_save: dict[str, str] = {}

    try:
        requirements, _ = parse_requirements(
            str(Path(downstream).expanduser()))
    except Exception as e:  # noqa: BLE001 校验失败不建批次
        return {}, {}, f"装箱单解析失败，无法校验工厂对照: {type(e).__name__}: {e}"
    valid_factories = set(requirements.keys())

    for d in alias_decisions:
        if not isinstance(d, dict):
            return {}, {}, f"alias_decisions 元素必须是对象: {d!r}"
        factory = d.get("factory")
        folder = d.get("folder")
        if not factory or not isinstance(factory, str):
            return {}, {}, f"alias_decisions 缺少有效的 factory 字段: {d!r}"
        if not folder or not isinstance(folder, str):
            return {}, {}, f"alias_decisions 缺少有效的 folder 字段: {d!r}"
        if factory not in valid_factories:
            return {}, {}, (f"工厂「{factory}」不在装箱单工厂集合内，"
                            "拒绝写入对照")
        if factory_filter and factory not in set(factory_filter):
            return {}, {}, (f"工厂「{factory}」不在 factory_filter 范围内，"
                            "拒绝写入对照")
        # folder 防注入校验与运行中 retry_factory 对照注入同源
        # （factory_match.validate_subfolder：拒绝分隔符/../非现存目录）
        try:
            validate_subfolder(upstream, folder)
        except ValueError as e:
            return {}, {}, f"{e}，拒绝写入对照"
        overrides[factory] = folder
        if d.get("save"):
            to_save[factory] = folder
    return overrides, to_save, None


def _exec_create_batch(args: dict,
                       on_progress: Callable[[dict], None] | None = None) -> dict:
    """create_batch 执行：alias_decisions 二次校验 → save=true 落盘永久对照
    → 全部决定转 overrides 透传 service.create_batch（service 再做路径校验）。
    任何校验失败返回 {"error": ...}，绝不建批次。

    skip_processed（W4b）：与 factory_filter 互斥，同传时 factory_filter
    优先（skip_processed 忽略并在结果注明）；service.create_batch 内
    实时重算差集，差集为空返回 skipped_all 不建批次。
    on_progress（W4a）：节点级进度回调，包装成 exec_progress 透传 service。"""
    try:
        settings = get_settings()
        downstream = args.get("downstream_file_path") or settings.downstream_file_path
        upstream = args.get("upstream_root") or settings.upstream_root
        factory_filter = args.get("factory_filter")
        skip_processed = bool(args.get("skip_processed"))
        alias_decisions = args.get("alias_decisions") or []

        overrides: dict[str, str] = {}
        if alias_decisions:
            overrides, to_save, err = _validate_alias_decisions(
                alias_decisions, downstream, upstream, factory_filter)
            if err:
                return {"error": err}
            if to_save:
                from app.factory_match import save_alias_entries
                # 永久对照先落盘（.bak 备份 + 原子写），再建批次
                save_result = save_alias_entries(to_save)

        result = service.create_batch(
            args["thread_id"],
            downstream_file_path=args.get("downstream_file_path"),
            upstream_root=args.get("upstream_root"),
            factory_filter=factory_filter,
            factory_alias_overrides=overrides or None,
            skip_processed=skip_processed,
            on_progress=_wrap_on_progress("create_batch", args, on_progress),
        )
        if skip_processed and factory_filter:
            result["note"] = ("factory_filter 与 skip_processed 同传，"
                              "factory_filter 优先，skip_processed 已忽略")
        if alias_decisions:
            result["alias_overrides_applied"] = overrides
            if to_save:
                result["alias_saved"] = save_result
        return result
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
        status_text = "待继续处理" if next_nodes else "已完成"
        lines = [f"批次 {thread_id} 当前状态: {status_text}"]
        warnings: list[str] = []
        if not next_nodes:
            warnings.append("该批次未处于挂起状态：仅挂起批次可重跑，确认后执行会报错")

        # 整批重跑醒目警告：与挂起状态警告相互独立，始终追加
        requirements = values.get("downstream_requirements") or {}
        if requirements:
            warnings.append(
                f"⚠️ 本操作是整批重跑：将从第 1 个工厂重新开始，"
                f"全部 {len(requirements)} 个工厂需重新提取并重新人工审核，"
                f"已审核结论作废。若只需重试当前识别失败的工厂，"
                f"请改用 retry_factory 工具。")
        else:
            warnings.append(
                "⚠️ 本操作是整批重跑：所有工厂需重新提取并重新人工审核，"
                "已审核结论作废。若只需重试当前识别失败的工厂，"
                "请改用 retry_factory 工具。")

        for key, label in (("upstream_root", "上游工厂文件夹"),
                           ("downstream_file_path", "下游装箱单")):
            old = values.get(key) or "（未设置，走 .env 缺省）"
            new = args.get(key)
            if new:
                marker = "不变" if old == new else "修改"
                lines.append(f"[{marker}] {label}:\n    {old}\n -> {new}")
            else:
                lines.append(f"[不变] {label}: {old}")

        return _preview(f"将带新路径重新运行批次 {thread_id} 的提取流程",
                        lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_rerun(args: dict,
                on_progress: Callable[[dict], None] | None = None) -> dict:
    """rerun 执行：service.rerun_with_paths 内部校验挂起状态，异常转 {"error": ...}。
    on_progress（W4a）：节点级进度回调，包装成 exec_progress 透传 service。"""
    try:
        return service.rerun_with_paths(
            args["thread_id"],
            upstream_root=args.get("upstream_root"),
            downstream_file_path=args.get("downstream_file_path"),
            on_progress=_wrap_on_progress("rerun", args, on_progress),
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _preview_retry_factory(args: dict) -> dict:
    """retry_factory 预览：取当前挂起 payload，展示将重试的工厂与 SKU 数；
    带 folder/save 时追加对照注入说明与永久对照覆盖警告。"""
    try:
        thread_id = args["thread_id"]
        payload = service.get_review_payload(thread_id)
        if payload is None:
            return _preview(
                "无法单厂重试", [],
                [f"批次 {thread_id} 当前未挂起待审核（不存在或已流转），"
                 "仅挂起批次可重试当前工厂"])

        factory = payload.get("factory_name") or "（未知工厂）"
        items = payload.get("items") or []
        lines = [f"批次 {thread_id} 当前挂起工厂: {factory}"
                 f"（{len(items)} 个 SKU）"]
        warnings: list[str] = []

        # 对照注入（W6b）：操作员告知的对应文件夹，预览展示注入内容
        folder = args.get("folder")
        if folder:
            lines.append(f"对照注入: {factory} -> {folder}")
            if bool(args.get("save")):
                lines.append("将永久保存对照到 alias_map.json")
                from app.factory_match import load_alias_map
                existing = load_alias_map().get(factory)
                if existing and existing != folder:
                    warnings.append(
                        f"现有永久对照 {factory}->{existing} 将被覆盖")

        return _preview(
            f"将重新提取批次 {thread_id} 当前挂起工厂「{factory}」的识别数据，"
            "已审核工厂不受影响",
            lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_retry_factory(args: dict,
                        on_progress: Callable[[dict], None] | None = None
                        ) -> dict:
    """retry_factory 执行：service.retry_factory_extraction 内部校验挂起状态，
    异常转 {"error": ...}。folder/save（W6b 对照注入）原样透传。
    on_progress（W4a）包装成 exec_progress 透传。"""
    try:
        return service.retry_factory_extraction(
            args["thread_id"],
            folder=args.get("folder"),
            save=bool(args.get("save")),
            on_progress=_wrap_on_progress("retry_factory", args, on_progress),
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# 内部字段名 → 用户可读中文名映射
_FIELD_LABEL: dict[str, str] = {
    "total_quantity": "总件数",
    "total_net_weight": "总净重",
    "total_gross_weight": "总毛重",
    "hs_code": "HS编码",
    "inspection_required": "是否需要商检",
    "name_cn": "中文品名",
    "name_en": "英文品名",
}


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
            raw_field = c.get("field", "")
            field_label = _FIELD_LABEL.get(raw_field, raw_field)
            lines.append(f"[改动] SKU {c.get('sku')} 字段 {field_label}: "
                         f"{c.get('old')} -> {c.get('new')}")
        for s in new_skus:
            inspection = "是" if s.get("inspection_required") else "否"
            lines.append(f"[新SKU] {s.get('sku')}: "
                         f"中文品名={s.get('name_cn')}, "
                         f"HS编码={s.get('hs_code')}, "
                         f"需要商检={inspection}")

        warnings: list[str] = []
        if not approved:
            warnings.append("审核结论为驳回：确认后该工厂数据不会落库")
        return _preview(
            f"将提交批次 {thread_id} 的审核结果（{'批准' if approved else '驳回'}，"
            f"改动 {edited_count} 个 SKU）", lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_submit_review(args: dict,
                        on_progress: Callable[[dict], None] | None = None) -> dict:
    """submit_review 执行：service.resume_order 内部二次校验挂起状态并落审计。
    on_progress（W4a）：节点级进度回调，包装成 exec_progress 透传 service。"""
    try:
        resume_data = {"approved": args["approved"], "items": args["items"]}
        return service.resume_order(
            args["thread_id"], resume_data,
            on_progress=_wrap_on_progress("submit_review", args, on_progress))
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


def _exec_set_paths(args: dict,
                    on_progress: Callable[[dict], None] | None = None) -> dict:
    """set_paths 执行：apply_paths 内含二次校验 + .env 备份 + 可选当前批次重跑。
    on_progress 形参为与 loop 透传对齐保留，本工具不使用（重跑耗时不经此回调）。"""
    try:
        from app import agent_chat

        return agent_chat.apply_paths(args["paths"],
                                      thread_id=args.get("thread_id"))
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------------------
# curate_kb 写工具（RAG 策展：队列排查 → 去重聚类 → 人工确认 → LLM 起草 → 入库）
# ---------------------------------------------------------------------------

_CURATE_PROMPT = r"""你是知识库策展助手。下面是多个操作员提出的相似问题（同一簇），
请为知识库起草一条候选条目。

## 簇的问题列表
{questions}

## 任务
根据问题类型（操作指引 guide / 错误案例 issue），输出对应的 JSON 结构：

如果是 **操作指引类（guide）**：
{{"category": "guide", "key": "英文蛇形关键词", "entry": {{"keywords": ["中文关键词1", "关键词2"], "title": "中文标题", "content": "中文回答内容", "priority": 5}}}}

如果是 **错误案例类（issue）**：
{{"category": "issue", "key": "英文蛇形 TYPE 名（建议 ISSUE_XXX 格式）", "entry": {{"title": "中文标题", "explain": "错误原因解释", "severity": "high|mid|low", "suggest": [{{"action": "英文 action 名", "label": "中文建议标签", "tool": null, "args_hint": {{}}}}]}}}}

## 铁律
1. key 必须是英文蛇形（如 how_to_check_sku），不能含中文或空格；
2. content/explain 必须基于簇中的实际问题，不得编造不存在的功能或场景；
3. 只输出 JSON，不要任何额外文字。"""


def _cluster_questions(items: list[dict]) -> list[dict]:
    """向量聚类：返回 [{questions, representative, suggested_category}]。"""
    from app.dispatcher import rag

    questions = [it["question"] for it in items]
    vecs = rag.embed_texts(questions)
    if vecs is None:
        # embed 不可用：每个问题单独成簇
        return [{"questions": [it["question"]], "representative": it["question"],
                 "suggested_category": "guide"} for it in items]

    # 简单贪心聚类：cosine > 0.75 归为一簇
    clusters: list[dict] = []
    assigned: set[int] = set()
    for i, it in enumerate(items):
        if i in assigned:
            continue
        assigned.add(i)
        cluster_qs = [it["question"]]
        for j in range(i + 1, len(items)):
            if j in assigned:
                continue
            score = _cosine_sim(vecs[i], vecs[j])
            if score > 0.75:
                cluster_qs.append(items[j]["question"])
                assigned.add(j)
        # 来源最多的类别作为建议类别
        cluster_sources = [it["source"] for it in items
                          if it["question"] in cluster_qs]
        cat = "issue" if cluster_sources.count("issue") > cluster_sources.count("guide") \
              else "guide"
        clusters.append({
            "questions": cluster_qs,
            "representative": cluster_qs[0],
            "suggested_category": cat,
        })
    return clusters


def _cosine_sim(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _preview_curate_kb(args: dict) -> dict:
    """curate_kb 预览：读队列 → 聚类 → 查现有 KB 去重 → 结构化预览。"""
    try:
        from app.dispatcher import rag

        max_items = int(args.get("max_items", 50))
        items = rag.read_curation_queue(max_items)
        if not items:
            return _preview("待策展队列为空，无需排查", [],
                           ["目前没有待策展的未命中问题"])

        clusters = _cluster_questions(items)
        lines: list[str] = []
        warnings: list[str] = []

        for idx, cl in enumerate(clusters):
            rep = cl["representative"]
            count = len(cl["questions"])
            cat = cl["suggested_category"]
            lines.append(f"[簇 {idx}] 出现 {count} 次 | 类别: {cat} | 代表: {rep}")

            # 查重：代表问题在现有 KB 两个 namespace 中检索
            guide_hits = rag.query_namespace("guide", rep, top_k=1, min_score=0.7)
            issue_hits = rag.query_namespace("issue", rep, top_k=1, min_score=0.7)
            near = guide_hits or issue_hits
            if near:
                hit = near[0]
                lines.append(f"  ⚠ 已有近似条目: {hit['id']}（score={hit['score']:.2f}，建议跳过）")
            else:
                lines.append(f"  ✅ 无近似条目，建议新增 {cat} 条目")
                if cat == "guide":
                    lines.append("  📝 LLM 将起草: title/keywords/content")
                else:
                    lines.append("  📝 LLM 将起草: title/explain/severity/suggest")

        total = len(items)
        summary = (f"待策展队列共 {total} 条，去重后 {len(clusters)} 个问题簇"
                   if total > 0 else "待策展队列为空")
        return _preview(summary, lines, warnings)
    except Exception as e:  # noqa: BLE001
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_curate_kb(args: dict,
                    on_progress: Callable[[dict], None] | None = None) -> dict:
    """curate_kb 执行：LLM 起草 → 写入 kb_extension.json → 灌库 → 清队列。
    on_progress 形参为与 loop 透传对齐保留，本工具不使用。"""
    try:
        from app.dispatcher import rag
        from app.extraction import llm_client

        confirmed = args.get("confirmed_clusters") or []
        if not isinstance(confirmed, list) or not confirmed:
            return {"error": "confirmed_clusters 为空，未执行任何操作"}

        max_items = int(args.get("max_items", 50))
        items = rag.read_curation_queue(max_items)
        if not items:
            return {"error": "待策展队列为空"}

        clusters = _cluster_questions(items)
        drafted: dict[str, dict] = {"guide": {}, "issue": {}}
        removed_questions: set[str] = set()

        for ci in confirmed:
            if not isinstance(ci, int) or ci < 0 or ci >= len(clusters):
                continue
            cl = clusters[ci]
            cat = cl["suggested_category"]
            qs_text = "\n".join(f"- {q}" for q in cl["questions"])
            prompt = _CURATE_PROMPT.replace("{questions}", qs_text)

            try:
                raw = llm_client.chat_completion(
                    [{"role": "user", "content": prompt}],
                    json_mode=True, source_file="curate_kb", max_tokens=1024,
                )
                draft = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"LLM 起草簇 {ci} 失败: {exc}"}

            if not isinstance(draft, dict):
                return {"error": f"簇 {ci} LLM 输出不是 JSON 对象"}

            d_cat = draft.get("category", cat)
            d_key = draft.get("key", "")
            d_entry = draft.get("entry", {})
            if not d_key or not isinstance(d_entry, dict):
                return {"error": f"簇 {ci} 缺少 key 或 entry"}

            drafted.setdefault(d_cat, {})[d_key] = d_entry
            removed_questions.update(cl["questions"])

        if not drafted["guide"] and not drafted["issue"]:
            return {"error": "没有可入库的条目"}

        ok = rag.save_extension(drafted)
        if not ok:
            return {"error": "写入 kb_extension.json 失败"}

        # 灌库
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sync_kb", str(Path(__file__).resolve().parents[3] / "scripts" / "sync_kb.py"),
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()

        # 清理队列
        rag.remove_from_queue(removed_questions)

        guide_count = len(drafted.get("guide", {}))
        issue_count = len(drafted.get("issue", {}))
        return {
            "status": "ok",
            "message": (f"已新增 guide {guide_count} 条、issue {issue_count} 条，"
                       f"已灌库并清理队列中 {len(removed_questions)} 个问题"),
            "drafted": drafted,
        }
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
                    "路径缺省时取 .env 配置。preview 会自动预扫工厂名对照"
                    "（确定命中/低置信推荐/无候选三档）；有存疑工厂时先向"
                    "操作员问清每个工厂用哪个文件夹、是否保存永久对照，"
                    "再带 alias_decisions 重新调用本工具。",
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
                "factory_filter": {
                    "type": "array",
                    "description": "可选，只处理指定工厂（装箱单工厂名列表）；"
                                   "不传=全部工厂",
                    "items": {"type": "string"},
                },
                "skip_processed": {
                    "type": "boolean",
                    "description": "可选，true 时自动跳过已处理过的工厂（已提取"
                                   "完成或已审核落库），只跑未处理工厂；与 "
                                   "factory_filter 互斥，同传时 factory_filter "
                                   "优先。全部工厂均已处理时不创建批次，返回 "
                                   "skipped_all",
                },
                "alias_decisions": {
                    "type": "array",
                    "description": "可选，工厂名对照决定清单（预扫存疑时第二轮"
                                   "调用携带）。每项：factory=装箱单工厂名、"
                                   "folder=上游一级文件夹名、save=true 保存永久"
                                   "对照（追加 alias_map.json，后续批次生效）/"
                                   "false 仅本次生效（不落盘）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "factory": {"type": "string"},
                            "folder": {"type": "string"},
                            "save": {"type": "boolean"},
                        },
                        "required": ["factory", "folder"],
                    },
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
        description="整批重跑（所有工厂重新提取+重新审核）。"
                    "让挂起中的批次带新路径从 Node1 重跑（回到 Node5 挂起）。"
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
    "retry_factory": Tool(
        name="retry_factory",
        description="重试当前挂起工厂的提取识别（只重跑这一个工厂，从提取节点"
                    "重新执行到重新挂起审核，已审核工厂不受影响）。"
                    "适用场景：当前工厂识别失败/超时/生成了人工补录占位数据，"
                    "操作员要求重试该工厂。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "仅挂起待审核批次可用；未挂起批次会报错。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "folder": {
                    "type": "string",
                    "description": "可选，操作员告知的对应上游文件夹名"
                                   "（upstream_root 下一级子目录名，不是完整路径）。"
                                   "用于工厂未匹配到文件夹（no_folder_matched）时"
                                   "注入对照重新提取",
                },
                "save": {
                    "type": "boolean",
                    "description": "可选，默认 false。true=把 工厂→folder 对照"
                                   "永久保存到 alias_map.json（后续批次自动生效）；"
                                   "false=仅本次批次生效",
                },
            },
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_retry_factory,
        execute=_exec_retry_factory,
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
    "curate_kb": Tool(
        name="curate_kb",
        description="排查待策展队列（操作员未命中的问题），去重聚类后展示候选问题簇，"
                    "经操作员确认后由 LLM 起草知识条目并写入扩展知识库。"
                    "写操作：preview 展示去重后的簇与查重结果，操作员确认 confirmed_clusters "
                    "后才执行起草+入库+灌库+清队列。",
        parameters={
            "type": "object",
            "properties": {
                "max_items": {
                    "type": "integer",
                    "description": "最多排查的队列条目数，默认 50",
                },
                "confirmed_clusters": {
                    "type": "array",
                    "description": "操作员确认要入库的簇索引（0-based，空数组跳过入库）",
                    "items": {"type": "integer"},
                },
            },
        },
        risk="write",
        preview=_preview_curate_kb,
        execute=_exec_curate_kb,
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
