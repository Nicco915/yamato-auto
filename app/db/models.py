"""SQLAlchemy 模型：按《第三阶段.md》第 2 节设计，适配 SQLite 方言。

- factories: 工厂主表
- factory_skus: SKU 主数据子表（多语言品名 / HS 编码 / 单件重量沉淀）
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
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
    sku_code = Column(String, ForeignKey('factory_skus.sku_code'), nullable=True)   # SKU 级精确匹配
    factory_id = Column(Integer, ForeignKey('factories.factory_id'), nullable=True)
    hs_code = Column(String, nullable=True)              # 税号
    supplier_name = Column(String, nullable=True)        # 供应商报关全称（如 青岛东基恒塑料包装有限公司）
    inspection_required = Column(Boolean, nullable=False, default=False)  # 商检（产品级）
    name_en = Column(String, nullable=True)              # 英文品名（产品组一）
    unit_code = Column(String, nullable=True)            # 计量单位代码（自定义七，如 '007'）
    is_incomplete = Column(Boolean, nullable=False, default=False)  # 待完善标记（Node5 反向生成时 True）
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


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
