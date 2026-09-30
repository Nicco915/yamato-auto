"""Batch 业务表 CRUD 封装。

端到端升级新增：为扫描发现、流水线状态图、复盘学习提供持久的批次元数据。
所有写入都是辅助设施，失败只记录警告，不阻塞主流程。
关键写（upsert/update_status/delete/mark_error）失败升级为 error 级日志：
这些写失败会导致监控看板状态滞留（如永远卡在「进行中」的僵尸行），需要显眼。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db.models import Batch
from app.db.session import get_engine, get_session

logger = logging.getLogger(__name__)

# ensure_error_columns 一次性幂等迁移标志（模块级 + 锁，线程安全）
_columns_ensured = False
_columns_lock = threading.Lock()


def ensure_error_columns() -> None:
    """老库幂等补列：batches 加 error_message / error_at（create_all 不会给已有表加列）。

    模块级标志保证每进程只执行一次；异常只记日志不抛，不阻塞主流程——
    但若加列失败，后续写 error_message 会报错，故失败时记 error 级。
    """
    global _columns_ensured
    if _columns_ensured:
        return
    with _columns_lock:
        if _columns_ensured:
            return
        try:
            engine = get_engine()
            with engine.connect() as conn:
                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(batches)")).fetchall()
                }
                if "error_message" not in cols:
                    conn.execute(
                        text("ALTER TABLE batches ADD COLUMN error_message VARCHAR(1024)")
                    )
                    logger.info("batches 表补列 error_message 完成")
                if "error_at" not in cols:
                    conn.execute(text("ALTER TABLE batches ADD COLUMN error_at DATETIME"))
                    logger.info("batches 表补列 error_at 完成")
                conn.commit()
            _columns_ensured = True
        except Exception as exc:  # noqa: BLE001
            # 加列失败会导致后续写 error_message 全部失败（状态滞留形态），升 error 级
            logger.error("batches 表补列 error_message/error_at 失败: %s", exc)


def get_batch(thread_id: str) -> dict[str, Any] | None:
    """按 thread_id 查询 batch 记录；不存在返回 None。"""
    ensure_error_columns()  # 读路径也要补列：老库缺列时 SELECT 直接报 no such column
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return None
            return _to_dict(row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 batch %s 失败: %s", thread_id, exc)
        return None


def list_batches(
    *,
    status: str | None = None,
    watch_dir: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """列出 batch 记录，可选按状态/监控目录过滤。"""
    ensure_error_columns()  # 读路径也要补列：老库缺列时 SELECT 直接报 no such column
    try:
        with get_session() as s:
            q = s.query(Batch)
            if status:
                q = q.where(Batch.status == status)
            if watch_dir:
                q = q.where(Batch.watch_dir == watch_dir)
            rows = q.order_by(Batch.created_at.desc()).limit(limit).all()
            return [_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("列出 batch 失败: %s", exc)
        return []


def upsert_batch(
    thread_id: str,
    *,
    watch_dir: str | None = None,
    folder_name: str | None = None,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    status: str | None = None,
    final_output_path: str | None = None,
) -> bool:
    """创建或更新 batch 记录。thread_id 不存在则插入，存在则更新传入的非空字段。"""
    ensure_error_columns()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            now = datetime.utcnow()
            if row is None:
                row = Batch(
                    thread_id=thread_id,
                    watch_dir=watch_dir,
                    folder_name=folder_name,
                    downstream_file_path=downstream_file_path,
                    upstream_root=upstream_root,
                    status=status or "unknown",
                    final_output_path=final_output_path,
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
            else:
                if watch_dir is not None:
                    row.watch_dir = watch_dir
                if folder_name is not None:
                    row.folder_name = folder_name
                if downstream_file_path is not None:
                    row.downstream_file_path = downstream_file_path
                if upstream_root is not None:
                    row.upstream_root = upstream_root
                if status is not None:
                    row.status = status
                if final_output_path is not None:
                    row.final_output_path = final_output_path
                row.updated_at = now
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("写入 batch %s 失败: %s", thread_id, exc)
        return False


def update_status(
    thread_id: str,
    status: str,
    *,
    final_output_path: str | None = None,
) -> bool:
    """更新 batch 状态；若 status 为 completed 则自动填充 completed_at。

    非 error 状态时清空 error_message/error_at（恢复后不留旧错误）。
    """
    ensure_error_columns()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            row.status = status
            row.updated_at = datetime.utcnow()
            if final_output_path is not None:
                row.final_output_path = final_output_path
            if status == "completed":
                row.completed_at = row.updated_at
            if status != "error":
                row.error_message = None
                row.error_at = None
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("更新 batch %s 状态失败: %s", thread_id, exc)
        return False


def mark_error(thread_id: str, message: str) -> bool:
    """将 batch 置为 error 状态并留痕失败原因；行不存在返回 False。"""
    ensure_error_columns()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            now = datetime.utcnow()
            row.status = "error"
            row.error_message = message[:500]
            row.error_at = now
            row.updated_at = now
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("标记 batch %s 为 error 失败: %s", thread_id, exc)
        return False


def delete_batch(thread_id: str) -> bool:
    """删除 batch 记录；主流程删除 checkpoint 时同步调用。"""
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("删除 batch %s 失败: %s", thread_id, exc)
        return False


def _to_dict(row: Batch) -> dict[str, Any]:
    return {
        "thread_id": row.thread_id,
        "watch_dir": row.watch_dir,
        "folder_name": row.folder_name,
        "downstream_file_path": row.downstream_file_path,
        "upstream_root": row.upstream_root,
        "status": row.status,
        "final_output_path": row.final_output_path,
        "error_message": row.error_message,
        "error_at": row.error_at.isoformat() if row.error_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
