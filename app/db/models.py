"""SQLAlchemy 模型：按《第三阶段.md》第 2 节设计，适配 SQLite 方言。

- factories: 工厂主表
- factory_skus: SKU 主数据子表（多语言品名 / HS 编码 / 单件重量沉淀）
- chat_sessions: 调度 Agent 会话清单（左侧 sidebar，支持 pin 批次、标题、待确认操作信封）
- chat_messages: 调度 Agent 对话内容（user/assistant 消息流水，按 ts 排序）
- chat_tool_history: 调度 Agent 工具调用审计流水（含确认状态：NULL=只读/0=拒/1=批）
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Factory(Base):
    """工厂表。"""

    __tablename__ = "factories"

    factory_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factory_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # 中文短名（中地/正达/贝来），文件夹匹配目标
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 商检工厂标记（取代 INSPECTION_FACTORIES 兜底名单）
    is_inspection_factory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    skus: Mapped[list["FactorySKU"]] = relationship(
        back_populates="factory", cascade="all, delete-orphan"
    )


class FactorySKU(Base):
    """SKU 主数据表：同一工厂下 SKU 唯一。"""

    __tablename__ = "factory_skus"
    __table_args__ = (
        UniqueConstraint("factory_id", "sku_code", name="unique_factory_sku"),
    )

    sku_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factory_id: Mapped[int] = mapped_column(
        ForeignKey("factories.factory_id", ondelete="CASCADE"), nullable=False
    )
    sku_code: Mapped[str] = mapped_column(String(100), nullable=False)

    # 多语言品名
    name_cn: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 中文品名（报关用）
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 英文品名（清关用）
    name_jp: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 日文品名

    # 合规与通关数据
    hs_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    inspection_required: Mapped[bool] = mapped_column(Boolean, default=False)

    # 物理属性沉淀（KG，三位小数）
    unit_net_weight: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    unit_gross_weight: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    factory: Mapped[Factory] = relationship(back_populates="skus")


class ReviewAudit(Base):
    """人工审核审计表：每次 resume 提交的人工改动与新 SKU 补录留痕。

    进 master.db（靠 get_engine() 的 create_all 自动建表，零迁移）。
    changes_json / new_skus_json 为 JSON 序列化文本（ensure_ascii=False）。
    审计是辅助设施：写入失败绝不阻塞已成功的 resume（见 service._write_audit）。
    """

    __tablename__ = "review_audits"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    factory_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    edited_count: Mapped[int] = mapped_column(Integer, default=0)
    changes_json: Mapped[str] = mapped_column(Text, default="[]")
    new_skus_json: Mapped[str] = mapped_column(Text, default="[]")
    result_status: Mapped[str | None] = mapped_column(String(20), nullable=True)


class DispatcherMemory(Base):
    """调度 Agent L2 操作记忆表：按 session_id 分区，跨重启持久化。

    进 master.db（靠 get_engine() 的 create_all 自动建表，零迁移）。
    recent_paths_json / operation_summary_json 为 JSON 序列化文本。
    记忆是辅助设施：写入失败绝不阻塞主流程（见 dispatcher.memory）。
    """

    __tablename__ = "dispatcher_memory"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_factory: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recent_paths_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# Container 表——柜子维度信息
class Container(Base):
    __tablename__ = 'containers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_thread_id = Column(String, nullable=False, index=True)  # 上游批次 thread_id
    kanri_no = Column(String, nullable=False)       # 虚拟柜号 KANRI_NO
    port = Column(String, nullable=False)            # 港口 MINATO_MEI_KJ
    container_type = Column(String, nullable=False)  # 箱型 CONTAINER_MEI
    factories = Column(JSON, nullable=False)         # 柜内工厂列表
    sj_factories = Column(JSON, nullable=False)      # 柜内商检工厂列表
    row_count = Column(Integer, nullable=False)      # 柜内行数（用于货量比较）
    created_at = Column(DateTime, server_default=func.now())


# Declaration 表——票
class Declaration(Base):
    __tablename__ = 'declarations'
    id = Column(Integer, primary_key=True, autoincrement=True)
    split_thread_id = Column(String, nullable=False, index=True)  # 分票图 thread_id
    ticket_no = Column(String, nullable=False)       # 票号：港口-序号（如「東京港-01」）
    port = Column(String, nullable=False)            # 港口
    container_type = Column(String, nullable=False)  # 箱型
    items = Column(JSON, nullable=False)             # [{kanri_no, factory_filter, is_partial}]
    sj_factories = Column(JSON, nullable=False)      # [{factory_name, inspection_required_sku_count}]
    status = Column(String, nullable=False, default='pending')  # pending/confirmed/reset
    version = Column(Integer, nullable=False, default=1)
    force_confirmed = Column(Boolean, nullable=False, default=False)  # 是否强制通过
    warnings = Column(JSON, nullable=True)           # 软校验警告 [{rule, message}]
    created_at = Column(DateTime, server_default=func.now())
    confirmed_at = Column(DateTime, nullable=True)


# ProductMapping 表——报关产品映射（品名级/SKU 级）
class ProductMapping(Base):
    __tablename__ = 'product_mappings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    product_name_cn = Column(String, nullable=False, index=True)   # 中文品名（主匹配键）
    # 【已废弃】SKU 级精确匹配旧列：已迁移到 product_mapping_skus 子表（一品名多 SKU），
    # 由 app.db.sync.ensure_mapping_skus_migrated 只读搬迁；列保留不删、值不清空（回滚保险）
    sku_code = Column(String, ForeignKey('factory_skus.sku_code'), nullable=True)
    factory_id = Column(Integer, ForeignKey('factories.factory_id'), nullable=True)
    hs_code = Column(String, nullable=True)              # 税号
    supplier_name = Column(String, nullable=True)        # 供应商报关全称（如 青岛东基恒塑料包装有限公司）
    inspection_required = Column(Boolean, nullable=False, default=False)  # 商检（产品级）
    name_en = Column(String, nullable=True)              # 英文品名（产品组一）
    unit_code = Column(String, nullable=True)            # 计量单位代码（自定义七，如 '007'）
    is_incomplete = Column(Boolean, nullable=False, default=False)  # 待完善标记（Node5 反向生成时 True）
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # SKU 关联子表（一品名多 SKU）；删映射行级联删子表行
    sku_links = relationship("ProductMappingSku", cascade="all, delete-orphan")


# ProductMappingSku 表——产品映射的 SKU 关联子表（一品名对多 SKU）
# sku_code 故意不加 ForeignKey：factory_skus.sku_code 本身无唯一约束，
# 旧列的外键也是弱引用；仅靠 UniqueConstraint(mapping_id, sku_code) 防重复。
class ProductMappingSku(Base):
    __tablename__ = 'product_mapping_skus'
    id = Column(Integer, primary_key=True, autoincrement=True)
    mapping_id = Column(Integer, ForeignKey('product_mappings.id'), nullable=False, index=True)
    sku_code = Column(String, nullable=False)              # 关联 SKU（弱引用，不加外键）

    __table_args__ = (
        UniqueConstraint("mapping_id", "sku_code", name="unique_mapping_sku"),
    )


# ProductGroup 表——品名组（套装拆分/同箱分摊）
class ProductGroup(Base):
    __tablename__ = 'product_groups'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)              # '6件套' / '3件套' / '烟灰缸+支架'
    group_type = Column(String, nullable=False)        # 'set_split' | 'box_share'
    source_name_cn = Column(String, nullable=False)    # 总表中的源品名（如 '6件套'、'烟灰缸'）


# ProductGroupMember 表——品名组成员
class ProductGroupMember(Base):
    __tablename__ = 'product_group_members'
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('product_groups.id'), nullable=False)
    product_name_cn = Column(String, nullable=False)  # 组件品名
    display_order = Column(Integer, nullable=False)   # 显示顺序，首行带箱数/毛重
    split_price = Column(Float, nullable=True)        # set_split：组件单价 USD/套；box_share：NULL（金额平均分）
    split_net_weight = Column(Float, nullable=True)   # 组件单件净重 kg/件


# FactoryAlias 表——工厂别名（日文名/全称/Excel 变体）
# 统一入库取代 alias_map.json（文件夹匹配）与 FACTORY_NORMALIZE_MAP（Excel 归一化）；
# 同一 alias 两种用途合并为一行（两个 use_* 标记同 True），按 factories 实体关联。
class FactoryAlias(Base):
    __tablename__ = 'factory_aliases'
    id = Column(Integer, primary_key=True, autoincrement=True)
    factory_id = Column(Integer, ForeignKey('factories.factory_id'), nullable=False)
    alias = Column(String, nullable=False, index=True)    # 别名（日文名/全称/Excel 变体）
    use_folder_match = Column(Boolean, nullable=False, default=True)    # 用于文件夹匹配（原 alias_map）
    use_excel_normalize = Column(Boolean, nullable=False, default=False)  # 用于 Excel 归一化（原 NORMALIZE_MAP）
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# Port 表——港口主数据（报关票名/文件名中文名、报关单英文名、发票号字母）
# DB 为权威源，declare/naming.PORT_MAP 硬编码退化为兜底 + 种子来源；
# inv_letter 全表唯一（撞字母会导致发票号串票）。
class Port(Base):
    __tablename__ = 'ports'
    id = Column(Integer, primary_key=True, autoincrement=True)
    port_jp = Column(String, nullable=False, unique=True)      # 装箱单里的港口原名（主匹配键，如 博多港）
    name_cn = Column(String, nullable=False)                   # 票名/文件名用中文名（博多 → 博多A票 / 报关博多A.xlsx）
    name_en = Column(String, nullable=False)                   # 报关单英文港口名（大写，如 HAKATA）
    inv_letter = Column(String(1), nullable=False, unique=True)  # 发票号字母（YIL{字母}{号码段}），全表唯一
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# SkuMasterAudit 表——SKU 主数据人工编辑留痕（review_audits 同级）
# PUT /api/v1/mappings/skus/{id} 时逐字段 diff，每个有变化的字段写一条：
# 何时（changed_at）/ 哪个 SKU（sku_code）/ 哪个字段（field）/ 旧值 / 新值。
class SkuMasterAudit(Base):
    __tablename__ = 'sku_master_audits'
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku_code = Column(String, nullable=False, index=True)
    field = Column(String, nullable=False)        # 被改字段名
    old_value = Column(String, nullable=True)
    new_value = Column(String, nullable=True)
    changed_at = Column(DateTime, server_default=func.now())


# ─────────────────────────────────────────────────────────────
# 端到端批次业务表（End-to-end 升级新增）
# ─────────────────────────────────────────────────────────────

class Batch(Base):
    """端到端批次业务表：补充 checkpoints.db + output/ 的批次身份，
    承载监控目录、发现时间、状态、最终输出路径等元数据。

    主键沿用 LangGraph thread_id，保持与 checkpoint、containers、declarations
    等表一致；不建立外键，避免历史数据不一致导致失败。
    """

    __tablename__ = "batches"

    thread_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    watch_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    folder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    downstream_file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    upstream_root: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="unknown")
    final_output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ─────────────────────────────────────────────────────────────
# 调度 Agent 会话持久化（Session 管理方案）
# ─────────────────────────────────────────────────────────────

class ChatSession(Base):
    """调度 Agent 会话清单：左侧 sidebar 条目，支持 pin 批次、标题、待确认操作信封。

    session_id 沿用前端 crypto.randomUUID() 生成；
    pinned_thread_id 可选，操作员在 sidebar pin 当前批次方便回查；
    pending_action_json 存放待确认操作信封（同一时刻最多一个，session 删则 pending 清）。
    """

    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned_thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    pending_action_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # title_source 取值：None=未设 / "auto"=截断自动 / "llm"=LLM摘要 / "manual"=手动
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # 关系
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    tool_history: Mapped[list["ChatToolHistory"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_chat_sessions_updated", updated_at.desc()),
    )


class ChatMessage(Base):
    """调度 Agent 对话内容：user / assistant 消息流水，按 ts 排序。

    ts 为 Unix timestamp（Float），用于精确排序与 hydrate 恢复。
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("idx_chat_messages_session_ts", "session_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class ChatToolHistory(Base):
    """调度 Agent 工具调用审计流水：记录每次 tool call 的工具名、参数摘要、结果摘要与确认状态。

    confirmed: NULL=只读工具（无需确认），0=操作员拒绝，1=操作员批准。
    """

    __tablename__ = "chat_tool_history"
    __table_args__ = (
        Index("idx_chat_tool_history_session_ts", "session_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    args_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None=只读, 0=拒, 1=批
    ts: Mapped[float] = mapped_column(Float, nullable=False)

    session: Mapped["ChatSession"] = relationship(back_populates="tool_history")
