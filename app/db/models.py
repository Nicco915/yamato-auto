"""SQLAlchemy 模型：按《第三阶段.md》第 2 节设计，适配 SQLite 方言。

- factories: 工厂主表
- factory_skus: SKU 主数据子表（多语言品名 / HS 编码 / 单件重量沉淀）
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Factory(Base):
    """工厂表。"""

    __tablename__ = "factories"

    factory_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factory_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
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
