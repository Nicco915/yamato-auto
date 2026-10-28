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
    # 批次号（= thread_id），用于输出文件分目录 {output}/{batch_id}/containers|declarations/
    batch_id: str

    # ----- Node1 产物 -----
    downstream_requirements: Dict[str, List[str]]  # {工厂名: [SKU, ...]}
    # {工厂名: {SKU: [行号, ...]}}——行号语义已废弃（写入侧人工改动感知
    # 决策 3）：Node6/清空一律按 (工厂, SKU, seq) row_key 索引现扫定位，
    # 本结构只承载「行数需求」（len(列表) = 该 SKU 应有行数）；元素值对
    # 原件解析产物是原件 openpyxl 行号（_sync_output_copy_rows 拷贝原件
    # 行值仍消费），对人工新增行并入（决策 4）是副本行号占位，仅供参考
    downstream_row_map: Dict[str, Dict[str, List[int]]]
    pending_factories: List[str]          # 待处理工厂队列
    # 暂缓队列（W6a）：提取失败的工厂先记到这里（条目 {"factory_name",
    # "failure_reason"}），不进 Node5/Node6——占位数据绝不写 Excel/主库；
    # 主队列走完后由 Node2 弹出做二遍重试，仍失败才最终挂起人工补录
    deferred_factories: List[Dict]
    # 自动跳过清单（2026-10-12 需求 1，Node4C 写入）：二遍仍缺资料
    # （no_folder_matched/no_documents）的工厂不挂人工审核，记到这里；
    # 条目 {"factory_name", "failure_reason", "ts"}，供日志/审计上下文与
    # pipeline_state「待补充」分类参考（权威分类以 review_audits 留痕为准）
    auto_skipped_factories: List[Dict]

    # ----- 当前批次 -----
    current_factory_data: CurrentFactoryData
    # 已审核通过的工厂计算结果快照，供 reopen 已审核工厂时恢复数据使用；
    # 新格式：{工厂名: dict(current_factory_data 完整快照)}；
    # 旧格式兼容：{工厂名: list(calculated_items)}（Step 4 兼容）
    factory_outputs: Dict[str, Any]  # {工厂名: dict | list}

    # ----- 人机协同 -----
    validation_status: str                # Pending / Approved / Rejected / Skipped
    # 单厂重试标志：置 True 时 Node3 跳过会话缓存强制重提（service 层
    # retry_factory_extraction 写入）；Node3 消费后必须自清（所有返回分支
    # 的 update 都带 False），防止残留污染后续工厂的提取流程
    force_reextract: bool
    # 补充工厂确认门（写入侧人工改动感知 决策 1 通道 B）：操作员确认覆写
    # 人工修改后由 add_factories_to_batch 写入确认工厂名单；Node6 writer
    # 对名单内工厂按通道 A（rework）语义覆盖（旧值留痕
    # manual_value_overwritten），writer 消费本厂后自清，防止残留确认
    # 污染后续写入
    overwrite_manual_factories: Optional[List[str]]

    # ----- 第七阶段：手动录入模式 -----
    # 手动跳过清单（设计文档 §2.1，批次启动时传入）：清单内工厂由 Node1
    # 从 pending_factories 剔除——不进 LLM 提取、不弹 Node5 审核，直接以
    # output 副本中人工已填的三列为下游数据源；补充工厂/批次重跑/
    # factory_watch 一律不自动触发识别，仅「补充工厂」入口的
    # reopen_factories 可把工厂从本清单移除重开识别
    manual_skip_factories: Optional[List[str]]
    # 写入遇值确认门「保留」标记（§2.4）：{工厂名: [row_key_str, ...]}；
    # 被保留行整行视同 MX 人工行——后续写回与 reopen 清空一律跳过，
    # 永不覆盖。Node6 writer 决策后落账（节点返回值合并），
    # writer._write_excel / clear_sku_rows 消费
    manual_kept_rows: Optional[Dict[str, List[str]]]
    # 内部确认现场：展示清单及逐格键先入 checkpoint，完成写入后清空。
    overwrite_gate: Optional[Dict[str, Any]]

    # ----- Node7 产物 -----
    final_output_path: str                # 最终输出的 Excel 副本路径
