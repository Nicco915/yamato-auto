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

    summary = {
        # datetime.utcnow() 在 Python 3.12+ 已弃用；改用 now(timezone.utc)
        # 后去掉 tzinfo，输出格式与原 utcnow().isoformat() 完全一致（保留微秒）
        "exported_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "downstream_file": state.get("downstream_file_path"),
        "final_output_path": out_path,
        "factories_processed": list((state.get("downstream_requirements") or {}).keys()),
        "validation_status": state.get("validation_status"),
    }
    summary_path = settings.output_dir_abs / "export_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("[Node7] 导出完成：%s；摘要 -> %s", out_path, summary_path)
    return {"final_output_path": out_path}
