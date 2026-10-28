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

    # 第七阶段 §1.1/§2.1：手动录入厂从待识别队列剔除（不进 LLM 提取、
    # 不弹 Node5 审核），但保留在 requirements/row_map 中（副本透出/
    # 导出统计/批次详情仍需要全集）。匹配口径与 factory_filter 一致：
    # 装箱单工厂名（MAKER_MEI_KJ 原文）精确匹配；normalize 回写 state，
    # 未命中任何工厂的名字直接丢弃（防误拼名字残留污染看板/监控口径）
    manual_skip = sorted(
        set(state.get("manual_skip_factories") or []) & set(requirements))
    skip_set = set(manual_skip)
    pending = [f for f in requirements.keys() if f not in skip_set]
    if skip_set:
        logger.info("[Node1] 手动录入跳过 %d 个工厂：%s",
                    len(skip_set), "、".join(manual_skip))
    logger.info("[Node1] 本次队列 %d 个", len(pending))

    result = {
        "downstream_file_path": file_path,
        "downstream_requirements": requirements,
        "downstream_row_map": row_map,
        "pending_factories": pending,
        "manual_skip_factories": manual_skip,
        "validation_status": "Pending",
    }

    # 第七阶段 §1.2：副本提前生成——解析后无条件把原件复制到
    # output/{batch_id}/containers/ 并建基线快照（含人工已填三列），
    # 不再等 Node6 首次写入；手动录入厂直接以该副本为下游数据源。
    # 副本是下游环节的硬依赖，失败不吞（零容错）；复用 writer 的
    # 「绝不写原件 + 已存在则复用」逻辑（延迟 import 与 writer 内
    # copy_snapshot 同哲学，防 app.graph → nodes → orchestrator 循环）
    if state.get("batch_id"):
        from app.nodes.writer import _ensure_output_copy
        out_path, copy_created = _ensure_output_copy(
            {"batch_id": state["batch_id"],
             "downstream_file_path": file_path},
            return_created=True)
        result["final_output_path"] = str(out_path)
        if copy_created:
            # 新副本 = 新一轮写入起点（首次运行/rerun 归档旧 output 后重建）：
            # 旧 run 的 factory_outputs / manual_kept_rows 指向已归档旧副本，
            # 必须清空。否则 writer 的 written_before 误判为「本厂已写过」，
            # 原件中人工预填的三列会躲过遇值确认门被系统值静默覆盖。
            result["factory_outputs"] = {}
            result["manual_kept_rows"] = {}

    return result
