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
from app.state import AgentState

logger = logging.getLogger(__name__)


def export_node(state: AgentState) -> dict:
    settings = get_settings()
    out_path = state.get("final_output_path")
    batch_id = state.get("batch_id") or "unknown"

    # 单批次摘要
    batch_summary = {
        "exported_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "downstream_file": state.get("downstream_file_path"),
        "final_output_path": out_path,
        "factories_processed": list((state.get("downstream_requirements") or {}).keys()),
        "validation_status": state.get("validation_status"),
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
