"""Batch 业务表 CRUD 封装。

端到端升级新增：为扫描发现、流水线状态图、复盘学习提供持久的批次元数据。
所有写入都是辅助设施，失败只记录警告，不阻塞主流程。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.db.models import Batch
from app.db.session import get_session

logger = logging.getLogger(__name__)


def get_batch(thread_id: str) -> dict[str, Any] | None:
    """按 thread_id 查询 batch 记录；不存在返回 None。"""
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
        logger.warning("写入 batch %s 失败: %s", thread_id, exc)
        return False


def update_status(
    thread_id: str,
    status: str,
    *,
    final_output_path: str | None = None,
) -> bool:
    """更新 batch 状态；若 status 为 completed 则自动填充 completed_at。"""
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
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("更新 batch %s 状态失败: %s", thread_id, exc)
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
        logger.warning("删除 batch %s 失败: %s", thread_id, exc)
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
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
