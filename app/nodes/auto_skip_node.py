"""Node 4C: 缺资料工厂自动跳过节点（Auto-skip Node，2026-10-12 需求 1）。

提取失败原因属「缺资料」类（no_folder_matched / no_documents），且为
二遍重试仍无资料或最后一个工厂首遍即缺资料时，Node4 条件边把流转引到
这里：不挂人工审核、不进 Node5/Node6（占位数据绝不写回 Excel/主库），
只把工厂记入 auto_skipped_factories 并向 review_audits 留痕
（result_status="factory_auto_skipped"，与人工跳过的 factory_skipped
区分）。批次跑完后由 pipeline_state 按「待补充」归类，可经「补充工厂」
捞回或在详情页强制结案。
"""
import json
import logging
from datetime import datetime

from app.state import AgentState

logger = logging.getLogger(__name__)

# 可自动跳过的失败原因（路由层 _route_after_compute 已保证，这里仅防御）
_AUTO_SKIP_REASONS = {"no_folder_matched", "no_documents"}


def auto_skip_node(state: AgentState) -> dict:
    cur = state.get("current_factory_data") or {}
    factory = cur.get("factory_name") or "未知工厂"
    reason = cur.get("failure_reason") or "unknown"

    if reason not in _AUTO_SKIP_REASONS:
        # 防御：非缺资料类失败不应路由到本节点；只记 error 日志原样返回
        # （不抛、不污染 auto_skipped_factories，由后续节点按现状处理）
        logger.error("[Node4C] 工厂「%s」失败原因 %s 不属于可自动跳过类，"
                     "按原样流转（不记自动跳过）", factory, reason)
        return {}

    skipped = list(state.get("auto_skipped_factories") or [])
    skipped.append({
        "factory_name": factory,
        "failure_reason": reason,
        "ts": datetime.utcnow().isoformat(),
    })
    logger.info("[Node4C] 工厂「%s」自动跳过（缺资料：%s），不挂人工审核",
                factory, reason)

    # 审计留痕：与 board.py 合成行留痕同哲学——节点内直写 DB（writer.py
    # 有先例），写库失败只 warning 不抛出，绝不反过来搞挂跑图
    try:
        from app.db.models import ReviewAudit
        from app.db.session import get_session
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=state.get("batch_id") or "unknown",
                factory_name=factory,
                approved=False,
                edited_count=0,
                changes_json=json.dumps([{"auto_skip_reason": reason}],
                                        ensure_ascii=False),
                new_skus_json="[]",
                result_status="factory_auto_skipped",
            ))
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("⚠️⚠️ [审计落库失败] thread=%s 工厂「%s」"
                       "factory_auto_skipped 留痕写入失败：%s: %s",
                       state.get("batch_id"), factory, type(e).__name__, e)

    return {"auto_skipped_factories": skipped}
