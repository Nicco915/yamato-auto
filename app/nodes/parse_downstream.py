"""Node 1: 下游解析与分组（Group & Queue Node）。

读取下游买家的标准装箱明细表，按工厂名（MAKER_MEI_KJ）聚合 SKU，
生成 pending_factories 队列，并记录每个 (工厂, SKU) 在 Excel 中的行号，
供 Node6 精准写回单元格。

下游文件实际结构（202624 青島XD 原文件）：
- 第 1 行为表头，57 列，812 行数据；
- 关键列：MAKER_MEI_KJ=工厂名（日文/英文）、SHOHIN_CD=SKU（13 位数字条码）、
  SOTOBAKO_D_HACCHU_SU=该行外箱发注数量（写回时 净重/毛重 = 单重 × 该列）；
- 原文件无 中文品名/净重/毛重 三列，由 Node6 首次写入时在 SHOHIN_MEI_E 后插入；
- 工厂名样例：山東中地 / Ｃ．正達工芸品 / TOP KOPH（青島）/ 上海億鑽五金工具有限公司（青島）。
"""
import logging

import pandas as pd

from app.config import get_settings
from app.state import AgentState

logger = logging.getLogger(__name__)

__all__ = ["parse_downstream_file", "parse_downstream"]


def parse_downstream_file(
    file_path: str,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[int]]]]:
    """解析下游装箱明细表 → (requirements, row_map) 纯函数（不依赖 state）。

    可被 Node1、add_factories_to_batch 等复用。

    Args:
        file_path: 装箱单 Excel 文件路径。

    Returns:
        (requirements, row_map)
        - requirements: {工厂名: [SKU, ...]}（按出现顺序去重）
        - row_map: {工厂名: {SKU: [Excel 行号, ...]}}（openpyxl 行号 = idx + 2）

    Raises:
        FileNotFoundError: 文件不存在
        Exception: pandas 解析失败时原样抛出
    """
    settings = get_settings()

    # SKU 是 13 位数字条码，必须按字符串读取，避免科学计数法/精度丢失
    df = pd.read_excel(file_path, sheet_name=0, dtype={settings.col_sku: str})
    df[settings.col_sku] = df[settings.col_sku].astype(str).str.strip()
    df[settings.col_factory] = df[settings.col_factory].astype(str).str.strip()

    requirements: dict[str, list[str]] = {}
    row_map: dict[str, dict[str, list[int]]] = {}

    for idx, row in df.iterrows():
        factory = row[settings.col_factory]
        sku = row[settings.col_sku]
        if not factory or factory == "nan" or not sku or sku == "nan":
            continue
        # pandas 行号 idx 从 0 开始；openpyxl 行号 = idx + 2（1 基 + 表头行）
        excel_row = int(idx) + 2
        requirements.setdefault(factory, [])
        if sku not in requirements[factory]:
            requirements[factory].append(sku)
        row_map.setdefault(factory, {}).setdefault(sku, []).append(excel_row)

    logger.info("[Node1] 解析 %s：共 %d 行，%d 个工厂",
                file_path, len(df), len(requirements))
    return requirements, row_map


# 向后兼容别名：其他模块（dispatcher/tools.py, api/service.py）仍通过此名导入
parse_requirements = parse_downstream_file


def parse_downstream(state: AgentState) -> dict:
    settings = get_settings()
    file_path = state.get("downstream_file_path") or settings.downstream_file_path

    requirements, row_map = parse_downstream_file(file_path)

    # 处理 factory_filter（如果有）
    factory_filter = state.get("factory_filter")
    if factory_filter:
        requirements = {k: v for k, v in requirements.items() if k in factory_filter}
        row_map = {k: v for k, v in row_map.items() if k in factory_filter}

    pending = list(requirements.keys())
    logger.info("[Node1] 本次队列 %d 个", len(pending))

    return {
        "downstream_file_path": file_path,
        "downstream_requirements": requirements,
        "downstream_row_map": row_map,
        "pending_factories": pending,
        "validation_status": "Pending",
    }
