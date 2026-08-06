"""调度 Agent 写工具执行结果的确定性中文总结（W2）。

问题4：execute_confirmed 原来把 300 字 JSON 直接糊给操作员，数字常被 LLM
转述出错。本模块按工具模板做**确定性**摘要（不经 LLM），产出：
- message：一句话人读结论（含关键数字）；
- summary_lines：补充明细行（进度/改动清单/警告）；
- links：跳转按钮（去审核 /review?thread_id=、看批次 /batch/{tid}）。

铁律契约：summarize_applied 绝不抛异常——整个函数 try/except 兜底，
任何意外（含 result 为 None/非 dict、未知工具）都落到 fallback。
"""
from __future__ import annotations


def _links_for(thread_id: str | None) -> list[dict]:
    """批次相关跳转链接：审核页 /review?thread_id= + 批次详情 /batch/{tid}。"""
    if not thread_id:
        return []
    return [
        {"label": "去审核", "href": f"/review?thread_id={thread_id}"},
        {"label": "看批次", "href": f"/batch/{thread_id}"},
    ]


def _progress_line(thread_id: str) -> str | None:
    """反查批次实时进度行（延迟 import service；失败静默省略）。"""
    try:
        from app.api import service

        summary = service.get_batch_summary(thread_id)
        prog = summary.get("progress") or {}
        total = prog.get("total")
        done = prog.get("done")
        if total:
            current = prog.get("current_factory") or "-"
            return f"批次进度：{done}/{total} 个工厂已处理（当前：{current}）"
    except Exception:  # noqa: BLE001 进度反查失败不阻塞摘要
        return None
    return None


def _review_lines(review_data: dict) -> list[str]:
    """pending_human_review 的审核包明细行（工厂/SKU 数/缺失/提取覆盖）。"""
    lines: list[str] = []
    items = review_data.get("items") or []
    lines.append(f"待审核工厂：{review_data.get('factory_name') or '未知'}"
                 f"，共 {len(items)} 个 SKU")
    missing = review_data.get("missing_skus") or []
    if missing:
        lines.append(f"缺失 SKU {len(missing)} 个："
                     f"{', '.join(str(s) for s in missing[:5])}"
                     f"{' 等' if len(missing) > 5 else ''}")
    coverage = review_data.get("extraction_coverage") or {}
    if isinstance(coverage, dict) and coverage:
        cov = coverage.get("coverage_ratio")
        if cov is not None:
            lines.append(f"提取覆盖率：{cov}")
    issues = review_data.get("extraction_issues") or []
    if issues:
        lines.append(f"提取反馈 {len(issues)} 条（审核页可见详情）")
    return lines


def _sum_batch_like(tool: str, result: dict) -> dict:
    """create_batch / rerun 共用模板：跑到待审核挂起 or 全程无挂起完成。"""
    tid = result.get("thread_id")
    status = result.get("status")
    lines: list[str] = []
    verb = "已创建" if tool == "create_batch" else "已重跑"

    if status == "pending_human_review":
        review_data = result.get("review_data") or {}
        lines += _review_lines(review_data)
        prog = _progress_line(str(tid)) if tid else None
        if prog:
            lines.append(prog)
        factory = review_data.get("factory_name")
        message = (f"批次 {tid} {verb}，现挂起等待人工审核"
                   f"{f'（工厂：{factory}）' if factory else ''}。")
        return {"message": message, "summary_lines": lines,
                "links": _links_for(str(tid) if tid else None)}

    if status == "completed":
        message = f"批次 {tid} {verb}，全部工厂处理完成，无需人工审核。"
        out = result.get("final_output_path")
        if out:
            lines.append(f"最终输出：{out}")
        return {"message": message, "summary_lines": lines,
                "links": ([{"label": "看批次", "href": f"/batch/{tid}"}]
                          if tid else [])}

    # 状态不认识：通用兜底但带上已知信息
    message = f"批次 {tid} {verb}（状态：{status or '未知'}）。"
    return {"message": message, "summary_lines": lines,
            "links": _links_for(str(tid) if tid else None)}


def _sum_submit_review(args: dict, result: dict) -> dict:
    """submit_review：当前工厂落库完成 or 推进到下一工厂待审核。"""
    tid = result.get("thread_id") or args.get("thread_id")
    status = result.get("status")

    if status == "pending_human_review":
        review_data = result.get("review_data") or {}
        factory = review_data.get("factory_name")
        lines = ["上一工厂审核结果已提交并落库。"] + _review_lines(review_data)
        prog = _progress_line(str(tid)) if tid else None
        if prog:
            lines.append(prog)
        message = (f"批次 {tid} 已推进到下一工厂待审核"
                   f"{f'（{factory}）' if factory else ''}。")
        return {"message": message, "summary_lines": lines,
                "links": _links_for(str(tid) if tid else None)}

    # success：全部工厂审核完毕、落库 + 写回完成
    message = f"批次 {tid} 审核已全部完成：数据已落库并写入下游表格。"
    lines = []
    final_msg = result.get("message")
    if final_msg and final_msg not in message:
        lines.append(str(final_msg))
    out = result.get("final_output_path")
    if out:
        lines.append(f"最终输出：{out}")
    val = result.get("final_validation_status")
    if val:
        lines.append(f"最终校验状态：{val}")
    return {"message": message, "summary_lines": lines,
            "links": ([{"label": "看批次", "href": f"/batch/{tid}"}]
                      if tid else [])}


def _sum_retry_factory(args: dict, result: dict) -> dict:
    """retry_factory：单厂重新提取后重新挂起待审核（或已是最后工厂完成）。
    带 folder 对照注入时 message 注明对照；alias_saved/alias_save_error
    分别追加「对照已永久保存」与落盘失败警告。"""
    tid = result.get("thread_id") or args.get("thread_id")
    status = result.get("status")
    factory = result.get("factory")
    folder = args.get("folder")
    # 对照注入文案片段：带 folder 时在成功 message 中体现
    mapping_text = f"按对照（→ {folder}）" if folder else ""

    # 对照落盘结果的 summary_lines 补充（成功追加说明，失败追加警告）
    def _alias_lines(lines: list) -> list:
        if result.get("alias_saved"):
            lines.append("对照已永久保存，后续批次自动生效。")
        if result.get("alias_save_error"):
            lines.append(f"⚠️ 对照永久保存失败（本次重试不受影响）："
                         f"{result['alias_save_error']}")
        return lines

    if status == "pending_human_review":
        review_data = result.get("review_data") or {}
        factory = factory or review_data.get("factory_name")
        items = review_data.get("items")
        count_text = f"（共 {len(items)} 个 SKU）" if isinstance(items, list) else ""
        lines = _review_lines(review_data)
        prog = _progress_line(str(tid)) if tid else None
        if prog:
            lines.append(prog)
        if folder:
            message = (f"批次 {tid} 工厂「{factory or '未知'}」已按对照"
                       f"（→ {folder}）重新提取，待人工审核{count_text}。")
        else:
            message = (f"批次 {tid} 工厂「{factory or '未知'}」已重新提取，"
                       f"待人工审核{count_text}。")
        return {"message": message, "summary_lines": _alias_lines(lines),
                "links": _links_for(str(tid) if tid else None)}

    if status == "completed":
        message = (f"批次 {tid} 工厂「{factory or '未知'}」已{mapping_text}"
                   "重新提取，全部工厂处理完成，无需人工审核。")
        lines = []
        out = result.get("final_output_path")
        if out:
            lines.append(f"最终输出：{out}")
        return {"message": message, "summary_lines": _alias_lines(lines),
                "links": ([{"label": "看批次", "href": f"/batch/{tid}"}]
                          if tid else [])}

    # 状态不认识：通用兜底但带上已知信息
    message = f"批次 {tid} 单厂重试已执行（状态：{status or '未知'}）。"
    return {"message": message, "summary_lines": _alias_lines([]),
            "links": _links_for(str(tid) if tid else None)}


# 环境变量名 → 人可读中文标签，避免向用户暴露内部变量名
ENV_LABELS: dict[str, str] = {
    "UPSTREAM_ROOT": "上游工厂文件夹",
    "DOWNSTREAM_FILE_PATH": "下游装箱表",
    "GT_SOURCE": "GT 基准文件",
}


def _sum_set_paths(result: dict) -> dict:
    """set_paths：.env 已持久化 + 运行时生效 + 可选的当前批次重跑。"""
    applied = result.get("applied") or {}
    count = len(applied)
    lines = [f"{ENV_LABELS.get(k, k)} → {v}" for k, v in applied.items()]
    message = f"已修改 {count} 项路径配置，写入 .env 持久生效并即时应用。"
    for w in result.get("warnings") or []:
        lines.append(f"注意：{w}")
    rerun = result.get("rerun")
    links: list[dict] = []
    if isinstance(rerun, dict) and rerun:
        r_tid = rerun.get("thread_id")
        if rerun.get("status") == "pending_human_review":
            lines.append(f"当前批次 {r_tid} 已用新路径重跑，现挂起等待人工审核。")
            links = _links_for(str(r_tid) if r_tid else None)
        elif rerun.get("status") == "completed":
            lines.append(f"当前批次 {r_tid} 已用新路径重跑完成。")
        elif rerun.get("error"):
            lines.append(f"当前批次重跑失败：{rerun['error']}")
    return {"message": message, "summary_lines": lines, "links": links}


def _sum_curate_kb(result: dict) -> dict:
    """curate_kb：LLM 起草 → 入库 → 灌库 → 清队列的结果复述。"""
    drafted = result.get("drafted") or {}
    guide_count = len(drafted.get("guide") or {})
    issue_count = len(drafted.get("issue") or {})
    message = result.get("message") or "知识库策展完成。"
    lines = [f"新增操作指引（guide）{guide_count} 条、错误案例（issue）{issue_count} 条。"]
    for cat, label in (("guide", "指引"), ("issue", "错误案例")):
        for key, entry in (drafted.get(cat) or {}).items():
            title = (entry or {}).get("title") if isinstance(entry, dict) else None
            lines.append(f"[{label}] {title or key}")
    return {"message": str(message), "summary_lines": lines, "links": []}


def _sum_start_split(result: dict) -> dict:
    """start_split：已启动分票并挂起等待审核。"""
    split_tid = result.get("split_thread_id", "")
    tid = split_tid.replace("split-", "", 1) if split_tid.startswith("split-") else ""
    message = result.get("message") or f"已启动分票 {split_tid}，等待人工审核。"
    lines = []
    if tid:
        lines.append(f"上游批次: {tid}")
    lines.append(f"split ID: {split_tid}")
    links = [{"label": "去分票页", "href": f"/split/{split_tid}"}] if split_tid else []
    return {"message": str(message), "summary_lines": lines, "links": links}


def _sum_confirm_split(result: dict) -> dict:
    """confirm_split：分票已确认落库。"""
    split_tid = result.get("split_thread_id", "")
    tid = split_tid.replace("split-", "", 1) if split_tid.startswith("split-") else ""
    total = result.get("total_declarations", 0)
    version = result.get("version", 1)
    force = "（强制通过）" if result.get("force_confirmed") else ""
    message = (f"批次 {tid} 分票已确认，共 {total} 票已写入数据库{force}。"
               if tid else f"分票已确认，共 {total} 票已写入数据库{force}。")
    lines = [f"版本: V{version}", f"票数: {total}"]
    links = [{"label": "去分票页", "href": f"/split/{split_tid}"}] if split_tid else []
    return {"message": message, "summary_lines": lines, "links": links}


def _sum_reset_split(result: dict) -> dict:
    """reset_split：已重置并重新生成推荐方案。"""
    split_tid = result.get("split_thread_id", "")
    version = result.get("version", 1)
    message = result.get("message") or f"已重置分票方案，新版本 V{version}。"
    lines = [f"新版本: V{version}"]
    links = [{"label": "去分票页", "href": f"/split/{split_tid}"}] if split_tid else []
    return {"message": str(message), "summary_lines": lines, "links": links}


def _fallback(tool: str, result: dict) -> dict:
    """未知工具/异常兜底：一句"已执行" + result 前 5 个标量 key。"""
    try:
        lines = []
        if isinstance(result, dict):
            for k, v in result.items():
                if len(lines) >= 5:
                    break
                if v is None or isinstance(v, (str, int, float, bool)):
                    lines.append(f"{k}: {v}")
        return {"message": f"已执行 {tool}", "summary_lines": lines, "links": []}
    except Exception:  # noqa: BLE001 兜底自身也绝不抛
        return {"message": f"已执行 {tool}", "summary_lines": [], "links": []}


def summarize_applied(tool: str, args: dict | None, result: dict | None) -> dict:
    """写工具执行结果 → {"message", "summary_lines", "links"}（绝不抛异常）。

    - result 含 error：公共前置，message 为失败文案（"执行失败：…"）；
    - create_batch/rerun/retry_factory/submit_review/set_paths/curate_kb：按工具模板；
    - 其余（未知工具、模板内异常、result 非 dict）：fallback。
    """
    try:
        tool = str(tool or "")
        args = args if isinstance(args, dict) else {}
        if not isinstance(result, dict):
            return _fallback(tool, result if isinstance(result, dict) else {})

        error = result.get("error")
        if error:
            return {"message": f"执行失败：{error}",
                    "summary_lines": [], "links": []}

        if tool in ("create_batch", "rerun"):
            return _sum_batch_like(tool, result)
        if tool == "retry_factory":
            return _sum_retry_factory(args, result)
        if tool == "submit_review":
            return _sum_submit_review(args, result)
        if tool == "set_paths":
            return _sum_set_paths(result)
        if tool == "curate_kb":
            return _sum_curate_kb(result)
        if tool == "start_split":
            return _sum_start_split(result)
        if tool == "confirm_split":
            return _sum_confirm_split(result)
        if tool == "reset_split":
            return _sum_reset_split(result)
        return _fallback(tool, result)
    except Exception:  # noqa: BLE001 铁律：摘要失败绝不阻塞执行结果返回
        return _fallback(str(tool or ""), result if isinstance(result, dict) else {})


__all__ = ["summarize_applied"]
