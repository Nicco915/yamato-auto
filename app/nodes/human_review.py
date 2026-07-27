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
from langgraph.types import interrupt

from app.state import AgentState

# 新 SKU 需人工补录的合规字段清单（《第三阶段.md》改造点 B）
NEW_SKU_REQUIRED_FIELDS = ["name_cn", "hs_code", "inspection_required"]


def human_review(state: AgentState) -> dict:
    cur = dict(state.get("current_factory_data") or {})

    # ---- 构建审核负载（第一阶段.md 第 6 节结构）----
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
            # 新 SKU：前端强制展示空白必填框
            entry["fields_to_fill"] = NEW_SKU_REQUIRED_FIELDS
        items_payload.append(entry)

    review_payload = {
        "factory_name": cur.get("factory_name"),
        "folder_path": cur.get("folder_path"),
        "source_documents": cur.get("source_documents") or [],
        "missing_skus": cur.get("missing_skus") or [],
        "items": items_payload,
    }

    print(f"[Node5] 🔴 挂起等待人工审核：工厂「{cur.get('factory_name')}」，"
          f"{len(items_payload)} 个 SKU")

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

        # 覆写提取数据与计算结果（人类修改总量时应同时给出重算后的单重）
        orig_extracted.update({k: v for k, v in new_extracted.items() if v is not None})
        base["extracted_data"] = orig_extracted
        if h_item.get("calculation"):
            base["calculation"] = h_item["calculation"]
        base["is_human_edited"] = edited or bool(h_item.get("is_human_edited"))
        base["status"] = h_item.get("status") or ("Normal" if approved else base.get("status"))

        # 新 SKU 的人工补录合规字段，直接挂到 item 上供 Node6 落库
        for f in NEW_SKU_REQUIRED_FIELDS + ["name_en", "name_jp"]:
            if h_item.get(f) is not None:
                base[f] = h_item[f]

        merged_items.append(base)

    # 人类未返回的 SKU 原样保留
    returned_skus = {i.get("sku") for i in human_items}
    for sku, base in original_by_sku.items():
        if sku not in returned_skus:
            merged_items.append(base)

    cur["calculated_items"] = merged_items
    status = "Approved" if approved else "Rejected"
    print(f"[Node5] 人工审核完成：{status}（修改 {sum(1 for i in merged_items if i.get('is_human_edited'))} 项）")

    return {"current_factory_data": cur, "validation_status": status}
