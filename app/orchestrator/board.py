# -*- coding: utf-8 -*-
"""监控目录看板服务。

把「监控目录总览」从调度 Agent 对话能力提为常驻可视化看板（/board）：
- board_state：三档分类（已完成/执行中/未执行候选），复用
  discovery.match_watch_folders 三重匹配；未完成批次顺带借
  get_pipeline_state 以 checkpoint 自愈校正滞留状态，并附工厂进度；
- mark_done：未执行候选 → 已完成（历史文件夹标记，扫描不再列出）；
- start_from_board：看板一键启动批次——确认动作发生在看板弹窗
  （一次一确认的确认门由前端弹窗承担），这里预写 running 批次行后
  后台线程跑 start_batch_from_scan，HTTP 立即返回不阻塞。

所有路径处理使用 pathlib.Path，兼容 macOS/Windows。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import batch_store
from app.orchestrator import discovery

logger = logging.getLogger(__name__)


def _watch_path() -> Path:
    """返回监控目录 Path；未配置/不存在抛 ValueError（路由层转 422）。"""
    settings = get_settings()
    if not settings.watch_dir:
        raise ValueError("监控目录未配置，请先在对话页设置监控目录")
    watch = Path(settings.watch_dir).expanduser()
    if not watch.is_dir():
        raise ValueError(f"监控目录不存在: {settings.watch_dir}")
    return watch


def board_state() -> dict[str, Any]:
    """看板三档数据：done / in_progress / candidates。

    - 配对：discovery.match_watch_folders（thread_id / folder_name / 路径归属）；
    - 执行中档：逐批 get_pipeline_state（顺带自愈回写滞留状态），
      附 current_phase 与工厂进度 done/total；
    - 候选档：附装箱单/ MX2 探测结果（供启动确认弹窗预填与按钮置灰）。
    """
    watch = _watch_path()
    matched = discovery.match_watch_folders(watch)

    done: list[dict] = []
    in_progress: list[dict] = []
    candidates: list[dict] = []

    for child in sorted(watch.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        rec = matched.get(child.name)
        if rec is not None and rec.get("status") != "completed":
            # 自愈：以 checkpoint 为权威源校正滞留状态 + 取流水线进度
            try:
                from app.orchestrator.pipeline_state import get_pipeline_state
                state = get_pipeline_state(rec["thread_id"])
                fresh = state.get("batch") or {}
                if fresh.get("status") and fresh["status"] != rec.get("status"):
                    rec = batch_store.get_batch(rec["thread_id"]) or fresh
                    matched[child.name] = rec
                extract = state.get("extract") or {}
                rec["_phase"] = state.get("current_phase")
                rec["_done_factories"] = len(extract.get("done_factories") or [])
                rec["_pending_factories"] = len(extract.get("pending_factories") or [])
                rec["_current_factory"] = extract.get("current_factory")
            except Exception:  # noqa: BLE001 自愈失败不阻塞看板
                pass
        if rec is not None and rec.get("status") == "completed":
            done.append({"folder_name": child.name,
                         "thread_id": rec["thread_id"],
                         "completed_at": rec.get("completed_at")})
        elif rec is not None:
            in_progress.append({
                "folder_name": child.name,
                "thread_id": rec["thread_id"],
                "status": rec.get("status"),
                "current_phase": rec.get("_phase"),
                "done_factories": rec.get("_done_factories", 0),
                "pending_factories": rec.get("_pending_factories", 0),
                "current_factory": rec.get("_current_factory"),
            })
        else:
            downstream = discovery.discover_downstream_files(child)
            mx2 = discovery.discover_mx2_files(child)
            candidates.append({
                "folder_name": child.name,
                "default_thread_id": discovery.pick_default_thread_id(child.name),
                "downstream_candidates": [str(p) for p in downstream],
                "mx2_count": len(mx2),
                "has_content": bool(downstream),
            })
    return {"watch_dir": str(watch),
            "total": len(done) + len(in_progress) + len(candidates),
            "done": done, "in_progress": in_progress, "candidates": candidates}


def mark_done(folder_name: str) -> dict[str, Any]:
    """未执行候选 → 已完成（只写 batches 表元数据，不动文件夹）。

    thread_id 取文件夹名原文（与 mark_batch_done 工具/扫描去重同口径）。
    查重用 match_watch_folders 三重匹配——手动建批（thread_id 与文件夹名
    无关）的文件夹也不能被重复标记。
    """
    folder_name = (folder_name or "").strip()
    if not folder_name:
        raise ValueError("文件夹名不能为空")
    watch = _watch_path()
    folder = watch / folder_name
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder_name}")

    matched = discovery.match_watch_folders(watch)
    existing = matched.get(folder_name)
    if existing is not None:
        if existing.get("status") == "completed":
            return {"ok": True, "message": f"「{folder_name}」已是已完成状态",
                    "thread_id": existing["thread_id"]}
        raise FileExistsError(
            f"「{folder_name}」已有批次记录（{existing['thread_id']}，"
            f"状态 {existing.get('status')}），不能标记为已完成")

    batch_store.upsert_batch(
        folder_name,
        watch_dir=str(watch),
        folder_name=folder_name,
        status="completed",
    )
    batch_store.update_status(folder_name, "completed")  # 填充 completed_at
    logger.info("看板标记完成 | folder=%s", folder_name)
    return {"ok": True,
            "message": f"已标记「{folder_name}」为已完成，之后扫描不再列出",
            "thread_id": folder_name}


def start_from_board(
    folder_name: str,
    thread_id: str | None = None,
    downstream_file_path: str | None = None,
) -> dict[str, Any]:
    """看板一键启动批次：预写 running 批次行 → 后台线程跑提取图。

    确认门由看板确认弹窗承担（一次一确认）；本函数立即返回 thread_id，
    前端随即跳转 /chat?thread_id= 由对话页轮询跟踪进度。

    后台完成/失败后由 pipeline_state 自愈校正展示状态；异常记 error 日志
    并把批次行置为 error。
    """
    folder_name = (folder_name or "").strip()
    if not folder_name:
        raise ValueError("文件夹名不能为空")
    watch = _watch_path()
    folder = watch / folder_name
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder_name}")

    tid = (thread_id or "").strip() or discovery.pick_default_thread_id(folder_name)

    # 查重：checkpoint 已存在 或 文件夹已被其他批次占用
    from app.api import service
    if service.get_order_state(tid).get("exists"):
        raise FileExistsError(f"批次号已存在: {tid}")
    matched = discovery.match_watch_folders(watch)
    existing = matched.get(folder_name)
    if existing is not None:
        raise FileExistsError(
            f"「{folder_name}」已有批次记录（{existing['thread_id']}）")

    # 预写 running 行：看板立刻把文件夹挪到「执行中」档
    #（start_batch_from_scan 的行要等跑到首个挂起点才写，预写消除空窗）
    batch_store.upsert_batch(
        tid,
        watch_dir=str(watch),
        folder_name=folder_name,
        status="running",
    )

    def _run() -> None:
        try:
            result = service.start_batch_from_scan(
                folder_name,
                thread_id=tid,
                downstream_file_path=downstream_file_path,
            )
            logger.info("看板启动批次完成首段 | thread_id=%s | status=%s",
                        tid, result.get("status"))
        except Exception:  # noqa: BLE001 后台线程异常不抛出，落状态 + 日志
            logger.exception("看板启动批次失败 | thread_id=%s", tid)
            batch_store.update_status(tid, "error")

    threading.Thread(target=_run, daemon=True,
                     name=f"board-start-{tid}").start()
    logger.info("看板启动批次 | folder=%s | thread_id=%s", folder_name, tid)
    return {"ok": True, "thread_id": tid,
            "message": f"批次 {tid} 已启动，跳转到对话页跟踪进度"}
