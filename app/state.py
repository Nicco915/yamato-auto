"""LangGraph 全局状态定义（对应《第一阶段.md》第 3 节）。

所有字段均为可 JSON/pickle 序列化的原生类型，
保证 SqliteSaver checkpointer 能正常持久化。
"""
from typing import Any, Dict, List, Optional, TypedDict


class CurrentFactoryData(TypedDict, total=False):
    """当前正在处理的工厂批次数据。"""

    factory_name: str                     # 下游装箱单中的工厂名（MAKER_MEI_KJ 原文）
    folder_path: Optional[str]            # Node2 模糊匹配到的本地工厂文件夹
    match_score: float                    # 模糊匹配得分（alias 命中记 100）
    source_documents: List[str]           # 该文件夹下的单据文件列表
    expected_skus: List[str]              # 下游要求该工厂提供的 SKU 列表
    extracted_items: List[Dict[str, Any]]   # Node3 提取的原始数据（总数/总重）
    calculated_items: List[Dict[str, Any]]  # Node4 计算+查库后的完整数据
    missing_skus: List[str]               # 下游要求但提取结果中缺失的 SKU
    # ----- W6a 暂缓队列标记（Node3/Node2 写入）-----
    extraction_ok: bool                   # Node3 提取是否成功（占位分支 False，成功/mock True）
    failure_reason: str                   # 提取失败原因（no_folder_matched / no_items_extracted / extraction_error: ...）
    is_final_attempt: bool                # True=暂缓队列二遍重试（仍失败才最终挂起人工补录）


class AgentState(TypedDict, total=False):
    """状态机共享状态（total=False：节点只返回需要更新的键）。"""

    # ----- 输入参数 -----
    downstream_file_path: str             # 下游装箱单路径
    upstream_root: str                    # 上游工厂文件夹根目录
    factory_filter: Optional[List[str]]   # 只处理指定工厂（调试/冒烟用，None=全部）
    # 批次级工厂名对照（W5）：预扫确认时用户给出的「仅本次生效」结果，
    # {装箱单工厂名: 上游文件夹名}，不落盘；Node2 匹配的最高优先档
    factory_alias_overrides: Optional[Dict[str, str]]

    # ----- Node1 产物 -----
    downstream_requirements: Dict[str, List[str]]  # {工厂名: [SKU, ...]}
    # {工厂名: {SKU: [openpyxl 行号, ...]}}，Node6 精准写回单元格用
    downstream_row_map: Dict[str, Dict[str, List[int]]]
    pending_factories: List[str]          # 待处理工厂队列
    # 暂缓队列（W6a）：提取失败的工厂先记到这里（条目 {"factory_name",
    # "failure_reason"}），不进 Node5/Node6——占位数据绝不写 Excel/主库；
    # 主队列走完后由 Node2 弹出做二遍重试，仍失败才最终挂起人工补录
    deferred_factories: List[Dict]

    # ----- 当前批次 -----
    current_factory_data: CurrentFactoryData

    # ----- 人机协同 -----
    validation_status: str                # Pending / Approved / Rejected
    # 单厂重试标志：置 True 时 Node3 跳过会话缓存强制重提（service 层
    # retry_factory_extraction 写入）；Node3 消费后必须自清（所有返回分支
    # 的 update 都带 False），防止残留污染后续工厂的提取流程
    force_reextract: bool

    # ----- Node7 产物 -----
    final_output_path: str                # 最终输出的 Excel 副本路径
