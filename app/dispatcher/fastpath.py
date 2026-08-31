# -*- coding: utf-8 -*-
"""快路径意图路由：高频纯读查询跳过 LLM，规则直调工具（毫秒级响应）。

动机：ReAct 引擎下每句话都要全量调 LLM（系统提示 + 全部工具 schema +
历史），「有哪些批次」这种纯查询也要等一次完整的 LLM 往返。快路径在
进引擎之前做保守的规则匹配：命中三个零风险只读意图（批次列表 / 批次
状态 / 用量查询）就直接调工具注册表里的 func 并格式化成自然语言返回；
拿不准一律返回 None 交给 LLM——宁可漏判，不可误判吃掉复杂问题。

命中判定三层保险：
1. 长度上限：超过 _MAX_LEN 字的句子直接放行给 LLM（复杂问题不做快路径）；
2. 动作词哨兵：带 发起/重跑/删除/保存… 等词的一律放行（可能是写操作）；
3. 白名单句式：每个意图只认有限的精确句式模板，不做模糊打分。

返回契约：命中 → {"tool": 工具名, "args": 参数, "message": 格式化文本}；
未命中/工具执行异常 → None（异常时记 warning 日志并放行给 LLM）。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_LEN = 20  # 超过这个长度的句子不做快路径

# 动作词哨兵：出现即放行给 LLM（写操作/多步意图优先）
_ACTION_WORDS = (
    "发起", "重跑", "重试", "删除", "修改", "提取", "跳过", "驳回",
    "补充", "确认", "提交", "保存", "对照", "映射", "分票", "报关",
    "设置", "重开", "审核", "打开", "下载", "导出", "新建", "改",
)

# 句尾语气词/标点归一化（锚定句尾，逐轮剥除「呢。」「，呢」这类组合）
_TAIL_RE = re.compile(r"(?:[呢吗啊吧呀]+|来着|[\s，。！？?!,.~～、]+)$")

_STATUS_CN = {
    "pending_review": "待审核",
    "running": "运行中",
    "completed": "已完成",
    "error": "异常",
    "unknown": "未知",
}

# ---- 批次列表句式 ----
_BATCH_LIST_RE = re.compile(
    r"^(?:(?:查看|查询|列出|显示|看看|看|查)(?:一下)?)?"
    r"(?:目前|现在|当前|所有|全部)?的?批次(?:列表|清单|情况)?$"
    r"|^(?:现在|目前|当前)?有(?:哪些|啥|什么)批次$"
    r"|^批次(?:都?有哪些|列表|清单)$"
)

# ---- 用量查询句式 ----
_USAGE_RE = re.compile(
    r"^(?:(?:查看|查询|看看|看|查)(?:一下)?)?(?:(?:当前|目前|本月|累计|总)的?)?"
    r"(?:token|Token|TOKEN)?(?:用量|消耗|花费|费用|使用量)(?:统计|情况)?$"
    r"|^(?:用了|消耗了)多少\s*(?:token|Token|TOKEN)$"
    r"|^(?:token|Token|TOKEN)(?:用量|消耗|统计)$"
)

# ---- 批次状态句式（需提取批次号）----
_BATCH_STATUS_RES = [
    # 「批次89的状态」「批次89状态」「批次89怎么样了」「89的进度」
    re.compile(
        r"^(?:批次|批号)?\s*([A-Za-z0-9][\w\-]{1,30}?)\s*的?"
        r"(?:状态|进度|情况|怎么样(?:了)?|如何)$"),
    # 「查一下批次89」「看看89」（歧义大，仅当含 状态/进度 语义的反向句式）
    re.compile(
        r"^(?:状态|进度)(?:查询|查看)?(?:批次|批号)\s*([A-Za-z0-9][\w\-]{1,30})$"),
]
# ---- 批次体检句式（需提取批次号）----
_BATCH_HEALTH_RES = [
    # 「批次89体检」「89体检」「体检批次89」「体检 89」
    re.compile(
        r"^(?:批次|批号)?\s*([A-Za-z0-9][\w\-]{1,30}?)\s*的?体检$"),
    re.compile(
        r"^体检\s*(?:批次|批号)?\s*([A-Za-z0-9][\w\-]{1,30})$"),
]
# 批次号提取的排除词（别把句式里的关键词当批次号）
_NOT_BATCH_ID = {"批次", "批号", "所有", "全部", "当前", "目前", "状态",
                 "进度", "情况"}


def _normalize(message: str) -> str:
    """strip + 逐轮剥除句尾语气词/标点（只动句尾，不碰句中内容）。"""
    text = message.strip()
    for _ in range(3):
        new = _TAIL_RE.sub("", text)
        if new == text:
            break
        text = new
    return text.strip()


def _guarded(message: str) -> str | None:
    """三层保险的前两级：返回归一化文本；不合格（太长/带动作词）返回 None。"""
    if len(message) > _MAX_LEN:
        return None
    if any(w in message for w in _ACTION_WORDS):
        return None
    norm = _normalize(message)
    return norm or None


def _fmt_batch_list(result: dict) -> str:
    batches = result.get("batches") or []
    if not batches:
        return "当前没有任何批次。"
    lines = [f"共 {len(batches)} 个批次："]
    for b in batches[:20]:
        status = _STATUS_CN.get(b.get("status"), b.get("status") or "未知")
        extra = ""
        prog = b.get("progress")
        if isinstance(prog, dict) and prog.get("total"):
            extra = f"，进度 {prog.get('done', 0)}/{prog['total']}"
            if prog.get("current_factory"):
                extra += f"，当前工厂 {prog['current_factory']}"
        lines.append(f"- {b.get('thread_id')}（{status}{extra}）")
    if len(batches) > 20:
        lines.append(f"……等共 {len(batches)} 个批次")
    return "\n".join(lines)


def _fmt_batch_status(thread_id: str, result: dict) -> str:
    if result.get("error"):
        return f"没有找到批次「{thread_id}」，请检查批次号是否正确。"
    status = _STATUS_CN.get(result.get("status"),
                            result.get("status") or "未知")
    lines = [f"批次 {thread_id}：{status}"]
    prog = result.get("progress")
    if isinstance(prog, dict) and prog.get("total"):
        line = f"进度 {prog.get('done', 0)}/{prog['total']}"
        if prog.get("current_factory"):
            line += f"，当前工厂 {prog['current_factory']}"
        lines.append(line)
    unprocessed = result.get("unprocessed_factories") or []
    if unprocessed:
        lines.append("未处理工厂：" + "、".join(str(f) for f in unprocessed))
    return "；".join(lines)


def _fmt_usage(result: dict) -> str:
    calls = result.get("calls", 0)
    failed = result.get("failed_calls", 0)
    total = result.get("total_tokens", 0)
    prompt = result.get("prompt_tokens", 0)
    completion = result.get("completion_tokens", 0)
    text = (f"本次服务启动以来：LLM 调用 {calls} 次"
            + (f"（失败 {failed} 次）" if failed else "")
            + f"，共消耗 {total:,} tokens（输入 {prompt:,} + 输出 {completion:,}）。")
    return text


def _extract_batch_id(norm: str) -> str | None:
    for rx in _BATCH_STATUS_RES:
        m = rx.match(norm)
        if m:
            token = m.group(1)
            if token not in _NOT_BATCH_ID:
                return token
    return None


def _extract_health_batch_id(norm: str) -> str | None:
    for rx in _BATCH_HEALTH_RES:
        m = rx.match(norm)
        if m:
            token = m.group(1)
            if token not in _NOT_BATCH_ID:
                return token
    return None


def _fmt_batch_health(thread_id: str, result: dict) -> str:
    if result.get("error"):
        return f"没有找到批次「{thread_id}」，请检查批次号是否正确。"
    status = _STATUS_CN.get(result.get("status"),
                            result.get("status") or "未知")
    lines = [f"批次 {thread_id} 体检：{status}"]
    prog = result.get("progress")
    if isinstance(prog, dict) and prog.get("total"):
        lines.append(f"进度 {prog.get('done', 0)}/{prog['total']}")
    factories = result.get("factories") or []
    if factories:
        role_cn = {"done": "已完成", "pending": "待处理", "current": "处理中",
                   "skipped": "已跳过"}
        counts: dict[str, int] = {}
        for f in factories:
            counts[f.get("role") or "unknown"] = \
                counts.get(f.get("role") or "unknown", 0) + 1
        lines.append("工厂：" + "、".join(
            f"{role_cn.get(r, r)} {n} 家" for r, n in sorted(counts.items())))
        problematic = [f for f in factories if f.get("issues")]
        if problematic:
            lines.append("有问题的工厂：")
            for f in problematic[:5]:
                lines.append(
                    f"- {f.get('factory')}（{f['issues']} 条）："
                    f"{f.get('first_issue') or ''}")
    unprocessed = result.get("unprocessed_factories") or []
    if unprocessed:
        lines.append("未处理工厂：" + "、".join(str(f) for f in unprocessed))
    usage = result.get("usage") or {}
    if usage.get("calls"):
        lines.append(f"LLM 用量：{usage['calls']} 次调用，"
                     f"共 {usage.get('total_tokens', 0):,} tokens")
    return "\n".join(lines)


def try_fastpath(message: str) -> dict | None:
    """命中快路径意图 → {"tool", "args", "message"}；否则 None。

    只读工具在此直接调用（不经 LLM）；工具层按惯例返回 {"error"} 时
    格式化成自然语言兜底，绝不抛异常阻塞对话主流程。
    """
    from app.dispatcher.tools import TOOLS  # 延迟 import 避免环

    norm = _guarded(message)
    if norm is None:
        return None

    if _BATCH_LIST_RE.match(norm):
        result = TOOLS["list_batches"].func({})
        if result.get("error"):
            logger.warning("[快路径] list_batches 执行失败：%s", result["error"])
            return None
        return {"tool": "list_batches", "args": {},
                "message": _fmt_batch_list(result)}

    if _USAGE_RE.match(norm):
        result = TOOLS["get_usage"].func({})
        if result.get("error"):
            logger.warning("[快路径] get_usage 执行失败：%s", result["error"])
            return None
        return {"tool": "get_usage", "args": {},
                "message": _fmt_usage(result)}

    health_id = _extract_health_batch_id(norm)
    if health_id:
        result = TOOLS["batch_health"].func({"thread_id": health_id})
        return {"tool": "batch_health", "args": {"thread_id": health_id},
                "message": _fmt_batch_health(health_id, result)}

    batch_id = _extract_batch_id(norm)
    if batch_id:
        result = TOOLS["get_batch_status"].func({"thread_id": batch_id})
        # 批次不存在/查询异常不算快路径失败——返回友好文案（用户指令意图
        # 明确，回给 LLM 反而绕弯）
        return {"tool": "get_batch_status", "args": {"thread_id": batch_id},
                "message": _fmt_batch_status(batch_id, result)}

    return None


__all__ = ["try_fastpath"]
