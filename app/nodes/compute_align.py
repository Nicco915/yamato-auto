"""Node 4: 纯代码计算与对齐 + 数据库联查（Compute & Align Node）。

按《第一阶段.md》设计原则：所有数学计算绝对禁止大模型，由本节点纯 Python 执行。
- 计算：单个净重 = 总净重 / 总件数，生成 "250.0 / 50" 公式字符串作为证据；
- 防错：ZeroDivisionError / TypeError 一律捕获，标 Error，决不中断流转；
- 对齐：与 expected_skus 求交集，标记缺失项；
- 联查（《第三阶段.md》改造点 A）：
  - 老 SKU：挂载中文品名/HS 编码，对比历史单重，差异 >5% 标 Warning；
  - 新 SKU：标记 is_new_sku=True，Node5 将强制人工补录合规字段。
"""
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Factory, FactorySKU
from app.db.session import get_session
from app.state import AgentState


def _safe_div(total, qty):
    """安全除法：返回 (结果, 公式字符串, 错误信息)。"""
    try:
        formula = f"{total} / {qty}"
        return total / qty, formula, None
    except (ZeroDivisionError, TypeError) as e:
        return None, f"{total} / {qty}", f"{type(e).__name__}: {e}"


def compute_align(state: AgentState) -> dict:
    settings = get_settings()
    cur = dict(state.get("current_factory_data") or {})
    factory_name = cur.get("factory_name", "")
    extracted = cur.get("extracted_items") or []
    expected_skus = set(cur.get("expected_skus") or [])

    # ---- 数据库联查：一次性取出该工厂的全部 SKU 主数据 ----
    db_records: dict[str, dict] = {}
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == factory_name)
        )
        if factory:
            rows = session.scalars(
                select(FactorySKU).where(FactorySKU.factory_id == factory.factory_id)
            ).all()
            db_records = {r.sku_code: {
                "name_cn": r.name_cn,
                "name_en": r.name_en,
                "name_jp": r.name_jp,
                "hs_code": r.hs_code,
                "inspection_required": r.inspection_required,
                "unit_net_weight": float(r.unit_net_weight) if r.unit_net_weight is not None else None,
                "unit_gross_weight": float(r.unit_gross_weight) if r.unit_gross_weight is not None else None,
            } for r in rows}

    calculated_items = []
    seen_skus: set[str] = set()

    for item in extracted:
        sku = str(item.get("sku_name", "")).strip()
        seen_skus.add(sku)
        qty = item.get("total_quantity")
        net_total = item.get("total_net_weight")
        gross_total = item.get("total_gross_weight")

        unit_net, net_formula, net_err = _safe_div(net_total, qty)
        unit_gross, gross_formula, gross_err = _safe_div(gross_total, qty)

        # ---- 状态判定：Error > Warning > Normal ----
        status = "Normal"
        error_msg = net_err or gross_err
        if item.get("needs_human_review") and error_msg:
            status = "Error"
        elif error_msg:
            status = "Error"
        elif item.get("needs_human_review"):
            status = "Needs_Review"

        # ---- 主数据联查 ----
        record = db_records.get(sku)
        is_new_sku = record is None
        db_info = record or {}
        if record and unit_net is not None and record.get("unit_net_weight"):
            diff = abs(unit_net - record["unit_net_weight"]) / record["unit_net_weight"]
            if diff > settings.weight_diff_warn_ratio and status == "Normal":
                status = "Warning"
                db_info["weight_diff_ratio"] = round(diff, 4)

        if sku not in expected_skus:
            status = "Warning" if status == "Normal" else status
            unexpected = True
        else:
            unexpected = False

        calculated_items.append({
            "sku": sku,
            "extracted_data": {
                "total_quantity": qty,
                "total_net_weight": net_total,
                "total_gross_weight": gross_total,
                "weight_unit": item.get("weight_unit", "KG"),
                "source_file": item.get("source_file"),
            },
            "calculation": {
                "net_formula": net_formula,
                "gross_formula": gross_formula,
                "calculated_unit_net": unit_net,
                "calculated_unit_gross": unit_gross,
            },
            "status": status,
            "error_msg": error_msg,
            "is_human_edited": False,
            "is_new_sku": is_new_sku,
            "unexpected_sku": unexpected,
            "db_record": db_info,
        })

    missing = sorted(expected_skus - seen_skus)
    cur["calculated_items"] = calculated_items
    cur["missing_skus"] = missing

    n_err = sum(1 for i in calculated_items if i["status"] == "Error")
    n_new = sum(1 for i in calculated_items if i["is_new_sku"])
    print(f"[Node4] 工厂「{factory_name}」：{len(calculated_items)} 项计算完成"
          f"（Error {n_err} / 新SKU {n_new} / 缺失SKU {len(missing)}）")

    return {"current_factory_data": cur}
