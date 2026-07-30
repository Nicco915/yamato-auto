# -*- coding: utf-8 -*-
"""批次错误翻译工具（规则表 + LLM 翻译）。

知识库检索接口 _search_issue_kb 双后端（V1/V2 共存，对外签名不变）：
- V1（默认）：硬编码 ISSUE_KB 映射表，按 type 精确查表
- V2：已知 type 仍走精确查表（确定性场景不需要向量）；**未知 type**
  用向量检索 issue namespace 找最近邻条目（KB_BACKEND=pinecone，
  见 agent设计/rag设计.md），未命中或任何失败回落通用模板
替换/升级只需改 _search_issue_kb 内部实现，外部调用不变。

铁律落点：**建议动作只来自代码规则表 ISSUE_KB，LLM 禁止发明动作**。
实现机制上把这条铁律做成了结构性约束——LLM 的输出契约只有
{"summary", "cause_notes"} 两个自由文本字段，causes/suggestions 的结构
完全由代码组装，LLM 就算幻觉也无处注入动作。

三部分设计：
1. 数据收集（纯代码）：从 service 三个只读入口汇总批次的异常素材——
   state（extraction_issues / missing_skus / 异常 calculated_items）、
   工厂会话（issues / deferred / 异常身份的 file_records）、
   挂起时的 review payload 补充；thread 不存在返回 {"error": ...} 不抛异常。
2. 规则表 ISSUE_KB（代码侧唯一决策来源）：issue type →
   {title, explain, severity, suggest[]}；MISSING_SKUS / CALC_ERROR 是
   代码合成类型（分别对应 missing_skus 非空、calculated_items 异常）；
   未收录 type 走通用模板（severity=mid，建议=人工核查）。
3. LLM 翻译 + 模板降级：正常路径由 LLM 产出 summary 与每类补充说明；
   LLM 调用异常或 JSON 解析失败时退回 ISSUE_KB 模板句式拼装
   （信息完整只是不润色），degraded=True——工具永不失败。
   EXPLAIN_MOCK=1 时跳过 LLM 直接走模板（确定性测试用，同样 degraded=True）。
"""
from __future__ import annotations

import json
import os
from typing import Any

# ---------------------------------------------------------------------------
# 规则表：代码侧唯一决策来源（LLM 不可见、不可改、不可发明）
# ---------------------------------------------------------------------------
# suggest 条目结构：{"action", "label", "tool", "args_hint"}；
# tool 为调度 Agent 的工具名（None=人工线下动作）；args_hint 中的运行时值
# （如 thread_id）在 _build_suggestions 组装时填充，规则表本身保持静态。
ISSUE_KB: dict[str, dict[str, Any]] = {
    "NO_PACKING_LIST": {
        "title": "缺少装箱单",
        "explain": "该工厂的单据里还没有收到可用的 SKU 级装箱单，"
                   "目前收到的只有疑似报关/汇总类文件，提取流水线按规则不直接采用。",
        "severity": "high",
        "suggest": [
            {"action": "manual_entry", "label": "人工补录该工厂装箱数据",
             "tool": None, "args_hint": {}},
            {"action": "check_paths", "label": "检查工厂文件夹路径配置是否正确",
             "tool": "set_paths", "args_hint": {}},
        ],
    },
    "CHANNEL_ERROR": {
        "title": "提取通道处理失败",
        "explain": "识别器在读取/提取目标文件时出错（解析异常或通道报错），"
                   "该文件的 SKU 数据可能未提取或提取不完整。",
        "severity": "high",
        "suggest": [
            {"action": "rerun_batch", "label": "重跑当前批次",
             "tool": "rerun", "args_hint": {}},  # thread_id 运行时填充
            {"action": "manual_check_file", "label": "人工打开出错文件确认内容是否正常",
             "tool": None, "args_hint": {}},
        ],
    },
    "TARGET_EMPTY": {
        "title": "目标文件提取为空",
        "explain": "被选为装箱单的目标文件提取结果为空——可能是误识别"
                   "（它不是真正的箱单）或文件本身异常，需要人工确认。",
        "severity": "high",
        "suggest": [
            {"action": "manual_check_file", "label": "人工确认目标文件是否为有效箱单",
             "tool": None, "args_hint": {}},
            {"action": "rerun_batch", "label": "确认文件后重跑当前批次",
             "tool": "rerun", "args_hint": {}},
        ],
    },
    "UNSUPPORTED_FILE_TYPE": {
        "title": "文件类型不支持",
        "explain": "有特殊类型文件，现有识别工具无法调取其内容；"
                   "如果它是装箱单，需要人工处理或扩展新的提取通道。",
        "severity": "mid",
        "suggest": [
            {"action": "manual_handle_file", "label": "人工处理该文件（转格式或人工补录）",
             "tool": None, "args_hint": {}},
        ],
    },
    "IMAGE_FILE": {
        "title": "图片文件无法扫描",
        "explain": "收到的是图片（或图片型 PDF），识别器无法扫描其内容；"
                   "如果它是装箱单，需人工指出后走视觉通道或人工补录。",
        "severity": "mid",
        "suggest": [
            {"action": "manual_handle_file", "label": "人工确认该图片并处理（视觉提取或补录）",
             "tool": None, "args_hint": {}},
        ],
    },
    "FORCE_EXTRACT_FAILED": {
        "title": "强制提取失败",
        "explain": "人工指定的文件在强制提取时仍然失败，"
                   "该文件可能损坏或内容格式超出通道能力。",
        "severity": "high",
        "suggest": [
            {"action": "manual_handle_file", "label": "人工检查该文件并线下补录数据",
             "tool": None, "args_hint": {}},
        ],
    },
    "SKU_REVISED": {
        "title": "SKU 改单覆盖",
        "explain": "同一 SKU 的新文件数值与此前提取结果不同，已按新值覆盖，"
                   "需要人工确认改单是否有效。",
        "severity": "mid",
        "suggest": [
            {"action": "submit_review", "label": "在审核界面确认改单数值",
             "tool": "submit_review", "args_hint": {}},
        ],
    },
    # ---- 代码合成类型（非 session issue，由 state 数据推导）----
    "MISSING_SKUS": {
        "title": "有 SKU 未提取到",
        "explain": "下游装箱表要求的部分 SKU 在提取结果中缺失，"
                   "相应箱单可能未到、未被识别为候选，或在暂缓文件中。",
        "severity": "high",
        "suggest": [
            {"action": "manual_entry", "label": "人工补录缺失 SKU 的数据",
             "tool": None, "args_hint": {}},
        ],
    },
    "CALC_ERROR": {
        "title": "计算/校验异常",
        "explain": "部分 SKU 在数量重量计算或合规校验中出现异常"
                   "（如单重与库内基准偏差过大、缺少基准数据），数值不可直接采用。",
        "severity": "high",
        "suggest": [
            {"action": "submit_review", "label": "在审核界面人工修正后提交",
             "tool": "submit_review", "args_hint": {}},
        ],
    },
}

# 未收录 type 的通用模板（severity=mid，建议=人工核查）
_GENERIC_KB: dict[str, Any] = {
    "title": "其他提取反馈",
    "explain": "提取过程中产生了一类未预设的反馈，需要人工核查具体情况。",
    "severity": "mid",
    "suggest": [
        {"action": "manual_review", "label": "人工核对该批次提取反馈",
         "tool": None, "args_hint": {}},
    ],
}

# severity 排序权重（causes 高严重度在前）
_SEVERITY_ORDER = {"high": 0, "mid": 1, "low": 2}

# ---- 预算裁剪参数：注入 LLM 的素材 JSON 控制在约 4000 字符内 ----
_LLM_BUDGET_CHARS = 4000
_MSG_MAX_CHARS = 200        # 单条 issue message 截断长度
_ERROR_ITEMS_MAX = 20       # 异常 SKU 条目上限
_FILE_RECORDS_MAX = 10      # 异常身份文件上限

# 普通登记角色（非异常），收集 file_records 时过滤掉
_NORMAL_FILE_ROLES = {"non_target", "duplicate", "subset"}

_EXPLAIN_PROMPT = r"""你是提取错误翻译官，服务于供应链单证提取流水线。
给你一批次的结构化错误数据和错误类型词典（type_dict），
请用操作员听得懂的中文解释：发生了什么、影响哪些 SKU/文件。

铁律：
1. 只做措辞翻译，禁止给出任何处理建议或动作（动作由系统规则表决定）；
2. 不得编造素材中不存在的 SKU、文件名或数字；
3. cause_notes 的键必须是素材中实际出现的 issue 类型，只补充该类型
   在本批次的具体情况（涉及哪些文件/SKU），不要复述词典原文。

只输出 JSON：
{"summary": "一段给人看的总述（2-4 句）",
 "cause_notes": {"<type>": "针对该类型本批次具体情况的补充说明"}}"""


# ---------------------------------------------------------------------------
# 第一部分：数据收集（纯代码）
# ---------------------------------------------------------------------------

def _trim_issue(issue: dict) -> dict:
    """issue 裁剪：只留解释所需字段，message 截断到 200 字符。"""
    return {
        "level": issue.get("level"),
        "type": issue.get("type") or "UNKNOWN",
        "message": str(issue.get("message") or "")[:_MSG_MAX_CHARS],
        "file": issue.get("file") or "",
    }


def _trim_error_item(item: dict) -> dict:
    """异常 calculated_items 条目：只取 sku/status/error_msg/review_reason。"""
    return {
        "sku": item.get("sku"),
        "status": item.get("status"),
        "error_msg": item.get("error_msg"),
        "review_reason": item.get("review_reason"),
    }


def _is_error_item(item: dict) -> bool:
    """calculated_items 异常判定：Error 状态，或带 review_reason / unexpected_sku。"""
    return bool(
        item.get("status") == "Error"
        or item.get("review_reason")
        or item.get("unexpected_sku")
    )


def _merge_issue(material: dict, issue: dict) -> None:
    """issue 去重合并（state 与会话文件两处来源可能重复）。"""
    key = (issue["type"], issue["message"], issue["file"])
    if key not in material["_seen_issues"]:
        material["_seen_issues"].add(key)
        material["issues"].append(issue)


def _collect_material(thread_id: str, factory: str | None) -> dict | None:
    """汇总批次异常素材；thread 不存在返回 None（由入口转成 error dict）。

    三个来源：
    - get_order_state → current_factory_data（仅当前工厂）：
      extraction_issues / extraction_coverage / missing_skus / 异常 calculated_items；
    - get_batch_detail → factories[].session：
      issues / deferred（含 reason）/ 异常身份的 file_records / coverage；
    - 挂起时 get_review_payload → extraction_issues / missing_skus / 异常 items 补充。
    factory 缺省时 = 当前工厂（state 数据）+ 全部有异常的工厂（会话数据）。
    """
    from app.api import service  # 延迟 import 避免环（service 依赖 graph 单例）

    state = service.get_order_state(thread_id)
    if not state.get("exists"):
        return None

    material: dict[str, Any] = {
        "issues": [],           # 裁剪+去重后的 issue 列表
        "missing_skus": [],     # 下游要求但未提取到的 SKU
        "error_items": [],      # 异常 calculated_items（裁剪后）
        "deferred": [],         # 暂缓文件 {path, reason}
        "file_records": {},     # 异常身份文件 path → {role, note}
        "current_factory": None,
        "_seen_issues": set(),  # 去重索引（不进 LLM 素材）
        "_seen_skus": set(),
    }

    values = state.get("values") or {}

    # ---- 来源 1：state 里的当前工厂数据 ----
    cur = values.get("current_factory_data") or {}
    cur_name = cur.get("factory_name")
    material["current_factory"] = cur_name
    if cur and (factory is None or factory == cur_name):
        for issue in cur.get("extraction_issues") or []:
            _merge_issue(material, _trim_issue(issue))
        _merge_missing(material, cur.get("missing_skus") or [])
        cov_missing = (cur.get("extraction_coverage") or {}).get("missing") or []
        _merge_missing(material, cov_missing)
        for item in (cur.get("calculated_items") or []):
            if _is_error_item(item):
                _merge_error_item(material, _trim_error_item(item))

    # ---- 来源 2：批次详情里的工厂会话 ----
    # get_batch_detail 对不存在 thread 抛 ValueError；上面已判存在，
    # 这里仍兜底（竞态：checkpointer 刚被清理等），不拖垮工具
    try:
        detail = service.get_batch_detail(thread_id)
    except Exception:  # noqa: BLE001 会话层故障不阻塞 state 层素材
        detail = None
    for f in (detail or {}).get("factories") or []:
        name = f.get("factory")
        if factory is not None and name != factory:
            continue
        sess = f.get("session")
        if not sess:
            continue
        # factory 缺省时只收有异常的工厂（issues 或暂缓文件非空）
        if factory is None and name != cur_name \
                and not (sess.get("issues") or sess.get("deferred")):
            continue
        for issue in sess.get("issues") or []:
            _merge_issue(material, _trim_issue(issue))
        for d in sess.get("deferred") or []:
            material["deferred"].append({
                "path": d.get("path"), "reason": d.get("reason"),
            })
        for path, rec in (sess.get("file_records") or {}).items():
            if rec.get("role") not in _NORMAL_FILE_ROLES:
                material["file_records"][path] = {
                    "role": rec.get("role"),
                    "note": str(rec.get("note") or "")[:_MSG_MAX_CHARS],
                }
        cov = sess.get("coverage") or {}
        _merge_missing(material, cov.get("missing") or [])

    # ---- 来源 3：挂起时的 review payload 补充 ----
    try:
        payload = service.get_review_payload(thread_id)
    except Exception:  # noqa: BLE001 payload 读取失败不阻塞已有素材
        payload = None
    if isinstance(payload, dict) \
            and (factory is None or payload.get("factory_name") == factory):
        for issue in payload.get("extraction_issues") or []:
            _merge_issue(material, _trim_issue(issue))
        _merge_missing(material, payload.get("missing_skus") or [])
        for item in payload.get("items") or []:
            if _is_error_item(item):
                _merge_error_item(material, _trim_error_item(item))

    return material


def _merge_missing(material: dict, skus: list[str]) -> None:
    """missing_skus 去重合并（state / 会话 coverage / payload 三处来源）。"""
    for sku in skus:
        if sku and sku not in material["_seen_skus"]:
            material["_seen_skus"].add(sku)
            material["missing_skus"].append(sku)


def _merge_error_item(material: dict, entry: dict) -> None:
    """异常 SKU 条目按 sku 去重（payload 与 state 可能重复）。"""
    key = entry.get("sku")
    if any(e.get("sku") == key for e in material["error_items"]):
        return
    material["error_items"].append(entry)


# ---------------------------------------------------------------------------
# 第二部分：知识库检索接口（V1 硬编码映射，V2 替换为 RAG）
# ---------------------------------------------------------------------------

def _issue_entry_from_metadata(md: dict) -> dict:
    """把 Pinecone metadata 还原为 ISSUE_KB 条目结构（RAG 命中未知 type 时用）。"""
    try:
        suggest = json.loads(md.get("suggest_json") or "[]")
    except (TypeError, ValueError):
        suggest = []
    return {
        "title": md.get("title", ""),
        "explain": md.get("explain", ""),
        "severity": md.get("severity", "mid"),
        "suggest": suggest,
    }


def _search_issue_kb(issue_types: list[str]) -> dict[str, dict]:
    """检索错误案例知识库（V1 精确查表 / V2 未知 type 向量检索）。

    根据问题类型列表检索匹配的知识库条目。返回按 type 索引的字典，
    每个条目包含 {title, explain, severity, suggest}。

    已知 type：走硬编码 ISSUE_KB 精确映射（确定性场景不需要向量）。
    未知 type：KB_BACKEND=pinecone 时用 type 文本查 issue namespace，
    命中（score ≥ RAG_MIN_SCORE）采用最近邻条目；未命中或任何失败
    走通用模板 _GENERIC_KB（severity=mid，建议=人工核查）。

    返回 dict[type, entry] 供 _build_causes / _build_suggestions / _llm_payload 使用。
    """
    from app.dispatcher import rag  # 延迟 import：无 key 环境不影响 V1

    use_rag = rag.backend_enabled()
    kb_map: dict[str, dict[str, Any]] = {}
    for type_ in issue_types:
        if type_ in ISSUE_KB:
            kb_map[type_] = ISSUE_KB[type_]
            continue
        entry: dict[str, Any] | None = None
        if use_rag:
            hits = rag.query_namespace("issue", type_, top_k=1)
            if hits:
                entry = _issue_entry_from_metadata(hits[0]["metadata"])
        kb_map[type_] = entry if entry is not None else _GENERIC_KB
        if entry is None:
            rag.log_curation(type_, source="issue")
    return kb_map


# ---------------------------------------------------------------------------
# 第三部分：规则表组装（causes / suggestions 的唯一来源）
# ---------------------------------------------------------------------------

def _build_causes(material: dict, cause_notes: dict[str, str],
                  degraded: bool) -> list[dict]:
    """按 issue type 分组，用 _search_issue_kb 检索的条目组装 causes。

    MISSING_SKUS / CALC_ERROR 由 state 数据合成；未收录 type 走通用模板。
    返回按 severity 排序（high → mid → low）的列表。
    """
    groups: dict[str, list[dict]] = {}
    for issue in material["issues"]:
        groups.setdefault(issue["type"], []).append(issue)

    # 收集所有 issue type（含合成类型），一次性检索知识库
    all_types = list(groups.keys())
    if material["missing_skus"]:
        all_types.append("MISSING_SKUS")
    if material["error_items"]:
        all_types.append("CALC_ERROR")
    kb_map = _search_issue_kb(all_types)

    causes: list[dict] = []
    for type_, issues in groups.items():
        kb = kb_map[type_]
        explanation = kb["explain"]
        note = cause_notes.get(type_)
        if note:
            # LLM 只补充本批次具体情况的说明，附加在规则表解释之后
            explanation = f"{explanation} {note}"
        elif degraded:
            # 模板降级：补充本批次命中次数，保证信息完整
            explanation = f"{explanation}（本批次共 {len(issues)} 条相关反馈）"
        causes.append({
            "type": type_,
            # RAG 命中的未知 type 用检索到的条目标题；通用模板才拼 type 后缀
            "title": kb["title"] if kb is not _GENERIC_KB else f"{_GENERIC_KB['title']}（{type_}）",
            "explanation": explanation,
            "evidence_files": sorted({i["file"] for i in issues if i["file"]}),
            "affected_skus": [],
            "severity": kb["severity"],
        })

    # ---- 合成类型：MISSING_SKUS ----
    if material["missing_skus"]:
        kb = kb_map["MISSING_SKUS"]
        explanation = kb["explain"]
        note = cause_notes.get("MISSING_SKUS")
        explanation = f"{explanation} {note}" if note else explanation
        causes.append({
            "type": "MISSING_SKUS",
            "title": kb["title"],
            "explanation": explanation,
            "evidence_files": [],
            "affected_skus": list(material["missing_skus"]),
            "severity": kb["severity"],
        })

    # ---- 合成类型：CALC_ERROR ----
    if material["error_items"]:
        kb = kb_map["CALC_ERROR"]
        explanation = kb["explain"]
        note = cause_notes.get("CALC_ERROR")
        explanation = f"{explanation} {note}" if note else explanation
        causes.append({
            "type": "CALC_ERROR",
            "title": kb["title"],
            "explanation": explanation,
            "evidence_files": [],
            "affected_skus": [e.get("sku") for e in material["error_items"] if e.get("sku")],
            "severity": kb["severity"],
        })

    causes.sort(key=lambda c: (_SEVERITY_ORDER.get(c["severity"], 1), c["type"]))
    return causes


def _build_suggestions(causes: list[dict], thread_id: str) -> list[dict]:
    """从 _search_issue_kb 匹配的条目汇总建议动作，按 action 去重。

    运行时填充：tool="rerun" 的建议补上 thread_id。
    顺序跟随 causes（高严重度类型的建议在前）。
    """
    # 一次性检索知识库（与 _build_causes 同一接口）
    kb_map = _search_issue_kb([c["type"] for c in causes])

    suggestions: list[dict] = []
    seen: set[str] = set()
    for cause in causes:
        kb = kb_map.get(cause["type"], _GENERIC_KB)
        for sug in kb["suggest"]:
            if sug["action"] in seen:
                continue
            seen.add(sug["action"])
            args_hint = dict(sug["args_hint"])
            if sug["tool"] == "rerun":
                args_hint["thread_id"] = thread_id
            suggestions.append({
                "action": sug["action"],
                "label": sug["label"],
                "tool": sug["tool"],
                "args_hint": args_hint,
            })
    return suggestions


def _template_summary(material: dict, causes: list[dict]) -> str:
    """模板降级 summary：规则表句式拼装，信息完整但不润色。"""
    titles = "、".join(c["title"] for c in causes)
    parts = [f"本批次共 {len(material['issues'])} 条提取反馈，涉及 {len(causes)} 类问题：{titles}。"]
    if material["missing_skus"]:
        parts.append(f"有 {len(material['missing_skus'])} 个 SKU 未提取到。")
    if material["error_items"]:
        parts.append(f"有 {len(material['error_items'])} 个 SKU 计算/校验异常。")
    parts.append("具体原因与建议动作见下方列表。")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 第三部分：LLM 翻译（只产出 summary + cause_notes，结构由代码组装）
# ---------------------------------------------------------------------------

def _llm_payload(material: dict, factory: str | None) -> dict:
    """组装注入 LLM 的素材（含类型词典），并按预算逐级裁剪。"""
    present_types = sorted({i["type"] for i in material["issues"]})
    if material["missing_skus"]:
        present_types.append("MISSING_SKUS")
    if material["error_items"]:
        present_types.append("CALC_ERROR")
    payload: dict[str, Any] = {
        "factory": factory or material["current_factory"],
        "issues": material["issues"],
        "missing_skus": material["missing_skus"],
        "error_items": material["error_items"][:_ERROR_ITEMS_MAX],
        "deferred": material["deferred"],
        "file_records": dict(list(material["file_records"].items())[:_FILE_RECORDS_MAX]),
        # 类型词典：让 LLM 知道每类含义，但不包含 suggest（动作不进 LLM 视野）
        # 通过 _search_issue_kb 接口检索（V1 硬编码，V2 向量库）
        "type_dict": {
            t: {"title": kb["title"], "explain": kb["explain"]}
            for t, kb in _search_issue_kb(present_types).items()
        },
    }

    # 预算裁剪（约 4000 字符）：先丢 file_records/deferred 辅助信息，
    # 再砍 error_items 到上限，最后把 issue message 二次截断；issues 保持全量
    def _size() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    if _size() > _LLM_BUDGET_CHARS:
        payload["file_records"] = {}
    if _size() > _LLM_BUDGET_CHARS:
        payload["deferred"] = payload["deferred"][:5]
    if _size() > _LLM_BUDGET_CHARS:
        for issue in payload["issues"]:
            issue["message"] = issue["message"][:80]
    return payload


def _llm_translate(material: dict, factory: str | None) -> dict | None:
    """LLM 翻译 → {"summary", "cause_notes"}；任何失败返回 None（走模板降级）。

    输出白名单强过滤：summary 只收字符串；cause_notes 只收
    「素材中实际出现的 type → 字符串说明」，LLM 发明的 type 一律丢弃。
    """
    from app.extraction import llm_client  # 延迟 import：无 API key 时模板降级仍可用

    payload = _llm_payload(material, factory)
    messages = [
        {"role": "system", "content": _EXPLAIN_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = llm_client.chat_completion(
            messages,
            json_mode=True,
            source_file="explain_errors",
            max_tokens=2048,
        )
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 LLM 故障/JSON 解析失败 → 模板降级
        return None
    if not isinstance(parsed, dict):
        return None

    summary = parsed.get("summary")
    notes = parsed.get("cause_notes")
    known_types = set(payload["type_dict"])
    cause_notes = {
        k: str(v) for k, v in (notes or {}).items()
        if isinstance(notes, dict) and k in known_types and isinstance(v, str) and v.strip()
    } if isinstance(notes, dict) else {}
    return {
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
        "cause_notes": cause_notes,
    }


# ---------------------------------------------------------------------------
# 工具入口（tools.py 按此契约调用，签名勿动）
# ---------------------------------------------------------------------------

def explain_errors(thread_id: str, factory: str | None = None) -> dict:
    """把批次提取错误翻译成人话：结构化原因 + 规则表建议动作。

    - thread 不存在 → {"error": f"批次不存在: {thread_id}"}（dict，不抛异常）；
    - 建议动作只来自 ISSUE_KB 规则表，LLM 从机制上无法发明动作；
    - 知识库检索通过 _search_issue_kb 接口（V1 硬编码映射，V2 可替换为 RAG）；
    - LLM 故障 / JSON 解析失败 / EXPLAIN_MOCK=1 → 模板降级（degraded=True），
      工具永不失败。
    """
    material = _collect_material(thread_id, factory)
    if material is None:
        return {"error": f"批次不存在: {thread_id}"}

    raw = {
        "issue_count": len(material["issues"]),
        "error_skus": len(material["error_items"]),
        "missing_skus": list(material["missing_skus"]),
    }

    # 完全无异常：不调 LLM，直接返回（非降级）
    if not (material["issues"] or material["missing_skus"] or material["error_items"]):
        return {
            "thread_id": thread_id,
            "summary": "本批次未发现提取异常，所有 SKU 提取与校验均正常。",
            "causes": [],
            "suggestions": [],
            "raw": raw,
            "degraded": False,
        }

    # ---- LLM 翻译 or 模板降级 ----
    llm_out: dict | None = None
    if os.environ.get("EXPLAIN_MOCK", "").strip() == "1":
        degraded = True  # 测试通道：跳过 LLM 直接走模板（确定性输出）
    else:
        llm_out = _llm_translate(material, factory)
        degraded = llm_out is None

    cause_notes = (llm_out or {}).get("cause_notes") or {}
    causes = _build_causes(material, cause_notes, degraded)
    suggestions = _build_suggestions(causes, thread_id)
    summary = (llm_out or {}).get("summary") or _template_summary(material, causes)

    return {
        "thread_id": thread_id,
        "summary": summary,
        "causes": causes,
        "suggestions": suggestions,
        "raw": raw,
        "degraded": degraded,
    }


__all__ = ["explain_errors", "ISSUE_KB"]
