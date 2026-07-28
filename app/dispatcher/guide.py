# -*- coding: utf-8 -*-
"""调度 Agent 操作指导工具：回答操作员"怎么用""为什么""最佳实践"类问题。

铁律落点：**回答内容只来自知识库 GUIDE_KB，LLM 只负责措辞润色**。
实现机制上把这条铁律做成了结构性约束——LLM 的输出契约只有
{"answer", "tone"} 两个字段，answer 的内容由知识库条目决定，
LLM 不能发明知识库之外的操作步骤或建议。

三部分设计：
1. 知识库 GUIDE_KB（模块级 dict）：覆盖 7+ 操作场景，每个条目包含
   keywords（关键词匹配用）、title、content、priority（数字越小越优先）；
   知识库检索不到时走通用模板（"暂无专门指引，可尝试……"）。
2. 上下文收集（纯代码）：如果提供 thread_id，调用 service.get_order_state
   拿当前批次状态（next_nodes / validation_status）；调用 service.list_batches
   拿批次总数；预算裁剪上下文 JSON 到 ~2000 字符内。
3. LLM 问答 + 模板降级：正常路径由 LLM 润色回答（json_mode, source_file="guide"）；
   GUIDE_MOCK=1 时跳过 LLM 直接走模板（确定性测试用）；
   LLM 调用异常或 JSON 解析失败 → 用命中的知识库条目 content 直接拼接回答
   （信息完整只是不润色），degraded=True——工具永不失败。
"""
from __future__ import annotations

import json
import os
from typing import Any

# ---------------------------------------------------------------------------
# 知识库 GUIDE_KB：代码侧唯一内容来源（LLM 不可见、不可改、不可发明）
# ---------------------------------------------------------------------------
# keywords：用于检索，question 包含任一 keyword 即命中；
# priority：数字越小越优先，排序后取前 3 条注入 LLM；
# content：回答的核心内容，LLM 只能润色措辞不能改变语义。
GUIDE_KB: dict[str, dict[str, Any]] = {
    "beginner_flow": {
        "keywords": ["怎么用", "流程", "步骤", "新手", "入门"],
        "title": "新手引导：完整操作流程",
        "content": "1. 配置路径（工厂文件夹/下游表/GT文件）\n2. 发起批次\n3. 人工审核\n4. 导出结果",
        "priority": 1,
    },
    "change_paths": {
        "keywords": ["改路径", "修改路径", "工厂文件夹"],
        "title": "如何修改路径配置",
        "content": "直接告诉我：'把工厂文件夹改到 /xxx'，我会解析→校验→预览→等你确认→写入 .env",
        "priority": 2,
    },
    "explain_error": {
        "keywords": ["错误", "失败", "为什么挂起", "什么意思"],
        "title": "如何理解批次错误",
        "content": "告诉我批次号，我调 explain_errors 给你详细解释（包含错误类型、影响范围、建议动作）",
        "priority": 2,
    },
    "best_practices": {
        "keywords": ["最佳实践", "建议", "习惯"],
        "title": "最佳实践",
        "content": "1. 每天下午 5 点检查待审核批次，避免积压\n2. 改路径前先备份 .env\n3. 失败批次及时重跑，不要拖到月底",
        "priority": 3,
    },
    "rerun_batch": {
        "keywords": ["重跑", "重新跑", "再跑一次"],
        "title": "如何重跑批次",
        "content": "告诉我：'重跑批次 ETD0725'，我会检查状态→预览→等你确认→从 Node1 重跑",
        "priority": 2,
    },
    "create_batch": {
        "keywords": ["发起", "新建", "创建", "开始", "新批次"],
        "title": "如何发起新批次",
        "content": "告诉我：'发起批次 ETD0725'（批次号必填），我会检查路径→预览→等你确认→从 Node1 跑到 Node5 挂起待审。缺省下stream/upstream 路径时用 .env 配置默认值。",
        "priority": 2,
    },
    "review_edit": {
        "keywords": ["改数", "审核", "修改 SKU"],
        "title": "如何修改审核数据",
        "content": "先调 get_review_payload 拿当前 items，告诉我哪个 SKU 改什么数值，我生成 diff 预览→确认→落库审计",
        "priority": 2,
    },
    "faq_common": {
        "keywords": ["常见问题", "FAQ", "为什么"],
        "title": "常见问题 FAQ",
        "content": "Q: 为什么批次挂起？A: Node5 人工审核点，需要操作员确认提取结果\nQ: 为什么提取失败？A: 可能是路径错误、文件质量问题、LLM 解析失败，用 explain_errors 查看",
        "priority": 3,
    },
}

# 通用模板（知识库检索不到时兜底）
_GENERIC_GUIDE: dict[str, Any] = {
    "title": "通用指引",
    "content": "暂无专门指引覆盖该问题。你可以尝试：\n1. 换个关键词重新提问\n2. 告诉我具体场景，我帮你定位到对应功能\n3. 联系管理员获取人工支持",
    "priority": 99,
}

# ---- 预算裁剪参数：上下文 JSON 控制在约 2000 字符内 ----
_CONTEXT_BUDGET_CHARS = 2000

# ---- LLM 系统提示词 ----
_GUIDE_PROMPT = r"""你是操作指导助手，服务于供应链单证提取流水线的操作员。
给你用户的问题、匹配到的知识库条目（kb_entries）、当前上下文（context）。
用操作员听得懂的中文回答。

铁律：
1. 回答内容必须基于知识库条目的 content，你只负责润色措辞、调整语气；
2. 不得编造知识库中不存在的操作步骤、功能名或路径；
3. 如果知识库条目不够回答用户问题，如实说明"目前指引未覆盖该场景"，
   不要自己发明答案；
4. 如果提供了上下文（当前批次状态等），适当结合上下文给出针对性回答；
5. tone 字段选择：日常操作用"friendly"，涉及错误/异常/严肃建议用"professional"。

只输出 JSON：
{"answer": "润色后的回答（基于知识库 content）",
 "tone": "friendly" | "professional"}"""


# ---------------------------------------------------------------------------
# 第一部分：知识库检索（纯代码，按 keywords 匹配 + priority 排序）
# ---------------------------------------------------------------------------

def _search_kb(question: str, top_k: int = 3) -> list[tuple[str, dict]]:
    """按关键词匹配检索知识库，返回命中的 (key, entry) 列表，按 priority 排序取前 top_k。

    匹配规则：question 包含 entry.keywords 中任一 keyword 即命中（大小写不敏感）。
    未命中任何条目时返回包含通用模板的列表（保证永远有内容可回答）。
    """
    q = question.lower()
    hits: list[tuple[str, dict, int]] = []
    for key, entry in GUIDE_KB.items():
        for kw in entry.get("keywords", []):
            if kw.lower() in q:
                hits.append((key, entry, entry.get("priority", 99)))
                break  # 同一条目命中一个 keyword 即可，不重复计入

    # 按 priority 升序排序（数字越小越优先），取前 top_k
    hits.sort(key=lambda x: x[2])
    top_hits = [(key, entry) for key, entry, _ in hits[:top_k]]

    # 兜底：完全没命中 → 返回通用模板
    if not top_hits:
        top_hits = [("_generic", _GENERIC_GUIDE)]

    return top_hits


# ---------------------------------------------------------------------------
# 第二部分：上下文收集（纯代码）
# ---------------------------------------------------------------------------

def _collect_context(thread_id: str | None) -> dict[str, Any]:
    """收集当前上下文（批次状态、工厂信息等），供 LLM 理解场景。

    - thread_id 非空：调用 service.get_order_state 拿当前批次状态
      （next_nodes / validation_status / current_factory_data）；
    - 始终调用 service.list_batches 拿批次总数；
    - 预算裁剪：JSON 控制在 ~2000 字符内。
    """
    from app.api import service  # 延迟 import 避免环（service 依赖 graph 单例）

    ctx: dict[str, Any] = {}

    # ---- 批次列表概要 ----
    try:
        batches = service.list_batches()
        batch_list = batches.get("batches") or batches.get("items") or []
        ctx["batch_total"] = len(batch_list)
        # 只保留最近 5 条的摘要（thread_id + status）
        ctx["recent_batches"] = [
            {"thread_id": b.get("thread_id"), "status": b.get("status")}
            for b in batch_list[:5]
        ]
    except Exception:  # noqa: BLE001 批次列表读取失败不阻塞上下文收集
        ctx["batch_total"] = -1
        ctx["recent_batches"] = []

    # ---- 当前批次状态（仅 thread_id 非空时） ----
    if thread_id:
        try:
            state = service.get_order_state(thread_id)
            if state.get("exists"):
                values = state.get("values") or {}
                ctx["current_thread"] = {
                    "thread_id": thread_id,
                    "next_nodes": values.get("next_nodes"),
                    "validation_status": values.get("validation_status"),
                }
                # 当前工厂名（如有）
                cur_factory = values.get("current_factory_data") or {}
                if cur_factory.get("factory_name"):
                    ctx["current_thread"]["factory_name"] = cur_factory["factory_name"]
            else:
                ctx["current_thread"] = {"thread_id": thread_id, "exists": False}
        except Exception:  # noqa: BLE001 state 读取失败不阻塞其他上下文
            ctx["current_thread"] = {"thread_id": thread_id, "error": "读取失败"}

    # ---- 预算裁剪：JSON 控制在 ~2000 字符内 ----
    ctx_json = json.dumps(ctx, ensure_ascii=False)
    if len(ctx_json) > _CONTEXT_BUDGET_CHARS:
        # 先丢 recent_batches 的详情，只保留总数
        ctx.pop("recent_batches", None)
        ctx_json = json.dumps(ctx, ensure_ascii=False)
    if len(ctx_json) > _CONTEXT_BUDGET_CHARS:
        # 再裁剪 current_thread 的非关键字段
        ct = ctx.get("current_thread")
        if isinstance(ct, dict):
            for k in list(ct.keys()):
                if k not in ("thread_id", "exists"):
                    ct.pop(k, None)

    return ctx


# ---------------------------------------------------------------------------
# 第三部分：LLM 问答 + 模板降级
# ---------------------------------------------------------------------------

def _build_kb_payload(hits: list[tuple[str, dict]]) -> list[dict]:
    """把命中的知识库条目组装成注入 LLM 的 payload 列表。"""
    return [
        {"key": key, "title": entry["title"], "content": entry["content"]}
        for key, entry in hits
    ]


def _template_answer(hits: list[tuple[str, dict]], question: str) -> str:
    """模板降级：用命中的知识库条目 content 直接拼接回答（信息完整只是不润色）。

    格式：先列出命中的指引标题和对应内容，多条时逐条列出。
    """
    parts: list[str] = []
    for key, entry in hits:
        title = entry["title"]
        content = entry["content"]
        parts.append(f"【{title}】\n{content}")
    return "\n\n".join(parts)


def _llm_answer(question: str, hits: list[tuple[str, dict]],
                context: dict[str, Any]) -> dict | None:
    """LLM 润色回答 → {"answer", "tone"}；任何失败返回 None（走模板降级）。

    输出白名单强过滤：answer 只收非空字符串；tone 只收 "friendly" / "professional"。
    """
    from app.extraction import llm_client  # 延迟 import：无 API key 时模板降级仍可用

    kb_payload = _build_kb_payload(hits)
    user_payload = {
        "question": question,
        "kb_entries": kb_payload,
        "context": context,
    }
    messages = [
        {"role": "system", "content": _GUIDE_PROMPT},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    try:
        raw = llm_client.chat_completion(
            messages,
            json_mode=True,
            source_file="guide",
            max_tokens=1024,
        )
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 LLM 故障/JSON 解析失败 → 模板降级
        return None
    if not isinstance(parsed, dict):
        return None

    answer = parsed.get("answer")
    tone = parsed.get("tone")
    # 白名单过滤
    if not isinstance(answer, str) or not answer.strip():
        return None
    if tone not in ("friendly", "professional"):
        tone = "friendly"
    return {
        "answer": answer.strip(),
        "tone": tone,
    }


# ---------------------------------------------------------------------------
# 工具入口（tools.py 按此契约调用，签名一字不差）
# ---------------------------------------------------------------------------

def ask_guide(question: str, thread_id: str | None = None) -> dict:
    """回答操作员"怎么用""为什么""最佳实践"类问题。

    返回结构：
    {
        "answer": str,              # 人话回答
        "references": list[str],    # 引用的知识库条目 key（便于追溯）
        "context": dict,            # 收集到的上下文（当前批次状态等）
    }

    - 知识库检索不到时走通用模板（不抛异常）；
    - LLM 故障 / JSON 解析失败 / GUIDE_MOCK=1 → 模板降级（degraded=True），
      回答内容由知识库 content 直接拼接，工具永不失败。
    """
    # ---- 知识库检索 ----
    hits = _search_kb(question, top_k=3)
    references = [key for key, _ in hits]

    # ---- 上下文收集 ----
    context = _collect_context(thread_id)

    # ---- LLM 润色 or 模板降级 ----
    llm_out: dict | None = None
    if os.environ.get("GUIDE_MOCK", "").strip() == "1":
        degraded = True  # 测试通道：跳过 LLM 直接走模板（确定性输出）
    else:
        llm_out = _llm_answer(question, hits, context)
        degraded = llm_out is None

    answer = (llm_out or {}).get("answer") or _template_answer(hits, question)

    result: dict[str, Any] = {
        "answer": answer,
        "references": references,
        "context": context,
    }
    if degraded:
        result["degraded"] = True

    return result


__all__ = ["ask_guide", "GUIDE_KB"]
