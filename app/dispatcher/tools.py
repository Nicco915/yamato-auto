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
    risk: str                   # "read" | "write" | "ui"
    func: Callable | None = None      # 只读工具执行函数：func(args: dict) -> dict
    preview: Callable | None = None   # 写工具：preview(args) -> {"summary", "lines", "warnings"}
    execute: Callable | None = None   # 写工具：confirm 后执行 execute(args, on_progress=None) -> dict，内部二次校验
    # risk="ui" 工具无 func/preview/execute，由 lc_tools 特殊处理


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _err(e: Exception) -> dict:
    """统一错误返回格式（绝不抛出，loop 会把 error 回喂 LLM）。"""
    return {"error": f"{type(e).__name__}: {e}"}


def _pinned_scope_warning(args: dict, session_id: str | None) -> str | None:
    """Pinned scope 检查（写工具前置防御）。

    当前会话已 pin 某个批次（chat_sessions.pinned_thread_id != None）时，
    若写工具的 thread_id 参数与 pinned_thread_id 不一致，返回一句自然
    语言警告；其他场景（无 pinned / 未传 thread_id / thread_id 一致）
    返回 None。

    设计要点：
    - 仅以 chat_sessions.pinned_thread_id 为唯一权威源（DB 表已存在，
      session_id 提供时实时查，避免 DispatcherSession 多承载一份缓存
      与 DB 失同步）；
    - 仅在 args 实际含 thread_id 时检查（set_paths 不带 thread_id、
      start_split 只校验上游路径——这些场景无 thread_id，沉默放行）；
    - session_id=None 时直接返回 None（保持向后兼容：单元测试与未启用
      pinned 的旧会话一致行为）；
    - DB 异常绝不抛出——降级返回 None（铁律：scope 警告是防御性的，不能
      阻塞确认门主流程）。
    """
    if not session_id:
        return None
    target_tid = args.get("thread_id")
    if not target_tid or not isinstance(target_tid, str):
        return None
    try:
        from app.db.models import ChatSession as _ChatSessionOrm
        from app.db.session import get_session as _get_db_session
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            pinned = row.pinned_thread_id if row else None
        if not pinned:
            return None
        if pinned == target_tid:
            return None
        return (f"当前会话已 pin 批次「{pinned}」，本次操作目标是"
                f"「{target_tid}」，确认仍要执行吗？")
    except Exception:  # noqa: BLE001 DB 异常降级放行
        return None


def _merge_pinned_warning(args: dict, session_id: str | None,
                          warnings: list[str]) -> list[str]:
    """把 _pinned_scope_warning 合并进 warnings 列表（带哨兵去重）。

    调用方只关心「返回新 list」，不重复 import。"""
    msg = _pinned_scope_warning(args, session_id)
    if msg and msg not in warnings:
        return warnings + [msg]
    return warnings


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
    """单批次轻量摘要：list_batches 摘要 + next_nodes / validation_status
    + unprocessed_factories（被跳过/驳回/未写入的工厂名单，供 Agent 发现
    可补入对象——list_batches 摘要层原本看不到）。"""
    try:
        thread_id = args["thread_id"]
        batches = service.list_batches().get("batches", [])
        summary = next((b for b in batches if b.get("thread_id") == thread_id), None)
        if summary is None:
            return {"error": f"批次不存在: {thread_id}"}
        state = service.get_order_state(thread_id)
        values = state.get("values") or {}
        # 未处理工厂 = 装箱单要求（有 filter 取交集）− 待处理队列 − 当前工厂
        # − 已写入（factory_outputs 只有 approve 的工厂才进）
        req = set((values.get("downstream_requirements") or {}).keys())
        factory_filter = values.get("factory_filter")
        if factory_filter:
            req &= set(factory_filter)
        pending = set(values.get("pending_factories") or [])
        current = (values.get("current_factory_data") or {}).get("factory_name")
        done = set((values.get("factory_outputs") or {}).keys())
        unprocessed = sorted(req - pending - {current} - done)
        return {
            **summary,
            "next_nodes": state.get("next_nodes"),
            "validation_status": values.get("validation_status"),
            "unprocessed_factories": unprocessed,
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


def _fn_list_directory(args: dict) -> dict:
    """列出目录内容，供 Agent 引导用户选择路径。

    参数：
    - path: 目录路径（可选，默认 home 目录）
    - type: "file" 或 "dir"（选择类型，用于过滤显示）
    - extensions: 文件扩展名过滤（如 "xlsx,xls"，逗号分隔）

    返回：
    {
        "current_path": "/absolute/path",
        "parent_path": "/parent/path" | None,
        "entries": [
            {"name": "folder", "path": "/full/path", "is_dir": true},
            {"name": "file.xlsx", "path": "/full/path", "is_dir": false, "size": 12345},
            ...
        ],
        "drives": ["C:\\", "D:\\"] | null  # Windows 盘符列表
    }
    """
    import os
    import string as _string

    path = args.get("path")
    type_filter = args.get("type", "dir")
    extensions = args.get("extensions")

    # 确定起始路径
    if path is None:
        if os.name == "nt":  # Windows
            drives = []
            for letter in _string.ascii_uppercase:
                drive_path = Path(f"{letter}:\\")
                if drive_path.exists():
                    drives.append(f"{letter}:\\")
            return {
                "current_path": None,
                "parent_path": None,
                "entries": [],
                "drives": drives,
                "message": "Windows 系统，请选择盘符",
            }
        else:
            browse_path = Path.home()
    else:
        browse_path = Path(path).expanduser()

    # 验证路径
    if not browse_path.exists():
        return {"error": f"路径不存在: {path}"}
    if not browse_path.is_dir():
        return {"error": f"不是目录: {path}"}

    # 解析扩展名
    allowed_ext = None
    if extensions:
        allowed_ext = {ext.strip().lower().lstrip(".") for ext in extensions.split(",")}

    # 列出内容
    entries = []
    try:
        for item in sorted(browse_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith("."):
                continue

            is_dir = item.is_dir()

            if type_filter == "file" and not is_dir and allowed_ext:
                if item.suffix.lower().lstrip(".") not in allowed_ext:
                    continue

            entry = {"name": item.name, "path": str(item), "is_dir": is_dir}
            if not is_dir:
                try:
                    entry["size"] = item.stat().st_size
                except OSError:
                    pass
            entries.append(entry)
    except PermissionError:
        return {"error": f"无权限访问: {path}"}

    parent_path = str(browse_path.parent) if browse_path.parent != browse_path else None

    return {
        "current_path": str(browse_path),
        "parent_path": parent_path,
        "entries": entries[:100],  # 限制返回数量，避免过多
        "drives": None,
        "total": len(entries),
    }


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

def _preview_create_batch(args: dict, session_id: str | None = None) -> dict:
    """create_batch 预览：展开实际路径 + 查重预检 + 工厂名对照预扫（W5 三档）。

    轮1（无 alias_decisions）：预扫结果分「确定命中/低置信推荐/无候选」三档
    进 lines，结构化结果放 factory_scan 供确认卡渲染；
    轮2（带 alias_decisions）：先做 _validate_alias_decisions 硬校验，
    失败返回 blocked=True（loop 层转 clarify，不出确认卡）；
    校验通过：被决定的工厂从 candidates/unmatched 移除并注入 resolved
    （确认卡与摘要不再把已决工厂显示为存疑），
    同时展示每条决定 [仅本次]/[永久保存]，
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
        # Pinned scope 防御：会话已 pin 别的批次时给一句警告
        warnings = _merge_pinned_warning(args, session_id, warnings)

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

        # ---- 轮2：alias_decisions 硬校验 + 决定合并进预扫三档 ----
        # 必须早于三档 lines/摘要渲染：被决定的工厂从 candidates/unmatched
        # 移除并注入 resolved（method 标注 永久对照/本次决定），否则确认卡
        # 与摘要会把已决工厂继续显示为存疑，给人"决定没生效"的错觉
        overrides: dict[str, str] = {}
        to_save: dict[str, str] = {}
        if alias_decisions:
            overrides, to_save, err = _validate_alias_decisions(
                alias_decisions, downstream, upstream, factory_filter)
            if err is not None:
                warnings.append(err)
                return _preview("工厂对照校验未通过", lines, warnings,
                                factory_scan=scan, blocked=True)
            for factory, folder in overrides.items():
                candidates.pop(factory, None)
                if factory in unmatched:
                    unmatched.remove(factory)
                resolved[factory] = {
                    "folder": folder,
                    "score": 100.0,
                    "method": "永久对照" if factory in to_save else "本次决定",
                }
            scan["resolved"] = resolved
            scan["candidates"] = candidates
            scan["unmatched"] = unmatched

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
        # 解析失败只进 warning 不阻断预览；重复工厂分行标注 + 汇总警告；
        # 每批次独立：限查本批次 thread_id，跨批次审核不再回看
        try:
            precheck = service.check_processed_factories(
                thread_id,
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

        # ---- 轮2：展示决定清单（硬校验与预扫合并已在三档渲染前完成，
        # overrides/to_save 直接复用，不再重复校验）----
        if alias_decisions:
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


def _preview_rerun(args: dict, session_id: str | None = None) -> dict:
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
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)
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


def _preview_retry_factory(args: dict, session_id: str | None = None) -> dict:
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
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)

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


def _preview_force_extract_file(args: dict, session_id: str | None = None) -> dict:
    """force_extract_file 预览：展示指定文件、页码、识别方式。

    校验失败（路径穿越/页码越界/session 缺失/批次未挂起）由 service 层抛
    ValueError，preview 兜底转清晰错误展示，不走确认门。
    """
    try:
        thread_id = args.get("thread_id")
        file_path = args.get("file_path")
        if not thread_id or not file_path:
            return _preview(
                "无法指定文件提取", [],
                ["必须同时提供 thread_id 与 file_path"])

        payload = service.get_review_payload(thread_id)
        if payload is None:
            return _preview(
                "无法指定文件提取", [],
                [f"批次 {thread_id} 当前未挂起待审核（不存在或已流转），"
                 "仅挂起批次可指定文件"])

        factory = payload.get("factory_name") or "（未知工厂）"
        items = payload.get("items") or []

        pages = args.get("pages")
        force_vision = bool(args.get("force_vision"))
        page_summary = ""
        if pages:
            page_summary = f"的第 {','.join(str(p) for p in pages)} 页"
        vision_note = "视觉大模型（看图识别）" if (pages or force_vision) else "自动路由（有文本层走文本/无文本层走视觉）"

        file_name = Path(file_path).name
        lines = [
            f"批次 {thread_id} 当前挂起工厂: {factory}（现有 {len(items)} 个 SKU）",
            f"指定文件: {file_name}",
        ]
        if pages:
            lines.append(f"指定页码: 第 {','.join(str(p) for p in pages)} 页（共 {len(pages)} 页）")
        lines.append(f"识别方式: {vision_note}")

        warnings = _merge_pinned_warning(args, session_id, [
            f"提取结果会并入当前工厂「{factory}」已有数据；同一个 SKU 数值不同时以本次为准，"
            "旧值会记入改单历史并标记需人工确认。",
        ])

        return _preview(
            f"将用「{file_name}」{page_summary}重新提取批次 {thread_id} 当前工厂「{factory}」的数据",
            lines, warnings,
        )
    except Exception as e:
        return _preview(
            "预览生成失败", [],
            [f"{type(e).__name__}: {e}"])


def _exec_force_extract_file(args: dict,
                             on_progress: Callable[[dict], None] | None = None
                             ) -> dict:
    """force_extract_file 执行：service.force_extract_file 内部校验挂起状态、
    路径白名单、页码 sanity，异常转 {"error": ...}。"""
    try:
        return service.force_extract_file(
            args["thread_id"],
            file_path=args["file_path"],
            pages=args.get("pages"),
            force_vision=bool(args.get("force_vision")),
            on_progress=_wrap_on_progress("force_extract_file", args, on_progress),
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


def _preview_submit_review(args: dict, session_id: str | None = None) -> dict:
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
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)
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


def _preview_set_paths(args: dict, session_id: str | None = None) -> dict:
    """set_paths 预览：硬错误列顶 + 旧→新变更预览 + 异平台警告。"""
    try:
        from app import agent_chat

        paths = args["paths"]
        errors = agent_chat.validate_paths(paths)
        lines = [f"[硬错误] {e}" for e in errors]
        lines += agent_chat.preview_changes(paths)
        warnings = agent_chat.cross_platform_warnings(paths)
        # Pinned scope 防御：set_paths 可选带 thread_id（带时与 pinned 比对）
        warnings = _merge_pinned_warning(args, session_id, warnings)
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


def _preview_curate_kb(args: dict, session_id: str | None = None) -> dict:
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
        # Pinned scope 防御（curate_kb 不带 thread_id，常规下沉默放行）
        warnings = _merge_pinned_warning(args, session_id, warnings)

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
# 分票工具辅助
# ---------------------------------------------------------------------------

def _proposal_summary(thread_id: str, proposal: dict) -> str:
    """从 proposal dict 生成自然语言摘要（调度 Agent 基础原则 2：纯文字）。"""
    ports = proposal.get("ports", [])
    total_tickets = sum(len(pg.get("groups", [])) for pg in ports)

    if total_tickets == 0:
        return f"批次 {thread_id} 分票方案暂无票"

    status = proposal.get("status", "pending_review")
    status_text = "已确认" if status == "confirmed" else "待审核"

    header = f"批次 {thread_id} {status_text}分票，共 {total_tickets} 票，{len(ports)} 个港口"
    port_parts = []
    for pg in ports:
        port = pg.get("port", "?")
        tickets = pg.get("groups", [])
        sj_count = sum(
            1 for t in tickets
            if t.get("sj_factories") and (
                isinstance(t["sj_factories"], list) and len(t["sj_factories"]) > 0
            )
        )
        base = f"{port} {len(tickets)} 票"
        if sj_count:
            base += f"（含 {sj_count} 票商检）"
        port_parts.append(base)
    return f"{header}：{'、'.join(port_parts)}"


def _reconstruct_proposal_from_declarations(
    split_thread_id: str, decls: list,
) -> dict:
    """从 Declaration 表记录重构 proposal 格式。"""
    from collections import OrderedDict

    port_groups: dict[str, list[dict]] = OrderedDict()
    for d in decls:
        if d.port not in port_groups:
            port_groups[d.port] = []
        port_groups[d.port].append({
            "ticket_no": d.ticket_no,
            "port": d.port,
            "container_type": d.container_type,
            "items": d.items or [],
            "sj_factories": d.sj_factories or [],
            "warnings": d.warnings or [],
        })

    return {
        "split_thread_id": split_thread_id,
        "status": "confirmed",
        "ports": [
            {"port": port, "groups": tickets}
            for port, tickets in port_groups.items()
        ],
    }


# ---------------------------------------------------------------------------
# 分票只读工具（risk="read"，func 直接查询）
# ---------------------------------------------------------------------------

def _fn_get_split_proposal(args: dict) -> dict:
    """查看当前推荐/已确认分票方案。返回自然语言摘要。"""
    try:
        thread_id = args["thread_id"]
        split_thread_id = f"split-{thread_id}"

        # 1) 尝试从 split graph state 读
        from app.split.graph import get_split_graph
        graph = get_split_graph()
        config = {"configurable": {"thread_id": split_thread_id}}
        snap = graph.get_state(config)

        if snap.values:
            status = snap.values.get("status", "")
            proposal = snap.values.get("proposal")

            if status in ("confirmed", "pending_review") and proposal:
                msg = _proposal_summary(thread_id, proposal)
                return {"status": status, "message": msg}

            # 其他状态（loading、completed 等）
            if proposal:
                msg = _proposal_summary(thread_id, proposal)
                return {"status": status or "unknown", "message": msg}
            return {"status": status or "unknown",
                    "message": f"批次 {thread_id} 分票状态：{status}（方案尚未生成）"}

        # 2) 尝试从 Declaration 表读已确认的
        from app.db.models import Declaration
        from app.db.session import get_session
        with get_session() as sess:
            decls = sess.query(Declaration).filter(
                Declaration.split_thread_id == split_thread_id,
                Declaration.status == "confirmed",
            ).order_by(Declaration.port, Declaration.ticket_no).all()
            if decls:
                proposal = _reconstruct_proposal_from_declarations(
                    split_thread_id, decls)
                msg = _proposal_summary(thread_id, proposal)
                return {"status": "confirmed", "message": msg}

        # 3) 兜底
        return {"status": "not_started",
                "message": "该批次尚未启动分票"}
    except Exception as e:
        return _err(e)


def _fn_list_declarations(args: dict) -> dict:
    """列出某批次的所有票，按港口+票号排序。返回自然语言列表。"""
    try:
        thread_id = args["thread_id"]
        split_thread_id = f"split-{thread_id}"

        from app.db.models import Declaration
        from app.db.session import get_session
        with get_session() as sess:
            decls = sess.query(Declaration).filter(
                Declaration.split_thread_id == split_thread_id,
                Declaration.status == "confirmed",
            ).order_by(Declaration.port, Declaration.ticket_no).all()

        if not decls:
            return {"status": "empty",
                    "message": f"批次 {thread_id} 暂无已确认的票"}

        lines = []
        for d in decls:
            sj_factories = d.sj_factories or []
            if sj_factories:
                factory_names = [
                    f.get("factory_name", "")
                    for f in sj_factories
                    if isinstance(f, dict) and f.get("factory_name")
                ]
                if factory_names:
                    sj_text = f" 含商检({', '.join(factory_names)})"
                elif any(
                    isinstance(f, str) and f
                    for f in sj_factories
                ):
                    sj_text = f" 含商检({', '.join(f for f in sj_factories if isinstance(f, str) and f)})"
                else:
                    sj_text = " 含商检"
            else:
                sj_text = " 普通"

            items = d.items or []
            full_count = sum(
                1 for it in items
                if isinstance(it, dict) and not it.get("is_partial")
            )
            container_desc = f"{full_count} 柜" if full_count else ""
            if not container_desc and items:
                container_desc = f"{len(items)} 柜"

            lines.append(
                f"「{d.ticket_no}」{d.container_type} {container_desc}{sj_text}"
            )

        if len(lines) > 50:
            port_summary: dict[str, dict[str, int]] = {}
            for d in decls:
                port = d.port
                if port not in port_summary:
                    port_summary[port] = {"count": 0, "sj_count": 0}
                port_summary[port]["count"] += 1
                if d.sj_factories:
                    port_summary[port]["sj_count"] += 1

            summary_lines = [f"共 {len(decls)} 票，以下为港口汇总："]
            for port, stats in sorted(port_summary.items()):
                parts = [f"{port} {stats['count']} 票"]
                if stats["sj_count"]:
                    parts.append(f"含 {stats['sj_count']} 票商检")
                summary_lines.append("  " + "，".join(parts))
            return {"status": "ok", "message": "\n".join(summary_lines),
                    "total": len(decls)}

        return {"status": "ok", "message": "\n".join(lines),
                "total": len(decls)}
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 分票写工具（risk="write"，preview + execute，execute 内二次校验）
# ---------------------------------------------------------------------------

def _preview_start_split(args: dict, session_id: str | None = None) -> dict:
    """start_split 预览：校验上游批次 + 源文件 + 判定是否已存在分票。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        source_file_path = args.get("source_file_path")

        lines = [f"上游批次: {thread_id}"]
        warnings: list[str] = []
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)

        if not thread_id:
            warnings.append("thread_id 为空，确认后执行会失败")
        else:
            state = service.get_order_state(thread_id)
            if not state.get("exists"):
                warnings.append(f"上游批次不存在: {thread_id}")
            elif not source_file_path:
                values = state.get("values") or {}
                source_file_path = values.get("final_output_path")
                if source_file_path:
                    lines.append(f"源文件（从批次状态获取）: {source_file_path}")
                else:
                    warnings.append("无法获取批次输出文件路径，请手动提供 source_file_path")

        if source_file_path:
            p = Path(source_file_path).expanduser()
            if not p.is_file():
                warnings.append(f"源文件不存在: {source_file_path}")
            else:
                lines.append(f"源文件: {source_file_path}")

        # 查是否已有分票记录
        split_thread_id = f"split-{thread_id}"
        try:
            from app.split.graph import get_split_graph
            graph = get_split_graph()
            snap = graph.get_state(
                {"configurable": {"thread_id": split_thread_id}})
            if snap.values:
                status = snap.values.get("status", "")
                warnings.append(
                    f"该批次已有分票记录（状态：{status}），确认后将覆盖")
        except Exception:
            pass  # split graph 未初始化时不阻塞

        summary = f"确认启动批次 {thread_id} 的分票？"
        return _preview(summary, lines, warnings)
    except Exception as e:
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_start_split(
    args: dict,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """start_split 执行：获取源文件 → 启动分票图 → 跑到 human_review 挂起。"""
    try:
        thread_id = args["thread_id"].strip()
        if not thread_id:
            return {"error": "thread_id 不能为空"}

        source_file_path = args.get("source_file_path")
        if not source_file_path:
            state = service.get_order_state(thread_id)
            if not state.get("exists"):
                return {"error": f"上游批次不存在: {thread_id}"}
            values = state.get("values") or {}
            source_file_path = values.get("final_output_path")
            if not source_file_path:
                return {"error": "无法获取批次输出文件路径，请手动提供 source_file_path"}

        p = Path(source_file_path).expanduser()
        if not p.is_file():
            return {"error": f"源文件不存在: {source_file_path}"}

        split_thread_id = f"split-{thread_id}"

        from app.split.graph import get_split_graph
        graph = get_split_graph()
        config = {"configurable": {"thread_id": split_thread_id}}

        initial = {
            "split_thread_id": split_thread_id,
            "source_file_path": str(p),
        }

        # 跑图直到 interrupt（human_review 挂起）
        for event in graph.stream(initial, config, stream_mode="updates"):
            if "__interrupt__" in event:
                final_snap = graph.get_state(config)
                proposal = final_snap.values.get("proposal", {})
                total_tickets = sum(
                    len(pg.get("groups", []))
                    for pg in proposal.get("ports", [])
                )
                return {
                    "status": "pending_review",
                    "split_thread_id": split_thread_id,
                    "message": (
                        f"已启动分票，split ID: split-{thread_id}，"
                        f"共 {total_tickets} 票，正在等待人工审核。"
                        f"建议打开 /split/split-{thread_id} 页面查看。"
                    ),
                }

        final_snap = graph.get_state(config)
        return {
            "status": final_snap.values.get("status", "completed"),
            "split_thread_id": split_thread_id,
            "message": f"分票已完成: split-{thread_id}",
        }
    except Exception as e:
        return _err(e)


def _preview_confirm_split(args: dict, session_id: str | None = None) -> dict:
    """confirm_split 预览：统计警告，force 模式判定。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        force = bool(args.get("force", False))
        split_thread_id = f"split-{thread_id}"

        if not thread_id:
            return _preview("无法确认分票", [], ["thread_id 为空"])

        # Pinned scope 防御
        pinned_msg = _pinned_scope_warning(args, session_id)

        from app.split.graph import get_split_graph
        graph = get_split_graph()
        snap = graph.get_state(
            {"configurable": {"thread_id": split_thread_id}})

        if not snap.values:
            return _preview(
                "无法确认分票", [],
                [f"分票批次不存在: split-{thread_id}"])

        if not snap.next:
            return _preview(
                "无法确认分票", [],
                ["该分票批次未处于等待审核状态，或已完成"])

        proposal = snap.values.get("proposal", {})
        if not proposal:
            return _preview("无法确认分票", [], ["未找到分票方案"])

        # 收集所有警告
        all_warnings: list[str] = []
        total_tickets = 0
        for pg in proposal.get("ports", []):
            for ticket in pg.get("groups", []):
                total_tickets += 1
                for w in ticket.get("warnings", []):
                    if isinstance(w, dict):
                        all_warnings.append(
                            f"{ticket.get('ticket_no', '?')}: "
                            f"{w.get('message', '')}"
                        )
                    elif isinstance(w, str):
                        all_warnings.append(
                            f"{ticket.get('ticket_no', '?')}: {w}"
                        )

        if all_warnings and not force:
            brief = all_warnings[:3]
            if len(all_warnings) > 3:
                brief.append(f"...等共 {len(all_warnings)} 个警告")
            return _preview(
                f"该方案存在 {len(all_warnings)} 个警告：{brief[0] if brief else ''}。"
                f"是否强制通过？如确认将被标为'强制通过'。",
                [f"批次 {thread_id} 分票方案共 {total_tickets} 票"]
                + brief,
                [w[:200] for w in all_warnings],
            )

        lines = [f"批次 {thread_id} 分票方案共 {total_tickets} 票"]
        if force and all_warnings:
            lines.append(f"将强制通过（忽略 {len(all_warnings)} 个警告）")
        if all_warnings and not force:
            lines.append(f"方案无警告，将正常确认")
        for w in all_warnings[:5]:
            lines.append(f"  [警告] {w[:120]}")
        if len(all_warnings) > 5:
            lines.append(f"  ...等共 {len(all_warnings)} 个警告")

        summary = f"确认批次 {thread_id} 分票方案（共 {total_tickets} 票）？"
        if force:
            summary += "（强制通过模式）"
        warnings_out = [w[:200] for w in all_warnings]
        if pinned_msg and pinned_msg not in warnings_out:
            warnings_out.insert(0, pinned_msg)
        return _preview(summary, lines, warnings_out)
    except Exception as e:
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_confirm_split(
    args: dict,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """confirm_split 执行：Command(resume=proposal) 唤醒图继续跑完。"""
    try:
        thread_id = args["thread_id"].strip()
        force = bool(args.get("force", False))
        split_thread_id = f"split-{thread_id}"

        from langgraph.types import Command
        from app.split.graph import get_split_graph

        graph = get_split_graph()
        config = {"configurable": {"thread_id": split_thread_id}}
        snap = graph.get_state(config)

        if not snap.values:
            return {"error": f"分票批次不存在: split-{thread_id}"}

        if not snap.next:
            return {"error": "该分票批次未处于等待审核状态，或已完成"}

        proposal = snap.values.get("proposal", {})
        if not proposal:
            return {"error": "未找到分票方案"}

        # 构造 resume 数据
        resume_data = dict(proposal)
        resume_data["status"] = "confirmed"
        resume_data["force_confirmed"] = force

        # 唤醒图跑完（persist_split → generate_docs → END）
        for event in graph.stream(
            Command(resume=resume_data), config, stream_mode="updates"
        ):
            if "__interrupt__" in event:
                final_snap = graph.get_state(config)
                return {
                    "status": "pending_review",
                    "split_thread_id": split_thread_id,
                    "message": (
                        f"分票确认后出现新的中断，"
                        f"status: {final_snap.values.get('status')}"
                    ),
                }

        final_snap = graph.get_state(config)
        # 统计票数
        total_tickets = sum(
            len(pg.get("groups", []))
            for pg in proposal.get("ports", [])
        )
        version = final_snap.values.get("version", 1)

        return {
            "status": "confirmed",
            "split_thread_id": split_thread_id,
            "version": version,
            "total_declarations": total_tickets,
            "message": (
                f"批次 {thread_id} 分票已确认，共 {total_tickets} 票"
                f"已写入数据库。{'（强制通过）' if force else ''}"
            ),
        }
    except Exception as e:
        return _err(e)


def _preview_reset_split(args: dict, session_id: str | None = None) -> dict:
    """reset_split 预览：展示当前版本与重置确认提示。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        split_thread_id = f"split-{thread_id}"

        if not thread_id:
            return _preview("无法重置分票", [], ["thread_id 为空"])

        from app.split.graph import get_split_graph
        graph = get_split_graph()
        snap = graph.get_state(
            {"configurable": {"thread_id": split_thread_id}})

        if not snap.values:
            return _preview(
                "无法重置分票", [],
                [f"分票批次不存在: split-{thread_id}"])

        old_version = snap.values.get("version", 1)

        lines = [
            f"批次: {thread_id}",
            f"split ID: {split_thread_id}",
            f"当前版本: V{old_version}",
            "原有方案将被保留为历史版本，推荐方案重新生成。",
        ]

        summary = (
            f"确认重置批次 {thread_id} 的分票？"
            "原有方案将被保留为历史版本，推荐方案重新生成。"
        )
        warnings: list[str] = []
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)
        return _preview(summary, lines, warnings)
    except Exception as e:
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_reset_split(
    args: dict,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """reset_split 执行：resume reset → update_state(START) → 重跑 proposal。"""
    try:
        thread_id = args["thread_id"].strip()
        split_thread_id = f"split-{thread_id}"

        from langgraph.graph import START
        from langgraph.types import Command
        from app.split.graph import get_split_graph

        graph = get_split_graph()
        config = {"configurable": {"thread_id": split_thread_id}}
        snap = graph.get_state(config)

        if not snap.values:
            return {"error": f"分票批次不存在: split-{thread_id}"}

        old_version = snap.values.get("version", 0)
        source_file_path = snap.values.get("source_file_path", "")
        if not source_file_path:
            proposal = snap.values.get("proposal", {})
            source_file_path = proposal.get("source_file", "")

        if not source_file_path:
            return {"error": "无法获取源文件路径，无法重新生成方案"}

        # 如果在挂起状态，先用 reset 唤醒跑完清理
        if snap.next:
            for _event in graph.stream(
                Command(resume={"status": "reset"}),
                config,
                stream_mode="updates",
            ):
                pass  # 跑完 persist_split 的 reset 清理

        # 重置到 START 并重跑
        graph.update_state(config, {
            "split_thread_id": split_thread_id,
            "source_file_path": source_file_path,
        }, as_node=START)

        for event in graph.stream(None, config, stream_mode="updates"):
            if "__interrupt__" in event:
                final_snap = graph.get_state(config)
                new_version = final_snap.values.get("version",
                                                   old_version + 1)
                return {
                    "status": "pending_review",
                    "split_thread_id": split_thread_id,
                    "version": new_version,
                    "message": (
                        f"已重置分票方案，新版本 V{new_version}，"
                        f"请查看 /split/split-{thread_id} 页面。"
                    ),
                }

        final_snap = graph.get_state(config)
        new_version = final_snap.values.get("version", old_version + 1)
        return {
            "status": "completed",
            "version": new_version,
            "message": (
                f"已重置分票方案，新版本 V{new_version}，"
                f"请查看 /split/split-{thread_id} 页面。"
            ),
        }
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 报关工具（list_declaration_files 只读；generate/upsert_mapping 写）
# ---------------------------------------------------------------------------

def _fn_list_declaration_files(args: dict) -> dict:
    """列出某批次已生成的报关单文件。返回自然语言列表。"""
    try:
        thread_id = args["thread_id"].strip()
        split_thread_id = f"split-{thread_id}"

        settings = get_settings()
        out_dir = settings.output_dir_abs / "declarations" / split_thread_id

        if not out_dir.is_dir():
            return {"status": "empty",
                    "message": "该批次尚未生成报关单"}

        files = sorted(out_dir.glob("*.xlsx"))
        if not files:
            return {"status": "empty",
                    "message": "该批次尚未生成报关单"}

        lines = [f"{f.name}（{f.stat().st_size // 1024} KB）" for f in files]
        header = f"批次 {thread_id} 已生成 {len(files)} 份报关单："
        if len(lines) > 30:
            # 按港口归类汇总展示
            port_groups: dict[str, int] = {}
            for f in files:
                # 报关东京A.xlsx → 东京
                stem = f.stem.replace("报关", "")
                port = "".join(ch for ch in stem if not ch.isascii())
                port_groups[port] = port_groups.get(port, 0) + 1
            summary = "、".join(f"{p} {n} 份" for p, n in sorted(port_groups.items()))
            return {"status": "ok",
                    "message": f"{header}{summary}。",
                    "total": len(files)}

        return {"status": "ok",
                "message": header + "\n" + "\n".join(lines),
                "total": len(files)}
    except Exception as e:
        return _err(e)


def _preview_generate_declarations(args: dict, session_id: str | None = None) -> dict:
    """generate_declarations 预览：校验分票已确认 + 展示发票号规则。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        invoice_number = (args.get("invoice_number") or "").strip()

        warnings: list[str] = []
        # Pinned scope 防御
        warnings = _merge_pinned_warning(args, session_id, warnings)
        if not thread_id:
            warnings.append("thread_id 为空")
        if not invoice_number:
            warnings.append("invoice_number（发票号码段）为空")

        lines = [f"批次: {thread_id}",
                 f"发票号码段: {invoice_number or '（未提供）'}",
                 "发票号规则: YIL + 港口字母 + 号码段"
                 "（如 YILT656=东京、YILN656=名古屋）"]

        if thread_id:
            split_thread_id = f"split-{thread_id}"
            from app.db.models import Declaration
            from app.db.session import get_session
            with get_session() as sess:
                confirmed = sess.query(Declaration).filter(
                    Declaration.split_thread_id == split_thread_id,
                    Declaration.status == "confirmed",
                ).count()
            if confirmed == 0:
                warnings.append("该批次分票方案尚未确认，请先在分票页确认")
            else:
                lines.append(f"已确认票数: {confirmed}")

        summary = (f"确认为批次 {thread_id} 生成报关单？"
                   f"发票号码段 {invoice_number or '（未提供）'}")
        return _preview(summary, lines, warnings)
    except Exception as e:
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _exec_generate_declarations(
    args: dict,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """generate_declarations 执行：调 declare.service 生成全部报关单。"""
    try:
        thread_id = (args.get("thread_id") or "").strip()
        invoice_number = (args.get("invoice_number") or "").strip()

        # 二次校验
        if not thread_id:
            return {"error": "thread_id 为空"}
        if not invoice_number:
            return {"error": "invoice_number（发票号码段）为空"}

        split_thread_id = f"split-{thread_id}"

        from app.db.models import Declaration
        from app.db.session import get_session
        with get_session() as sess:
            confirmed = sess.query(Declaration).filter(
                Declaration.split_thread_id == split_thread_id,
                Declaration.status == "confirmed",
            ).count()
        if confirmed == 0:
            return {"error": "该批次分票方案尚未确认，请先在分票页确认"}

        from app.declare.service import generate_declarations
        result = generate_declarations(split_thread_id, invoice_number)

        count = result.get("count", 0)
        warnings = result.get("warnings") or []
        warn_text = f"，{len(warnings)} 条警告" if warnings else ""
        return {
            "status": "generated",
            "split_thread_id": split_thread_id,
            "count": count,
            "warnings": warnings,
            "message": (
                f"批次 {thread_id} 的 {count} 份报关单已生成{warn_text}，"
                f"可在分票页或批次页下载。"
            ),
        }
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return _err(e)


def _preview_upsert_product_mapping(args: dict, session_id: str | None = None) -> dict:
    """upsert_product_mapping 预览：展示将写入的映射字段。"""
    try:
        name = (args.get("product_name_cn") or "").strip()
        warnings: list[str] = []
        if not name:
            warnings.append("product_name_cn（中文品名）为空")
            return _preview("无法维护产品映射", [], warnings)

        # 判断新增还是更新
        from app.db.models import ProductMapping
        from app.db.session import get_session
        with get_session() as sess:
            existing = sess.query(ProductMapping).filter(
                ProductMapping.product_name_cn == name).first()
        action = "更新" if existing else "新增"

        field_labels = [
            ("hs_code", "税号"), ("supplier_name", "供应商"),
            ("inspection_required", "需商检"), ("name_en", "英文品名"),
            ("unit_code", "计量单位代码"),
        ]
        lines = [f"品名: {name}", f"操作: {action}"]
        for key, label in field_labels:
            v = args.get(key)
            if v is not None and v != "":
                lines.append(f"{label}: {v}")
        skus = _collect_tool_skus(args)
        if skus:
            lines.append(f"关联 SKU: {', '.join(skus)}")

        summary = f"确认{action}品名「{name}」的产品映射？"
        return _preview(summary, lines, warnings)
    except Exception as e:
        return _preview("预览生成失败", [], [f"{type(e).__name__}: {e}"])


def _collect_tool_skus(args: dict) -> list[str]:
    """从工具入参收集 SKU 列表：sku_codes（数组）+ 旧单值 sku_code 合并去重。

    外部入参保持 sku_code 单值兼容；strip、去空、去重保序。
    """
    raw: list = []
    codes = args.get("sku_codes")
    if isinstance(codes, (list, tuple)):
        raw.extend(codes)
    elif isinstance(codes, str):  # 容错：LLM 把数组写成逗号串
        raw.extend(codes.split(","))
    single = args.get("sku_code")
    if single:
        raw.append(single)
    seen: set[str] = set()
    result: list[str] = []
    for c in raw:
        c = str(c).strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _exec_upsert_product_mapping(
    args: dict,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """upsert_product_mapping 执行：按品名查有则更新无则插入，必要时回填 SKU。

    SKU 写 product_mapping_skus 子表（一品名多 SKU）；传了 sku_code/sku_codes
    才动 SKU 列表（整体替换），不传保持现状。SKU 被其他映射行占用时返回中文
    错误说明（不落库），不抛异常栈。
    """
    try:
        name = (args.get("product_name_cn") or "").strip()
        if not name:
            return {"error": "product_name_cn（中文品名）为空"}

        from app.db.models import ProductMapping, ProductMappingSku
        from app.db.session import get_session
        from app.db.sync import check_sku_conflicts, sync_mapping_to_sku

        updatable = ("hs_code", "supplier_name", "inspection_required",
                     "name_en", "unit_code")
        sku_given = "sku_code" in args or "sku_codes" in args
        sku_codes = _collect_tool_skus(args) if sku_given else []

        with get_session() as sess:
            mapping = sess.query(ProductMapping).filter(
                ProductMapping.product_name_cn == name).first()
            action = "更新"
            if mapping is None:
                mapping = ProductMapping(product_name_cn=name)
                sess.add(mapping)
                sess.flush()  # 先拿到 id，冲突校验排除本行才有意义
                action = "新增"

            # SKU 冲突拦截：整体不落库，中文说明返回给操作员
            if sku_given and sku_codes:
                conflicts = check_sku_conflicts(
                    sess, sku_codes, exclude_mapping_id=mapping.id)
                if conflicts:
                    parts = [
                        f"SKU {c['sku_code']} 已被映射「{c['product_name_cn']}」占用"
                        for c in conflicts
                    ]
                    sess.rollback()
                    return {
                        "error": "SKU 冲突，未保存：" + "；".join(parts)
                                 + "。如需改挂请先调整对应映射。",
                    }

            for key in updatable:
                if key in args and args[key] is not None and args[key] != "":
                    setattr(mapping, key, args[key])

            if sku_given:
                # 整体替换子表：先清（flush 落删除）再插，避免保留不变的 SKU
                # 在同次 flush 撞 unique_mapping_sku 唯一约束；
                # 旧列同步为第一个/None（防启动迁移幽灵搬回已删 SKU）
                mapping.sku_links.clear()
                sess.flush()
                mapping.sku_links.extend(
                    ProductMappingSku(sku_code=c) for c in sku_codes)
                mapping.sku_code = sku_codes[0] if sku_codes else None

            # 关键字段补齐则清待完善标记；税号仍空则标记待完善
            mapping.is_incomplete = not bool(mapping.hs_code)

            sess.flush()
            synced = sync_mapping_to_sku(sess, mapping)
            sess.commit()

        hs_text = f"税号 {mapping.hs_code}" if mapping.hs_code else "税号未填"
        sj_text = "需商检" if mapping.inspection_required else "无需商检"
        sku_text = f"，关联 SKU {len(sku_codes)} 个" if sku_given else ""
        sync_text = f"，已同步 {synced} 条 SKU 主数据" if synced else ""
        return {
            "status": "ok",
            "action": action,
            "message": f"品名「{name}」的映射已{action}（{hs_text}，{sj_text}）{sku_text}{sync_text}。",
        }
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# create_factory_alias / add_factories / query_master_data
# ---------------------------------------------------------------------------

def _preview_create_factory_alias(args: dict, session_id: str | None = None) -> dict:
    """create_factory_alias 预览：展示对照内容；folder 校验失败提前进 warnings。"""
    factory = (args.get("factory") or "").strip()
    folder = (args.get("folder") or "").strip()
    lines = [f"工厂: {factory}", f"对应文件夹: {folder}"]
    warnings: list[str] = []
    try:
        from app.factory_match import validate_subfolder
        validate_subfolder(get_settings().upstream_root, folder)
    except ValueError as e:
        warnings.append(str(e))
    return _preview(
        f"将永久保存对照：工厂「{factory}」→ 文件夹「{folder}」，"
        "后续批次自动按此对照匹配文件夹",
        lines, warnings)


def _exec_create_factory_alias(args: dict,
                               on_progress: Callable[[dict], None] | None = None
                               ) -> dict:
    """create_factory_alias 执行：与审核页 save-alias 同一服务端口径
    （validate_subfolder 校验 + short_name 冲突检测 + save_alias_entries 落库，
    DB 权威 + json 回退）。"""
    from fastapi import HTTPException

    from app.factory_match import save_alias_entries, validate_subfolder
    from app.review.router import _ensure_factory_short_name

    factory = (args.get("factory") or "").strip()
    folder = (args.get("folder") or "").strip()
    if not factory:
        return {"error": "工厂名不能为空"}
    try:
        validate_subfolder(get_settings().upstream_root, folder)
    except ValueError as e:
        return {"error": str(e)}
    try:
        _ensure_factory_short_name(factory, folder)
    except HTTPException as e:  # 409 short_name 冲突
        return {"error": str(e.detail)}
    try:
        saved = save_alias_entries({factory: folder})
    except Exception as e:  # noqa: BLE001
        return _err(e)
    msg = (f"已永久保存对照：工厂「{factory}」→ 文件夹「{folder}」，"
           "后续批次自动生效")
    overwritten = saved.get("overwritten") or []
    if overwritten:
        msg += f"（覆盖了原有对照: {'、'.join(overwritten)}）"
    return {"ok": True, "message": msg, "factory": factory, "folder": folder}


def _preview_add_factories(args: dict, session_id: str | None = None) -> dict:
    """add_factories 预览：批次存在性检查 + 动作说明（实际补入名单以执行时
    重新解析装箱单为准）。"""
    thread_id = args["thread_id"]
    warnings: list[str] = []
    try:
        state = service.get_order_state(thread_id)
        if not state.get("exists"):
            return _preview("无法补充工厂", [],
                            [f"批次不存在: {thread_id}"])
    except Exception as e:  # noqa: BLE001
        warnings.append(f"批次状态查询失败: {type(e).__name__}: {e}")
    warnings = _merge_pinned_warning(args, session_id, warnings)
    return _preview(
        f"将重新解析装箱单，把批次 {thread_id} 中未处理的工厂"
        "（装箱单新增/被跳过/被驳回的）补入本批次重新提取，"
        "完成后批次重新挂起等待审核；已审核写入的工厂不受影响",
        [], warnings)


def _exec_add_factories(args: dict,
                        on_progress: Callable[[dict], None] | None = None
                        ) -> dict:
    """add_factories 执行：透传 service.add_factories_to_batch；
    批次运行中/不存在的错误如实透传。"""
    try:
        result = service.add_factories_to_batch(
            args["thread_id"],
            on_progress=_wrap_on_progress("add_factories", args, on_progress),
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return _err(e)
    factories = result.get("factories") or []
    if not result.get("added"):
        return {"ok": True,
                "message": result.get("message") or "没有待补充的工厂",
                "factories": []}
    msg = f"已补入 {len(factories)} 家工厂：{'、'.join(factories)}。"
    if result.get("status") == "pending_human_review":
        msg += "批次已完成重新提取并挂起，请前往审核页审核。"
    else:
        msg += "批次已处理完成。"
    return {"ok": True, "message": msg, "factories": factories,
            "status": result.get("status"),
            "thread_id": result.get("thread_id")}


def _fn_query_master_data(args: dict) -> dict:
    """主数据查询：先按 SKU 精确查（产品映射子表 + 工厂 SKU 表），
    再按工厂名/短名/别名模糊查；两路结果合并返回。"""
    q = (args.get("query") or "").strip()
    if not q:
        return {"error": "query 不能为空"}
    from sqlalchemy import func, or_, select

    from app.db.models import (
        Factory,
        FactoryAlias,
        FactorySKU,
        ProductMapping,
        ProductMappingSku,
    )
    from app.db.session import get_session

    result: dict[str, Any] = {"query": q, "sku_matches": [], "factory_matches": []}
    try:
        with get_session() as sess:
            # ---- SKU 精确匹配：产品映射子表 + 工厂 SKU 表 ----
            seen: set[tuple] = set()
            links = sess.scalars(
                select(ProductMappingSku)
                .where(ProductMappingSku.sku_code == q)
            ).all()
            for link in links:
                m = sess.get(ProductMapping, link.mapping_id)
                if m is None:
                    continue
                key = ("mapping", m.id)
                if key in seen:
                    continue
                seen.add(key)
                result["sku_matches"].append({
                    "sku": q, "来源": "产品映射",
                    "中文品名": m.product_name_cn, "英文品名": m.name_en,
                    "税号": m.hs_code,
                    "商检": "需要" if m.inspection_required else "不需要",
                    "供应商": m.supplier_name,
                })
            sku_rows = sess.scalars(
                select(FactorySKU).where(FactorySKU.sku_code == q)
            ).all()
            for r in sku_rows:
                f = sess.get(Factory, r.factory_id)
                key = ("factory_sku", r.sku_id)
                if key in seen:
                    continue
                seen.add(key)
                result["sku_matches"].append({
                    "sku": q, "来源": "工厂SKU主数据",
                    "工厂": f.factory_name if f else None,
                    "中文品名": r.name_cn, "英文品名": r.name_en,
                    "税号": r.hs_code,
                    "商检": "需要" if r.inspection_required else "不需要",
                    "单件净重": (float(r.unit_net_weight)
                               if r.unit_net_weight is not None else None),
                    "单件毛重": (float(r.unit_gross_weight)
                               if r.unit_gross_weight is not None else None),
                })

            # ---- 工厂模糊匹配：工厂名/短名/别名 LIKE %q% ----
            like = f"%{q}%"
            fac_ids: set[int] = set()
            for f in sess.scalars(select(Factory).where(or_(
                    Factory.factory_name.like(like),
                    Factory.short_name.like(like)))).all():
                fac_ids.add(f.factory_id)
            for a in sess.scalars(select(FactoryAlias).where(
                    FactoryAlias.alias.like(like))).all():
                fac_ids.add(a.factory_id)
            for fid in sorted(fac_ids)[:10]:
                f = sess.get(Factory, fid)
                if f is None:
                    continue
                aliases = sess.scalars(
                    select(FactoryAlias.alias)
                    .where(FactoryAlias.factory_id == fid)
                ).all()
                sku_count = sess.scalar(
                    select(func.count(FactorySKU.sku_id))
                    .where(FactorySKU.factory_id == fid)
                ) or 0
                result["factory_matches"].append({
                    "工厂名": f.factory_name, "中文短名": f.short_name,
                    "别名": sorted(aliases), "主数据SKU数": sku_count,
                })
    except Exception as e:  # noqa: BLE001
        return _err(e)

    if not result["sku_matches"] and not result["factory_matches"]:
        result["message"] = f"主数据中未找到与「{q}」相关的 SKU 或工厂"
    # 防超长：SKU 命中截 20 条
    result["sku_matches"] = result["sku_matches"][:20]
    return result


# ---------------------------------------------------------------------------
# process_skipped_factory（剧本宏：一次确认跑完 建对照 + 补充工厂 全链）
# ---------------------------------------------------------------------------

def _preview_process_skipped_factory(args: dict, session_id: str | None = None) -> dict:
    """剧本宏预览：一次展示两步计划 + 前置校验警告（folder 合法性 /
    批次存在性 / 工厂是否已被处理）。"""
    thread_id = (args.get("thread_id") or "").strip()
    factory = (args.get("factory") or "").strip()
    folder = (args.get("folder") or "").strip()
    warnings: list[str] = []
    try:
        from app.factory_match import validate_subfolder
        validate_subfolder(get_settings().upstream_root, folder)
    except ValueError as e:
        warnings.append(str(e))
    try:
        state = service.get_order_state(thread_id)
        if not state.get("exists"):
            warnings.append(f"批次不存在: {thread_id}")
        else:
            values = state.get("values") or {}
            # 工厂已写入（approve 过）则本次补充不会重复处理，提前告知
            if factory in (values.get("factory_outputs") or {}):
                warnings.append(
                    f"工厂「{factory}」在本批次已审核写入，补充时不会重复处理")
    except Exception as e:  # noqa: BLE001 预览期校验失败不阻塞，仅提示
        warnings.append(f"批次状态查询失败: {type(e).__name__}: {e}")
    warnings = _merge_pinned_warning(args, session_id, warnings)
    return _preview(
        f"将处理批次 {thread_id} 中被跳过的工厂「{factory}」",
        [f"第 1 步：永久保存对照——工厂「{factory}」→ 文件夹「{folder}」，"
         "后续批次自动生效",
         f"第 2 步：重新解析装箱单，把「{factory}」补入批次 {thread_id} "
         "重新提取，完成后批次重新挂起等待审核"],
        warnings)


def _exec_process_skipped_factory(args: dict,
                                  on_progress: Callable[[dict], None] | None = None
                                  ) -> dict:
    """剧本宏执行：顺序跑 建对照 → 补充工厂，与两个独立工具共用执行体。

    失败语义：第一步失败则不执行第二步；第二步失败时明确回报系统状态
    （对照已保存成功 + 补充失败原因），不沉默回滚已成功的第一步。
    """
    step1 = _exec_create_factory_alias(args)
    if step1.get("error"):
        return {"error": f"第 1 步（保存对照）失败：{step1['error']}"}
    msg1 = step1.get("message") or "对照已保存"
    if on_progress:
        on_progress({"type": "exec_progress", "tool": "process_skipped_factory",
                     "thread_id": args.get("thread_id"),
                     "message": "对照已保存，开始补充工厂…"})
    step2 = _exec_add_factories(args, on_progress=on_progress)
    if step2.get("error"):
        return {"error": f"{msg1}；但第 2 步（补充工厂）失败：{step2['error']}",
                "partial": True}
    return {"ok": True,
            "message": f"{msg1}。{step2.get('message') or ''}".strip(),
            "factory": args.get("factory"), "folder": args.get("folder"),
            "factories": step2.get("factories") or [],
            "status": step2.get("status"),
            "thread_id": step2.get("thread_id")}


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
        description="查询单个批次的轻量状态摘要（状态/进度/下一节点/校验结果/"
                    "未处理工厂名单）。比 get_batch_detail 快，操作员只问"
                    "“某某批次现在怎么样了”时优先用它；unprocessed_factories "
                    "字段列出被跳过/驳回/未写入的工厂（可用 add_factories 补入）。",
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

    "list_directory": Tool(
        name="list_directory",
        description="列出目录内容，用于引导用户选择文件/文件夹路径。"
                    "Agent 可用此工具探索文件系统，帮助用户定位要处理的文件。"
                    "参数：path(目录路径，可选，默认为 home 目录；Windows 下不传则返回盘符列表)、"
                    "type('file' 或 'dir'，选择过滤类型)、"
                    "extensions(扩展名过滤，如 'xlsx,xls'，逗号分隔)。"
                    "返回目录内容列表（最多 100 条），含 name/path/is_dir/size 等字段。"
                    "跳过隐藏文件（以 . 开头）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要列出的目录路径（可选）；Windows 下不传则返回盘符列表供用户选择",
                },
                "type": {
                    "type": "string",
                    "enum": ["file", "dir"],
                    "description": "过滤类型：file=只显示文件，dir=只显示目录；不传默认 dir",
                },
                "extensions": {
                    "type": "string",
                    "description": "扩展名过滤（逗号分隔，如 'xlsx,xls'）；仅当 type='file' 时生效",
                },
            },
        },
        risk="read",
        func=_fn_list_directory,
    ),

    # ---- UI 交互工具（risk="ui"，由前端渲染交互组件）----
    "request_file_selection": Tool(
        name="request_file_selection",
        description="请求用户在界面上手动选择文件或文件夹路径。"
                    "调用后前端会弹出文件浏览器模态框，用户选择的路径会作为"
                    "新用户消息返回，Agent 可在下一轮看到所选路径。"
                    "适用场景：Agent 不确定用户的文件路径、需要用户确认文件位置等。"
                    "参数：type('file' 或 'dir'，选择类型)、"
                    "extensions(扩展名过滤，如 'xlsx,xls'，仅 type='file' 时有效)、"
                    "title(浏览器标题，可选)。",
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["file", "dir"],
                    "description": "选择类型：file=选文件，dir=选文件夹",
                },
                "extensions": {
                    "type": "string",
                    "description": "扩展名过滤（逗号分隔，如 'xlsx,xls'）；仅 type='file' 时有效",
                },
                "title": {
                    "type": "string",
                    "description": "文件浏览器标题（可选），如 '选择上游工厂文件夹'",
                },
            },
            "required": ["type"],
        },
        risk="ui",
        # UI 工具无 func/preview/execute，由 lc_tools 特殊处理
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
                    "仅挂起待审核批次可用；未挂起批次会报错。"
                    "注意：批次已完成时应改用 add_factories 补入跳过/未处理的工厂。",
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
    "force_extract_file": Tool(
        name="force_extract_file",
        description="对操作员指定的单个文件强制重新提取（跳过自动目标识别），"
                    "可选指定页码范围只提取其中几页，指定页码时一律走视觉大模型识别。"
                    "适用场景：①自动识别选错了箱单文件（比如挑了报关汇总版而不是"
                    "SKU 级箱单），操作员告知正确文件；②箱单是扫描件/图片，"
                    "自动识别扫不出内容；③一份 PDF 里混了多份单据，操作员只要其中几页；"
                    "④文本层识别把数字读错了，要改用看图的方式重新识别。"
                    "提取结果并入当前工厂的已有结果（同 SKU 数值不同按改单覆盖，"
                    "并标记需人工确认），随后自动重新挂起待审核。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "仅挂起待审核批次可用；文件必须在本批次上游工厂文件夹之下。"
                    "注意：本工具只处理一个文件，不重跑整个工厂——要重跑整个工厂的"
                    "识别请用 retry_factory。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "file_path": {
                    "type": "string",
                    "description":
                                "要强制提取的文件绝对路径，必须位于本批次上游"
                                "工厂文件夹之下。操作员没给绝对路径时，先用 "
                                "list_directory 浏览该工厂文件夹帮他定位，"
                                "或用 request_file_selection 让他在界面选择，"
                                "禁止编造路径",
                },
                "pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description":
                                "可选，只提取这几页（PDF 专用，页码从 1 开始，"
                                "最多 12 页）。传了页码就一定走视觉大模型识别。"
                                "不传=整份文件按常规规则识别。"
                                "操作员说「第 3 到 5 页」时传 [3,4,5]；"
                                "对 Excel/图片文件不适用，不要传",
                },
                "force_vision": {
                    "type": "boolean",
                    "description":
                                "可选，默认 false。true=不管文件有没有文字层，"
                                "一律用视觉大模型看图识别。操作员说"
                                "「用看图的方式再试」「识别的数字不对，换个方式」"
                                "时设 true；传了 pages 时自动为 true，无需重复指定",
                },
            },
            "required": ["thread_id", "file_path"],
        },
        risk="write",
        preview=_preview_force_extract_file,
        execute=_exec_force_extract_file,
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

    # ---- 分票工具（只读 + 写）----
    "get_split_proposal": Tool(
        name="get_split_proposal",
        description="查看当前推荐/已确认分票方案的摘要（港口数/票数/商检分布）。"
                    "操作员问“分票方案是什么”“这个批次怎么分的”时使用；"
                    "返回纯自然语言摘要如“批次 XX 已确认分票，共 30 票，4 个港口："
                    "名古屋港 12 票（含 4 票商检）…”。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_get_split_proposal,
    ),
    "list_declarations": Tool(
        name="list_declarations",
        description="列出某批次所有已确认票的明细，按港口+票号排序。"
                    "每票一行：「票号」箱型 柜数 含商检(工厂名)/普通。"
                    "超 50 票时自动按港口汇总。"
                    "操作员问“有哪些票”“某批次票的明细”时使用。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_list_declarations,
    ),
    "start_split": Tool(
        name="start_split",
        description="对话触发分票：为已完成的提取批次启动分票流水线。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "preview 自动从批次状态获取 filled Excel 路径；"
                    "也可手动提供 source_file_path 覆盖。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "source_file_path": {
                    "type": "string",
                    "description": "可选，批次 filled Excel 的绝对路径；"
                                   "缺省时从上游批次 state 取 final_output_path",
                },
            },
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_start_split,
        execute=_exec_start_split,
    ),
    "confirm_split": Tool(
        name="confirm_split",
        description="对话中确认分票方案（相当于 UI 的「确认分票」按钮）。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "方案存在警告时，操作员须确认是否强制通过"
                    "（force=true）；无警告时直接确认。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "force": {
                    "type": "boolean",
                    "description": "可选，默认 false。true=强制通过（忽略警告，"
                                   "标记为强制通过）",
                },
            },
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_confirm_split,
        execute=_exec_confirm_split,
    ),
    "reset_split": Tool(
        name="reset_split",
        description="对话中重置分票：原有方案保留为历史版本，重新生成推荐方案。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_reset_split,
        execute=_exec_reset_split,
    ),

    # ---- 报关工具（只读 + 写）----
    "list_declaration_files": Tool(
        name="list_declaration_files",
        description="列出某批次已生成的报关单文件（文件名+大小）。"
                    "操作员问“报关单生成了吗”“生成了哪些报关单”时使用；"
                    "超 30 份时按港口汇总。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="read",
        func=_fn_list_declaration_files,
    ),
    "generate_declarations": Tool(
        name="generate_declarations",
        description="按已确认的分票方案生成全部报关单（xlsx）。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "必须提供 invoice_number（发票号码段，如 656），"
                    "操作员没给时要先追问。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "invoice_number": {
                    "type": "string",
                    "description": "发票号码段（人工提供），如 '656'；"
                                   "完整发票号自动拼为 YIL+港口字母+号码段",
                },
            },
            "required": ["thread_id", "invoice_number"],
        },
        risk="write",
        preview=_preview_generate_declarations,
        execute=_exec_generate_declarations,
    ),
    "upsert_product_mapping": Tool(
        name="upsert_product_mapping",
        description="新增或更新产品映射（中文品名→税号/供应商/商检/英文品名/计量单位代码/关联 SKU）。"
                    "一个品名可关联多个 SKU（sku_codes 数组或 sku_code 单值）；"
                    "SKU 被其他品名占用时会拒绝并说明冲突。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。"
                    "大批量维护引导操作员去主数据维护页。",
        parameters={
            "type": "object",
            "properties": {
                "product_name_cn": {
                    "type": "string",
                    "description": "中文品名（必填，主匹配键）",
                },
                "hs_code": {"type": "string", "description": "税号（HS 编码）"},
                "supplier_name": {"type": "string", "description": "供应商报关全称"},
                "inspection_required": {"type": "boolean", "description": "是否需要商检"},
                "name_en": {"type": "string", "description": "英文品名"},
                "unit_code": {"type": "string", "description": "计量单位代码（如 007）"},
                "sku_code": {"type": "string", "description": "关联 SKU 编码（单个；与 sku_codes 合并）"},
                "sku_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关联 SKU 编码列表（一品名多 SKU）；传入则整体替换该品名的 SKU 列表",
                },
            },
            "required": ["product_name_cn"],
        },
        risk="write",
        preview=_preview_upsert_product_mapping,
        execute=_exec_upsert_product_mapping,
    ),
    "create_factory_alias": Tool(
        name="create_factory_alias",
        description="永久保存工厂↔文件夹对照（工厂别名）。操作员告知「某工厂对应"
                    "某个文件夹」时使用；保存后后续所有批次自动按此对照匹配。"
                    "文件夹名是上游根目录下现存的一级子目录名，不是完整路径。"
                    "对照冲突（工厂已有不一致的中文短名等）会拒绝并提示去主数据页处理。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。",
        parameters={
            "type": "object",
            "properties": {
                "factory": {
                    "type": "string",
                    "description": "工厂名（装箱单/单据里的工厂名）",
                },
                "folder": {
                    "type": "string",
                    "description": "对应的上游文件夹名（一级子目录名，不是完整路径）",
                },
            },
            "required": ["factory", "folder"],
        },
        risk="write",
        preview=_preview_create_factory_alias,
        execute=_exec_create_factory_alias,
    ),
    "add_factories": Tool(
        name="add_factories",
        description="补充工厂：重新解析装箱单，把批次中未处理的工厂（装箱单新增的、"
                    "被跳过的、被驳回的）补入该批次重新提取，完成后重新挂起审核。"
                    "已完成/挂起中的批次可用；正在运行的批次会报错。"
                    "已审核写入的工厂绝不重复处理。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。",
        parameters={
            "type": "object",
            "properties": {"thread_id": _THREAD_ID_PROP},
            "required": ["thread_id"],
        },
        risk="write",
        preview=_preview_add_factories,
        execute=_exec_add_factories,
    ),
    "query_master_data": Tool(
        name="query_master_data",
        description="查询主数据：传入 SKU 条码或工厂名/别名。SKU 命中返回品名/税号/"
                    "商检/单重/所属工厂；工厂命中返回工厂名/中文短名/别名列表/"
                    "主数据 SKU 数。操作员问「这个 SKU 是什么」「这个工厂的税号」"
                    "「某工厂有哪些别名」等主数据问题时使用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "SKU 条码或工厂名/别名",
                },
            },
            "required": ["query"],
        },
        risk="read",
        func=_fn_query_master_data,
    ),
    "process_skipped_factory": Tool(
        name="process_skipped_factory",
        description="剧本宏：一次确认跑完「处理被跳过工厂」全链——第 1 步永久保存"
                    "工厂↔文件夹对照（后续批次自动生效），第 2 步把该工厂补入批次"
                    "重新提取并重新挂起审核。操作员要处理已完成批次里因没匹配上"
                    "文件夹而被跳过的工厂、且已告知对应文件夹名时使用（等价于"
                    "create_factory_alias + add_factories 组合，但只需一次确认）。"
                    "写操作：须先向操作员展示 preview 并获得确认后才执行。",
        parameters={
            "type": "object",
            "properties": {
                "thread_id": _THREAD_ID_PROP,
                "factory": {
                    "type": "string",
                    "description": "被跳过的工厂名（装箱单/单据里的工厂名）",
                },
                "folder": {
                    "type": "string",
                    "description": "对应的上游文件夹名（一级子目录名，不是完整路径）",
                },
            },
            "required": ["thread_id", "factory", "folder"],
        },
        risk="write",
        preview=_preview_process_skipped_factory,
        execute=_exec_process_skipped_factory,
    ),
}


# ---------------------------------------------------------------------------
# 注册表查询接口
# ---------------------------------------------------------------------------

def visible_tools(phase: int = 1) -> list[Tool]:
    """按阶段返回可见工具：phase=1 只读+UI 工具；phase=2 全部（含写工具）。"""
    if phase >= 2:
        return list(TOOLS.values())
    return [t for t in TOOLS.values() if t.risk in ("read", "ui")]


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
