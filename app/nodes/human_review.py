"""Node 5: 人工审核中断点（Human-in-the-Loop Node）。

核心安全阀。interrupt() 挂起整个图，向外部抛出《第一阶段.md》第 6 节
结构的审核 payload；人类确认/修改后通过 Command(resume=...) 注入数据，
本节点用人类数据【强制覆写】state。

resume 数据约定：
{
  "approved": true/false,
  "items": [ ... 与 payload 中 items 同构（可修改 extracted_data 数值，
             新 SKU 必须补齐 name_cn / hs_code / inspection_required） ... ]
}
"""
import json
import logging
from pathlib import Path

from langgraph.types import interrupt

from app.config import get_settings
from app.logging_config import bind_factory_from_state
from app.nodes.compute_align import _safe_div
from app.state import AgentState

logger = logging.getLogger(__name__)

# 工厂商检默认值配置（从 JSON 加载，未配置的工厂默认 0）
_FACTORY_INSPECTION_CACHE: dict[str, int] | None = None

def _load_factory_inspection_defaults() -> dict[str, int]:
    """加载 factory_inspection_defaults.json，缓存到模块级变量。"""
    global _FACTORY_INSPECTION_CACHE
    if _FACTORY_INSPECTION_CACHE is not None:
        return _FACTORY_INSPECTION_CACHE
    config_path = Path(__file__).parents[1] / "config" / "factory_inspection_defaults.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            _FACTORY_INSPECTION_CACHE = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("[Node5] 加载工厂商检配置失败，使用默认值: %s", e)
        _FACTORY_INSPECTION_CACHE = {}
    return _FACTORY_INSPECTION_CACHE

# 新 SKU 需人工补录的合规字段清单（《第三阶段.md》改造点 B）
NEW_SKU_REQUIRED_FIELDS = ["name_cn", "hs_code", "inspection_required"]


def _norm_text(v):
    """文本合规字段归一化：strip 后为空视为 None（与 Node6 not-None 才写的语义对齐）。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_bool(v):
    """商检字段归一化：审核页文本框提交的是字符串，"0"/"false" 直接 bool() 会变 True。"""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "是"):
        return True
    if s in ("0", "false", "no", "否"):
        return False
    return None  # 无法识别视为未填写，不触发更新


def human_review(state: AgentState) -> dict:
    # L2 日志关联：从 state 重绑当前工厂名（多工厂 resume 链上 submit 拷贝的
    # context 残留上一工厂）；interrupt 恢复后节点从头重跑，入口绑定对
    # 挂起前/恢复后两程日志都生效；节点独立 context 保证不外泄，无需清理
    bind_factory_from_state(state)
    cur = dict(state.get("current_factory_data") or {})

    # ---- 构建审核负载（第一阶段.md 第 6 节结构）----
    factory_name = cur.get("factory_name")
    inspection_defaults = _load_factory_inspection_defaults()
    default_inspection = inspection_defaults.get(factory_name, 0)

    items_payload = []
    for item in cur.get("calculated_items") or []:
        entry = {
            "sku": item.get("sku"),
            "extracted_data": item.get("extracted_data"),
            "calculation": item.get("calculation"),
            "status": item.get("status"),
            "is_human_edited": item.get("is_human_edited", False),
            "is_new_sku": item.get("is_new_sku", False),
            "db_record": item.get("db_record") or {},
            # 透传 Node4 的异常详情，让前端能向操作员解释 Error/Warning 原因
            "error_msg": item.get("error_msg"),
            "unexpected_sku": item.get("unexpected_sku", False),
        }
        if item.get("is_new_sku"):
            # 新 SKU：前端展示需补录字段，inspection_required 按工厂配置设默认值
            entry["fields_to_fill"] = NEW_SKU_REQUIRED_FIELDS
            entry["inspection_required"] = default_inspection
        else:
            # 老 SKU：主库三字段提升到顶层，作为前端编辑框初值（本批次可改；
            # 留空提交时 Node6 写入回退 db_record 主库值，见 writer.py）
            db = entry["db_record"]
            entry["name_cn"] = db.get("name_cn")
            entry["hs_code"] = db.get("hs_code")
            entry["inspection_required"] = db.get("inspection_required")
        items_payload.append(entry)

    review_payload = {
        "factory_name": cur.get("factory_name"),
        "folder_path": cur.get("folder_path"),
        "source_documents": cur.get("source_documents") or [],
        "missing_skus": cur.get("missing_skus") or [],
        "items": items_payload,
        # Node3 提取 Agent 的结构化反馈与覆盖率（暂无箱单/目标为空/改单等）
        "extraction_issues": cur.get("extraction_issues") or [],
        "extraction_coverage": cur.get("extraction_coverage") or {},
        # 单重差异预警阈值，供审核页对照列实时高亮（Node4 判 Warning 同口径）
        "weight_diff_warn_ratio": get_settings().weight_diff_warn_ratio,
    }

    # W6a：暂缓二遍重试仍失败的最终挂起，透传标记供审核页提示
    # 「已重试仍失败，请人工补录或告知对照文件夹」（前端渲染本期不做）；
    # 无此情形不加键，payload 结构对既有消费者保持兼容
    if cur.get("is_final_attempt") and cur.get("extraction_ok") is False:
        review_payload["final_attempt"] = True
        review_payload["failure_reason"] = cur.get("failure_reason") or "unknown"

    logger.info("[Node5] 🔴 挂起等待人工审核：工厂「%s」，%d 个 SKU",
                cur.get('factory_name'), len(items_payload))

    # 🔴 系统在此暂停！resume 值为人类反馈数据
    human_feedback = interrupt(review_payload)

    if not isinstance(human_feedback, dict):
        human_feedback = {"approved": False, "items": []}

    approved = bool(human_feedback.get("approved", False))
    human_items = human_feedback.get("items") or []

    # ---- 用人类数据强制覆写 calculated_items ----
    original_by_sku = {i.get("sku"): i for i in cur.get("calculated_items") or []}
    merged_items = []
    for h_item in human_items:
        sku = h_item.get("sku")
        base = dict(original_by_sku.get(sku, {}))
        orig_extracted = (base.get("extracted_data") or {}).copy()
        new_extracted = h_item.get("extracted_data") or {}

        # 检测人工是否修改了底层数值
        edited = any(
            new_extracted.get(k) is not None and new_extracted.get(k) != orig_extracted.get(k)
            for k in ("total_quantity", "total_net_weight", "total_gross_weight")
        )

        # 检测人工是否修改了合规字段（中文品名/HS/商检等）：
        # 老 SKU 原值在 db_record，需回退；归一化后 None 视为未改
        db = base.get("db_record") or {}
        for f in ("name_cn", "name_en", "name_jp", "hs_code"):
            new_v = _norm_text(h_item.get(f))
            old_v = _norm_text(base.get(f) if base.get(f) is not None else db.get(f))
            if new_v is not None and new_v != old_v:
                edited = True
        new_insp = _norm_bool(h_item.get("inspection_required"))
        old_insp = _norm_bool(base.get("inspection_required")
                              if base.get("inspection_required") is not None
                              else db.get("inspection_required"))
        if new_insp is not None and new_insp != old_insp:
            edited = True

        # 覆写提取数据（人工只改原始数值，不需要也不应该自己算单重）
        orig_extracted.update({k: v for k, v in new_extracted.items() if v is not None})
        base["extracted_data"] = orig_extracted
        base["is_human_edited"] = edited or bool(h_item.get("is_human_edited"))
        base["status"] = h_item.get("status") or ("Normal" if approved else base.get("status"))

        # ---- 计算隔离：单重一律由本节点纯 Python 重算 ----
        # 不论人工/前端是否提交了 calculation，都以最终提取数值为准重算，
        # 杜绝"改了总量、单重还是旧值"的静默不一致（零容错）。
        qty = orig_extracted.get("total_quantity")
        unit_net, net_formula, net_err = _safe_div(orig_extracted.get("total_net_weight"), qty)
        unit_gross, gross_formula, gross_err = _safe_div(orig_extracted.get("total_gross_weight"), qty)
        base["calculation"] = {
            "net_formula": net_formula,
            "gross_formula": gross_formula,
            "calculated_unit_net": unit_net,
            "calculated_unit_gross": unit_gross,
        }
        err = net_err or gross_err
        if err:
            base["error_msg"] = err
            if edited and base.get("status") == "Normal":
                base["status"] = "Error"
        elif edited:
            base["error_msg"] = None

        # 新 SKU 的人工补录合规字段，归一化后挂到 item 上供 Node6 落库：
        # 文本字段 strip（空串不挂，保持留空回退主库值语义）；
        # inspection_required 存真布尔，杜绝 bool("0") == True 的隐患
        for f in ("name_cn", "name_en", "name_jp", "hs_code"):
            nv = _norm_text(h_item.get(f))
            if nv is not None:
                base[f] = nv
        bv = _norm_bool(h_item.get("inspection_required"))
        if bv is not None:
            base["inspection_required"] = bv

        merged_items.append(base)

    # 人类未返回的 SKU 原样保留
    returned_skus = {i.get("sku") for i in human_items}
    for sku, base in original_by_sku.items():
        if sku not in returned_skus:
            merged_items.append(base)

    cur["calculated_items"] = merged_items
    status = "Approved" if approved else "Rejected"
    logger.info("[Node5] 人工审核完成：%s（修改 %d 项）", status,
                sum(1 for i in merged_items if i.get('is_human_edited')))

    return {"current_factory_data": cur, "validation_status": status}
