"""Node 5: 人工审核中断点（Human-in-the-Loop Node）。

核心安全阀。interrupt() 挂起整个图，向外部抛出《第一阶段.md》第 6 节
结构的审核 payload；人类确认/修改后通过 Command(resume=...) 注入数据，
本节点用人类数据【强制覆写】state。

resume 数据约定：
{
  "approved": true/false,
  "items": [ ... 与 payload 中 items 同构（可修改 extracted_data 数值，
             新 SKU 必须补齐 name_cn / hs_code / inspection_required；
             SKU 改名的项必须携带 orig_sku=改名前的 sku） ... ]
}
"""
import logging
import re

from langgraph.types import interrupt
from sqlalchemy import select

from app.logging_config import bind_factory_from_state
from app.nodes.compute_align import _safe_div
from app.nodes.review_payload import build_review_payload
from app.state import AgentState

logger = logging.getLogger(__name__)

# 新 SKU 需人工补录的合规字段清单（《第三阶段.md》改造点 B）；
# 实际定义在 app/nodes/review_payload.py 里，reopen 复用同一份
NEW_SKU_REQUIRED_FIELDS = ["name_cn", "hs_code", "inspection_required"]

# SKU 13 位刚性校验（与 extraction/session.py A4 同口径）
_SKU_13_RE = re.compile(r"^\d{13}$")

# 主库查询异常哨兵：与「查不到」（None）区分——查询失败不改写 db_record/is_new_sku
_LOOKUP_FAILED = object()

# pop 缺省哨兵：区分「original 里没有该键」（D3 空卡新增）与「base 本身是空 dict」
_MISSING = object()


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


def _detect_compliance_edits(h_item: dict, base: dict) -> bool:
    """检测人工是否修改了合规字段（中文品名/HS/商检等）：
    老 SKU 原值在 db_record，需回退；归一化后 None 视为未改。
    """
    db = base.get("db_record") or {}
    for f in ("name_cn", "name_en", "name_jp", "hs_code"):
        new_v = _norm_text(h_item.get(f))
        old_v = _norm_text(base.get(f) if base.get(f) is not None else db.get(f))
        if new_v is not None and new_v != old_v:
            return True
    new_insp = _norm_bool(h_item.get("inspection_required"))
    old_insp = _norm_bool(base.get("inspection_required")
                          if base.get("inspection_required") is not None
                          else db.get("inspection_required"))
    return new_insp is not None and new_insp != old_insp


def _lookup_sku_record(factory_name: str, sku: str):
    """SKU 改名后用新 sku 单条重查主库（与 compute_align 同口径）。

    返回：记录 dict（查到）/ None（查不到）/ _LOOKUP_FAILED（查询异常）。
    """
    from app.db.models import Factory, FactorySKU
    from app.db.session import get_session

    try:
        with get_session() as session:
            factory = session.scalar(
                select(Factory).where(Factory.factory_name == factory_name))
            if factory is None:
                return None
            r = session.scalar(
                select(FactorySKU).where(
                    FactorySKU.factory_id == factory.factory_id,
                    FactorySKU.sku_code == sku))
    except Exception as e:  # noqa: BLE001 主库异常不拖垮审核提交
        logger.warning("[Node5] 改名后重查主库失败（保留原 db_record/is_new_sku）| "
                       "factory=%s sku=%s | %s: %s",
                       factory_name, sku, type(e).__name__, e)
        return _LOOKUP_FAILED
    if r is None:
        return None
    return {
        "name_cn": r.name_cn,
        "name_en": r.name_en,
        "name_jp": r.name_jp,
        "hs_code": r.hs_code,
        "inspection_required": r.inspection_required,
        "unit_net_weight": (float(r.unit_net_weight)
                            if r.unit_net_weight is not None else None),
        "unit_gross_weight": (float(r.unit_gross_weight)
                              if r.unit_gross_weight is not None else None),
    }


def _merge_human_items(human_items: list[dict], original_items: list[dict],
                       approved: bool, factory_name: str = "") -> list[dict]:
    """用人类提交的 items 强制覆写 original_items（Node5 resume 后的纯合并逻辑）。

    SKU 改名契约（批次3 B）：h_item 携带 orig_sku 表示该卡由 orig_sku 改名而来。
    - 按 orig_sku（缺省回退 sku）pop 找 base——旧键不残留；
    - base["sku"] 显式补键（修新 sku 卡/D3 空卡 base 无 sku 键 → writer 丢数据的隐患）；
    - 重复守卫：改名目标已存在/被本批占用 → Error + 保留 orig_sku，不用新值覆盖；
    - 格式守卫（A4 同口径 ^\\d{13}$）：非空 sku 不合法 → Error + 保留 orig_sku；
      空 sku 卡（无条码条目）不强制；
    - 改名成功：新 sku 重查主库刷新 db_record / is_new_sku，留痕 sku_renamed_from；
    - 守卫失败不写下游由 status=Error 天然保证（下游只写 Normal）。
    """
    original_by_sku = {i.get("sku"): i for i in original_items or []}
    merged_items = []
    claimed_skus: set = set()  # 本批 h_item 已占用的 sku（重复守卫用）

    for h_item in human_items:
        sku = h_item.get("sku")
        orig_sku = h_item.get("orig_sku")
        raw_base = original_by_sku.pop(orig_sku or sku, _MISSING)
        is_fresh_card = raw_base is _MISSING  # D3 空卡新增：original 里不存在
        base = {} if is_fresh_card else dict(raw_base)
        orig_extracted = (base.get("extracted_data") or {}).copy()
        new_extracted = h_item.get("extracted_data") or {}

        renamed = bool(orig_sku) and orig_sku != sku

        # ---- SKU 守卫（仅非空 sku；空 sku 卡=无条码条目，不强制 13 位）----
        guard_error = None
        if sku:
            if not _SKU_13_RE.fullmatch(str(sku)):
                guard_error = "SKU 必须是 13 位数字"
            elif sku in original_by_sku or sku in claimed_skus:
                # 改名目标已被占用（pop 后仍在 original / 本批其他卡已占用）
                guard_error = "人工改名后 SKU 重复"
        if guard_error and renamed:
            sku = orig_sku  # 守卫失败：保留 orig_sku，不用新 sku 覆盖

        # 检测人工是否修改了底层数值
        edited = any(
            new_extracted.get(k) is not None and new_extracted.get(k) != orig_extracted.get(k)
            for k in ("total_quantity", "total_net_weight", "total_gross_weight")
        )
        if renamed:
            edited = True  # 改名视为编辑

        # 检测人工是否修改了合规字段（中文品名/HS/商检等）
        if _detect_compliance_edits(h_item, base):
            edited = True

        # 覆写提取数据（人工只改原始数值，不需要也不应该自己算单重）
        orig_extracted.update({k: v for k, v in new_extracted.items() if v is not None})
        base["extracted_data"] = orig_extracted
        base["sku"] = sku  # 显式补键（改名成功的新 sku / D3 空卡新增）
        base["is_human_edited"] = edited or bool(h_item.get("is_human_edited"))
        base["status"] = h_item.get("status") or ("Normal" if approved else base.get("status"))

        # 改名成功：审计留痕；改名/D3 空卡新增：用新 sku 重查主库确定 is_new_sku
        if renamed and not guard_error:
            base["sku_renamed_from"] = orig_sku
        if sku and not guard_error and (renamed or is_fresh_card):
            record = _lookup_sku_record(factory_name, str(sku))
            if record is _LOOKUP_FAILED:
                # 主库异常：老卡保留原 db_record/is_new_sku；空卡回退前端标记
                if is_fresh_card and "is_new_sku" not in base:
                    base["is_new_sku"] = bool(h_item.get("is_new_sku"))
            elif record is None:
                base["is_new_sku"] = True   # 触发新 SKU 合规字段路径
                base["db_record"] = {}
            else:
                base["is_new_sku"] = False
                base["db_record"] = record

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

        # SKU 守卫优先级最高：强制 Error（下游只写 Normal，天然不写）
        if guard_error:
            base["status"] = "Error"
            base["error_msg"] = guard_error

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

        if sku:
            claimed_skus.add(sku)
        merged_items.append(base)

    # 人类未返回的 SKU 原样保留（pop 后剩余的键；returned_keys 按 orig_sku 判定，
    # 改名项的旧 sku 不会被误判为"未返回"）
    returned_keys = {(i.get("orig_sku") or i.get("sku")) for i in human_items}
    for key, base in original_by_sku.items():
        if key not in returned_keys:
            merged_items.append(base)
    return merged_items


def human_review(state: AgentState) -> dict:
    # L2 日志关联：从 state 重绑当前工厂名（多工厂 resume 链上 submit 拷贝的
    # context 残留上一工厂）；interrupt 恢复后节点从头重跑，入口绑定对
    # 挂起前/恢复后两程日志都生效；节点独立 context 保证不外泄，无需清理
    bind_factory_from_state(state)
    cur = dict(state.get("current_factory_data") or {})

    # ---- 构建审核负载（第一阶段.md 第 6 节结构）----
    # 公共构建函数 review_payload.py：与 reopen 共用同一份逻辑；
    # overrides 透传用于 alias_suggestion 的 override 判定
    review_payload = build_review_payload(
        cur, overrides=state.get("factory_alias_overrides"),
    )

    logger.info("[Node5] 🔴 挂起等待人工审核：工厂「%s」，%d 个 SKU",
                cur.get('factory_name'), len(review_payload["items"]))

    # 🔴 系统在此暂停！resume 值为人类反馈数据
    human_feedback = interrupt(review_payload)

    if not isinstance(human_feedback, dict):
        human_feedback = {"approved": False, "items": []}

    approved = bool(human_feedback.get("approved", False))
    human_items = human_feedback.get("items") or []

    # ---- 用人类数据强制覆写 calculated_items（含 SKU 改名合并/守卫）----
    merged_items = _merge_human_items(
        human_items, cur.get("calculated_items") or [],
        approved, cur.get("factory_name") or "",
    )

    cur["calculated_items"] = merged_items
    status = "Approved" if approved else "Rejected"
    logger.info("[Node5] 人工审核完成：%s（修改 %d 项）", status,
                sum(1 for i in merged_items if i.get('is_human_edited')))

    return {"current_factory_data": cur, "validation_status": status}
