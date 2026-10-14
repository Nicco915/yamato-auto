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


class BatchAbortedError(Exception):
    """用户主动终止批次的控制流异常（2026-10-14 批次终止）。

    图节点（folder_router/export_node）在厂边界检查 cancel_requested 标志
    命中时抛出，由 service 六条 graph.stream 包装层捕获并转 aborted 状态
    ——这是用户主动语义，不是系统异常，绝不走 _mark_batch_error。

    定义在本文件的理由：控制流异常与 cancel_requested 标志同源（同一处
    事实源、同一组读写函数）；nodes 层（folder_router 等）import
    app.db.batch_store 无循环依赖（folder_router 现已 import app.config /
    app.factory_match，同层先例）。
    """


# ensure_error_columns 一次性幂等迁移标志（模块级 + 锁，线程安全）
_columns_ensured = False
_columns_lock = threading.Lock()

# ensure_force_completed_column 一次性幂等迁移标志（同上）
_force_col_ensured = False
_force_col_lock = threading.Lock()

# ensure_cancel_requested_column 一次性幂等迁移标志（同上）
_cancel_col_ensured = False
_cancel_col_lock = threading.Lock()


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


def ensure_force_completed_column() -> None:
    """老库幂等补列：batches 加 force_completed（2026-10-12 待补充状态，
    人工强制结案防自愈打回标志；create_all 不会给已有表加列）。

    与 ensure_error_columns 同模式：模块级标志每进程只执行一次；异常只记
    日志不抛——但加列失败会导致读写 force_completed 报错，故失败记 error 级。
    所有读写路径都调用（读路径也要补列：老库缺列时 SELECT 直接报
    no such column，与 error_message 同理）。
    """
    global _force_col_ensured
    if _force_col_ensured:
        return
    with _force_col_lock:
        if _force_col_ensured:
            return
        try:
            engine = get_engine()
            with engine.connect() as conn:
                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(batches)")).fetchall()
                }
                if "force_completed" not in cols:
                    conn.execute(
                        text("ALTER TABLE batches ADD COLUMN force_completed "
                             "BOOLEAN DEFAULT 0")
                    )
                    logger.info("batches 表补列 force_completed 完成")
                conn.commit()
            _force_col_ensured = True
        except Exception as exc:  # noqa: BLE001
            logger.error("batches 表补列 force_completed 失败: %s", exc)


def ensure_cancel_requested_column() -> None:
    """老库幂等补列：batches 加 cancel_requested（2026-10-14 协作式终止标志；
    create_all 不会给已有表加列）。

    与 ensure_force_completed_column 同模式：模块级标志每进程只执行一次；
    异常只记日志不抛——但加列失败会导致读写 cancel_requested 报错，故失败
    记 error 级。所有读写路径都调用（读路径也要补列：老库缺列时 SELECT
    直接报 no such column）。
    """
    global _cancel_col_ensured
    if _cancel_col_ensured:
        return
    with _cancel_col_lock:
        if _cancel_col_ensured:
            return
        try:
            engine = get_engine()
            with engine.connect() as conn:
                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(batches)")).fetchall()
                }
                if "cancel_requested" not in cols:
                    conn.execute(
                        text("ALTER TABLE batches ADD COLUMN cancel_requested "
                             "BOOLEAN DEFAULT 0")
                    )
                    logger.info("batches 表补列 cancel_requested 完成")
                conn.commit()
            _cancel_col_ensured = True
        except Exception as exc:  # noqa: BLE001
            logger.error("batches 表补列 cancel_requested 失败: %s", exc)


def get_batch(thread_id: str) -> dict[str, Any] | None:
    """按 thread_id 查询 batch 记录；不存在返回 None。"""
    ensure_error_columns()  # 读路径也要补列：老库缺列时 SELECT 直接报 no such column
    ensure_force_completed_column()
    ensure_cancel_requested_column()
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
    ensure_force_completed_column()
    ensure_cancel_requested_column()
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
    ensure_force_completed_column()
    ensure_cancel_requested_column()
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
    """更新 batch 状态；若 status 为 completed/completed_with_errors 则自动填充 completed_at。

    非 error 状态时清空 error_message/error_at（恢复后不留旧错误）。
    转 completed 时异步触发自动备份（2026-10-12 数据备份机制），
    completed_with_errors 不触发；触发失败只记 warning，不影响返回 True。
    aborted（2026-10-14 批次终止）不填 completed_at、不触发结案备份。
    """
    ensure_error_columns()
    ensure_force_completed_column()
    ensure_cancel_requested_column()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            old_status = row.status
            row.status = status
            row.updated_at = datetime.utcnow()
            if final_output_path is not None:
                row.final_output_path = final_output_path
            if status in ("completed", "completed_with_errors"):
                row.completed_at = row.completed_at or row.updated_at
            if status != "error":
                row.error_message = None
                row.error_at = None
            s.commit()
        if status == "completed" and old_status != "completed":
            try:
                from app.orchestrator import backup as _backup
                _backup.trigger_async(f"batch_completed:{thread_id}")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "结案自动备份触发失败 | thread_id=%s", thread_id,
                    exc_info=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("更新 batch %s 状态失败: %s", thread_id, exc)
        return False


def mark_error(thread_id: str, message: str) -> bool:
    """将 batch 置为 error 状态并留痕失败原因；行不存在返回 False。"""
    ensure_error_columns()
    ensure_force_completed_column()
    ensure_cancel_requested_column()
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


def set_force_completed(thread_id: str) -> bool:
    """置人工强制结案标志（2026-10-12 待补充状态）：pipeline_state 自愈
    见 force_completed=True 不再把批次打回 awaiting_factories。
    行不存在返回 False；写失败记 error 日志返回 False。"""
    ensure_force_completed_column()
    ensure_cancel_requested_column()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            row.force_completed = True
            row.updated_at = datetime.utcnow()
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("标记 batch %s 强制结案失败: %s", thread_id, exc)
        return False


def set_cancel_requested(thread_id: str) -> bool:
    """置协作式终止标志（2026-10-14 批次终止）：图内 Node2/Node7 检查点
    与预提取循环在厂边界见 True 即停。行不存在返回 False；
    写失败记 error 日志返回 False。"""
    ensure_cancel_requested_column()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            row.cancel_requested = True
            row.updated_at = datetime.utcnow()
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("置 batch %s 终止标志失败: %s", thread_id, exc)
        return False


def clear_cancel_requested(thread_id: str) -> bool:
    """清协作式终止标志（协作停止落 aborted 后 / 新一轮运行起跑时调用）。
    行不存在返回 False；写失败记 error 日志返回 False。"""
    ensure_cancel_requested_column()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            row.cancel_requested = False
            row.updated_at = datetime.utcnow()
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("清 batch %s 终止标志失败: %s", thread_id, exc)
        return False


def is_cancel_requested(thread_id: str) -> bool:
    """查询终止标志；行不存在或查询失败一律返回 False（宁可不拦，
    不把查询故障变成批次停摆）。"""
    ensure_cancel_requested_column()
    try:
        with get_session() as s:
            row = s.get(Batch, thread_id)
            if row is None:
                return False
            return bool(row.cancel_requested)
    except Exception as exc:  # noqa: BLE001
        logger.warning("查询 batch %s 终止标志失败（按未请求处理）: %s",
                       thread_id, exc)
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


def list_by_status(status: str, *, limit: int = 1000) -> list[dict[str, Any]]:
    """按状态列出 batch 记录（2026-10-12 缺资料文件夹监控：factory_watch
    每轮捞 status == "awaiting_factories" 的批次）。

    list_batches(status=...) 的语义化薄封装；查询失败返回 []（同
    list_batches 的兜底哲学，监控线程下一轮重试即可）。
    """
    return list_batches(status=status, limit=limit)


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
        "force_completed": bool(row.force_completed),
        "cancel_requested": bool(row.cancel_requested),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
