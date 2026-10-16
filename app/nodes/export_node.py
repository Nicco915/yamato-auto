"""Node 7: 终态导出（Export Node）。

队列清空后触发。当前为骨架实现：
- 确认最终 Excel 副本已生成；
- 输出一份按工厂汇总的 JSON 摘要（后续报关单证生成的挂接点，
  《第三阶段.md》改造点 D：届时直接从 DB 取中文品名/HS 编码生成合规单证）。
"""
import json
import logging
from datetime import datetime, timezone

from app.config import get_settings
from app.db import batch_store
from app.state import AgentState

logger = logging.getLogger(__name__)


def export_node(state: AgentState) -> dict:
    # 协作式终止检查点（2026-10-14）：最后一家厂直走 Node7→END 不再回
    # Node2 的兜底——导出前命中终止标志同样停下，由 service stream
    # 包装层捕获转 aborted
    batch_id = state.get("batch_id")
    if batch_id and batch_store.is_cancel_requested(batch_id):
        raise batch_store.BatchAbortedError(batch_id)

    settings = get_settings()
    out_path = state.get("final_output_path")
    batch_id = state.get("batch_id") or "unknown"

    # 失败工厂计数（提取失败告警闭环 §3.2 配套）。
    # 口径：downstream_requirements 全集 − factory_outputs 快照（仅 Approved 工厂
    # 进快照）。差集 = 提取失败（deferred 耗尽/占位挂起/被跳过）+ 人工驳回/跳过
    # ——state 无独立的「最终失败」字段，差集是最接近的可得口径，故注释留痕。
    requirements = state.get("downstream_requirements") or {}
    factory_outputs = state.get("factory_outputs") or {}
    factories_failed = len(set(requirements) - set(factory_outputs))

    # 人工改动透出（写入侧人工改动感知 决策 1，阶段二 §2.7）：
    # 通道 C 保留未覆盖 / 通道 A 覆写 / 数量列人工改动，逐工厂计数汇总
    manual_overrides_kept = 0
    manual_values_overwritten = 0
    qty_manual_changes = 0
    for fo in factory_outputs.values():
        if not isinstance(fo, dict):
            continue
        manual_overrides_kept += int(fo.get("manual_override_kept") or 0)
        manual_values_overwritten += int(fo.get("manual_values_overwritten") or 0)
        qty_manual_changes += len(fo.get("qty_manual_changes") or [])

    # 单批次摘要
    batch_summary = {
        "exported_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "downstream_file": state.get("downstream_file_path"),
        "final_output_path": out_path,
        "factories_processed": list(requirements.keys()),
        "factories_failed": factories_failed,
        "validation_status": state.get("validation_status"),
        "manual_overrides_kept": manual_overrides_kept,
        "manual_values_overwritten": manual_values_overwritten,
        "qty_manual_changes": qty_manual_changes,
    }

    # 1) 批次级：output/{batch_id}/export_summary.json
    batch_dir = settings.batch_output_dir(batch_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_summary_path = batch_dir / "export_summary.json"
    batch_summary_path.write_text(
        json.dumps(batch_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 2) 全局级：output/export_summary.json（追加/更新该批次条目）
    global_summary_path = settings.output_dir_abs / "export_summary.json"
    global_summary = {}
    if global_summary_path.exists():
        try:
            global_summary = json.loads(global_summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            global_summary = {}
    global_summary.setdefault("batches", {})[batch_id] = batch_summary
    global_summary["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    global_summary_path.write_text(
        json.dumps(global_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("[Node7] 导出完成：%s；批次摘要 -> %s；全局摘要 -> %s",
                out_path, batch_summary_path, global_summary_path)
    return {"final_output_path": out_path}
