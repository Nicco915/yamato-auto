"""数据库会话工厂与初始化工具。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Base

_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    """惰性创建全局 Engine（SQLite，允许跨线程使用以配合 asyncio.to_thread）。"""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            f"sqlite:///{settings.master_db_abs}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(_engine)  # 首次运行自动建表
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Session:
    """获取一个新会话（调用方负责 close，推荐 with 语法）。"""
    get_engine()
    return _SessionLocal()
