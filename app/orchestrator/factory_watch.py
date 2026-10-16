# -*- coding: utf-8 -*-
"""缺资料文件夹监控 → 自动补充（2026-10-12 需求 4，实施计划阶段二 §2.1）。

「待补充」（awaiting_factories）批次的缺口工厂资料，往往稍后由操作员
补放进上游文件夹。本模块起一个 daemon 轮询线程（默认 30 秒一轮，env
FACTORY_WATCH_SCAN_SECONDS 可调），每轮：

1. batch_store.list_by_status 捞出全部 awaiting_factories 批次
   （监控对象每轮从 DB 现查，无持久状态——uvicorn 重启后自然恢复监控）；
2. 逐批读 checkpoint：缺口 = downstream_requirements − factory_outputs，
   upstream_root 取 state 值（缺省回落 settings.upstream_root）；
3. 逐缺口工厂用 factory_match.match_factory_folder 与 Node2 同口径
   （alias_map / folder_candidates / cutoff / 批次 overrides）重匹配
   当前文件夹列表；命中且文件夹内已有受支持单据
   （folder_router.SUPPORTED_EXTS，rglob 口径与 folder_router 一致）
   → 候选「资料到达」；
4. 半拷贝防护：内存表 {(tid, factory): {相对路径: 字节数}}，首轮仅记录，
   连续 STABLE_ROUNDS=2 轮快照一致且非空才判定到达；进程重启表清空 =
   重新数两轮，可接受；
5. 触发：该批有到达工厂且批次空闲（无 board-start-{tid} /
   pre-extract-{tid} 活线程、图 next 为空或仅有 interrupt）→ 监控线程内
   同步调 service.add_factories_to_batch(tid)（内部跑图到下一个 Node5
   挂起，耗时数十秒，串行可接受；批次随即被自愈为 running/pending_review，
   下轮不再被扫描）；触发异常记 error 日志并记冷却时间戳，
   TRIGGER_COOLDOWN_SECONDS=300 秒内跳过该批。其余仍无资料的工厂由
   补充进来的这一遍图按需求 1 再次自动跳过，本模块不特判；
6. 触发成功去重（2026-10-12 调整 A，修重复触发边界）：触发成功时把
   该工厂当时的稳定快照记入 _last_triggered；后续扫描中快照完全一致
   → 跳过不再触发（人工在审核页驳回/跳过某厂后批次回到待补充、文件夹
   没动，不得推翻人工决定）；有差异（新文件/大小变/文件删减）或无记录
   → 允许触发。触发失败不记快照——冷却到期后即使文件没变也允许重试。
   进程重启表清空：重启后可能多触发一次，可接受。

不监控：非 awaiting 批次、已提取完成工厂所在文件夹（本模块只看缺口
工厂，新文件不进扫描口径）。不引新依赖（纯轮询，不用 watchdog）。
所有路径处理使用 pathlib.Path，兼容 macOS/Windows；线程名只用 ASCII。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import batch_store
from app.logging_config import logging_context
from app.nodes.folder_router import SUPPORTED_EXTS

logger = logging.getLogger(__name__)

# 轮询间隔（秒）：env FACTORY_WATCH_SCAN_SECONDS 可调，默认 30
SCAN_SECONDS = int(os.environ.get("FACTORY_WATCH_SCAN_SECONDS", "30"))
# 半拷贝防护：文件快照连续两轮一致才算「资料到达」
STABLE_ROUNDS = 2
# 触发失败冷却（秒）：同一批次触发抛异常后冷却期内跳过，避免每轮轰炸
TRIGGER_COOLDOWN_SECONDS = 300

_THREAD_NAME = "factory-watch"

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_stop_event = threading.Event()

# 稳定判定内存表 {(tid, factory): {相对路径: 字节数}}
_snapshots: dict[tuple[str, str], dict[str, int]] = {}
# 触发失败冷却表 {tid: 允许再次触发的 monotonic 时间戳}
_cooldowns: dict[str, float] = {}
# 触发成功快照表 {(tid, factory): {相对路径: 字节数}}（2026-10-12 调整 A）：
# 上次成功触发时该缺口工厂文件夹的稳定快照；快照未变不重复触发，
# 防人工驳回/跳过后批次回到待补充时被监控立刻推翻人工决定。
# 触发失败不写入（冷却到期后即使文件没变也允许重试）。
# 进程重启表清空：重启后可能多触发一次，可接受。
_last_triggered: dict[tuple[str, str], dict[str, int]] = {}
_state_lock = threading.Lock()


def start_factory_watch() -> None:
    """幂等启动监控 daemon 线程（线程名 factory-watch）；已在跑则直接返回。"""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_run_loop, daemon=True,
                                   name=_THREAD_NAME)
        _thread.start()
    logger.info("缺资料文件夹监控已启动 | 每 %d 秒一轮 | 稳定轮次=%d",
                SCAN_SECONDS, STABLE_ROUNDS)


def stop_factory_watch() -> None:
    """置停止旗标（测试/服务关停用）；不 join，daemon 线程随进程消亡。"""
    _stop_event.set()


def _run_loop() -> None:
    # 用 wait 代替 sleep：停止旗标可即时唤醒退出；先等一轮再扫，
    # 避开服务启动初期迁移/自检的资源尖峰
    while not _stop_event.wait(SCAN_SECONDS):
        try:
            _scan_once()
        except Exception:  # noqa: BLE001 单轮异常绝不允许杀死监控线程
            logger.error("[factory-watch] 扫描轮次异常", exc_info=True)


def _scan_once() -> None:
    """单轮扫描（测试可直接调，无需等 SCAN_SECONDS）。"""
    batches = batch_store.list_by_status("awaiting_factories")
    if not batches:
        logger.debug("[factory-watch] 本轮无待补充批次")
        _prune_snapshots(set())
        _prune_triggered()
        return

    alive_tids: set[str] = set()
    for batch in batches:
        tid = batch.get("thread_id")
        if not tid:
            continue
        alive_tids.add(tid)
        try:
            with logging_context(thread_id=tid):
                _scan_batch(tid)
        except Exception:  # noqa: BLE001 单批异常不拖垮整轮扫描
            logger.error("[factory-watch] 扫描批次异常 | thread_id=%s", tid,
                         exc_info=True)
    # 快照表瘦身：已不再是 awaiting 的批次（触发成功转 running / 人工
    # 强制结案 / 删除）条目清掉，防内存缓慢泄漏；批次若重回 awaiting
    # 重新数两轮即可
    _prune_snapshots(alive_tids)
    _prune_triggered()


def _scan_batch(tid: str) -> None:
    """扫描单个待补充批次：算缺口 → 重匹配 → 稳定判定 → 空闲则触发。"""
    from app.api import service  # 延迟 import：与 board.py 同哲学防循环
    from app.factory_match import (
        load_alias_map, load_folder_match_candidates, match_factory_folder)

    # 触发失败冷却期内直接跳过
    with _state_lock:
        cooldown_until = _cooldowns.get(tid, 0.0)
    if time.monotonic() < cooldown_until:
        logger.debug("[factory-watch] 批次触发冷却中，本轮跳过")
        return

    graph = service.get_graph()
    snap = graph.get_state(service._config(tid))
    values: dict[str, Any] = snap.values or {}
    requirements = values.get("downstream_requirements") or {}
    outputs = values.get("factory_outputs") or {}
    gap = [f for f in requirements if f not in outputs]
    # 决策 2（阶段四）：人工剔除厂减除出缺口——不再监控其文件夹
    # （操作员已明确不要它，资料到达也不触发补充）
    removed = service._manually_removed_factories(tid)
    if removed:
        gap = [f for f in gap if f not in removed]
    if not gap:
        # 无缺口（自愈即将转 completed），顺手清掉本批残留快照与触发快照
        _prune_snapshots(alive_tids=None, keep_tid=tid)
        _clear_triggered_for(tid)
        return

    # 触发快照按缺口瘦身：工厂已写入了结（approve）或从装箱单移除后
    # 不再是缺口，其触发快照永久失效，随本轮清掉
    gap_set = set(gap)
    with _state_lock:
        for key in [k for k in _last_triggered
                    if k[0] == tid and k[1] not in gap_set]:
            del _last_triggered[key]

    settings = get_settings()
    upstream = Path(values.get("upstream_root")
                    or settings.upstream_root).expanduser()
    if not upstream.is_dir():
        logger.warning("[factory-watch] 上游工厂目录不存在，跳过: %s", upstream)
        return

    try:
        folders = [d.name for d in upstream.iterdir() if d.is_dir()]
    except OSError as e:
        logger.warning("[factory-watch] 上游工厂目录不可读，跳过: %s", e)
        return
    alias_map = load_alias_map()
    folder_candidates = load_folder_match_candidates()
    cutoff = settings.fuzzy_match_score_cutoff
    # 与 Node2 同口径：批次级「仅本次生效」对照也参与匹配
    overrides = values.get("factory_alias_overrides") or None

    arrived: list[str] = []
    arrived_sizes: dict[str, dict[str, int]] = {}
    candidate_keys: set[tuple[str, str]] = set()
    for factory in gap:
        hit, _score, _method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff,
            overrides=overrides, folder_candidates=folder_candidates)
        if not hit:
            continue  # 文件夹未出现：继续盯
        sizes = _supported_doc_sizes(upstream / hit)
        if not sizes:
            continue  # 命中但空（含预建空文件夹）：盯单据出现
        key = (tid, factory)
        with _state_lock:
            last = _last_triggered.get(key)
        if last is not None and last == sizes:
            # 与上次成功触发时的快照完全一致：文件夹没动过——通常是人工
            # 驳回/跳过后批次回到待补充，不重复触发推翻人工决定
            # （2026-10-12 调整 A）；文件有增删/大小变化才放行进入稳定计数
            logger.debug("[factory-watch] 快照未变化，不重复触发 | 工厂=%s",
                         factory)
            continue
        candidate_keys.add(key)
        with _state_lock:
            prev = _snapshots.get(key)
            if prev is None:
                # 首轮仅记录快照，不触发（半拷贝防护）
                _snapshots[key] = sizes
                logger.debug("[factory-watch] 首轮记录快照 | 工厂=%s | 文件=%d",
                             factory, len(sizes))
            elif prev == sizes:
                # 连续两轮一致且非空 → 判定资料到达，从表中移除
                del _snapshots[key]
                arrived.append(factory)
                arrived_sizes[factory] = sizes
            else:
                # 快照变化（仍在拷贝中）→ 覆写等下轮重数
                _snapshots[key] = sizes
                logger.debug("[factory-watch] 快照变化重新计数 | 工厂=%s",
                             factory)

    # 本批不再是候选的（文件被删/文件夹消失/不再是缺口）快照条目清掉，
    # 下次出现重新数两轮
    with _state_lock:
        for key in [k for k in _snapshots
                    if k[0] == tid and k not in candidate_keys]:
            del _snapshots[key]

    if not arrived:
        return
    if not _batch_idle(tid, snap):
        # 批次忙（一键启动/预提取/图在跑）：本轮不触发；到达工厂的快照
        # 已移除，下轮重新数两轮，保守可接受
        logger.info("[factory-watch] 资料到达但批次忙，下轮再试 | 到达=%s",
                    arrived)
        return

    logger.info("[factory-watch] 资料到达，自动补充工厂 | thread_id=%s | 到达=%s",
                tid, arrived)
    try:
        result = service.add_factories_to_batch(tid)
    except Exception as e:  # noqa: BLE001 失败记冷却，冷却期内不再轰炸
        # 触发失败不记 _last_triggered 快照：冷却到期后即使文件没变也允许重试
        logger.error("[factory-watch] 自动补充失败 | thread_id=%s | %s: %s",
                     tid, type(e).__name__, e, exc_info=True)
        with _state_lock:
            _cooldowns[tid] = time.monotonic() + TRIGGER_COOLDOWN_SECONDS
        return
    with _state_lock:
        _cooldowns.pop(tid, None)
        # 触发成功：记录各到达工厂当时的稳定快照，之后快照未变不重复触发
        for f in arrived:
            _last_triggered[(tid, f)] = arrived_sizes.get(f, {})
    logger.info("[factory-watch] 自动补充完成 | thread_id=%s | added=%s | "
                "status=%s", tid, result.get("added"), result.get("status"))


def _supported_doc_sizes(folder: Path) -> dict[str, int]:
    """受支持单据快照 {相对路径: 字节数}；口径与 folder_router 收集
    source_documents 一致（rglob + SUPPORTED_EXTS）。读取失败按空处理。"""
    sizes: dict[str, int] = {}
    try:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                try:
                    sizes[p.relative_to(folder).as_posix()] = p.stat().st_size
                except OSError:
                    continue  # 文件正在被改写/删除，本轮跳过该文件
    except OSError:
        return {}
    return sizes


def _batch_idle(tid: str, snap: Any) -> bool:
    """批次空闲判定：无 board-start-{tid} / pre-extract-{tid} 活线程，
    且图 next 为空或仅有 interrupt（挂起等人工不算占用——awaiting 批次
    正常已跑完，next 为空）。"""
    busy_names = (f"board-start-{tid}", f"pre-extract-{tid}")
    for t in threading.enumerate():
        if t.name in busy_names and t.is_alive():
            return False
    has_interrupt = any(task.interrupts for task in snap.tasks)
    return has_interrupt or not snap.next


def _prune_snapshots(alive_tids: set[str] | None,
                     keep_tid: str | None = None) -> None:
    """清掉不再受监控批次的快照条目。

    alive_tids 给出本轮全部 awaiting 批次（None 表示只按 keep_tid 清理）。
    """
    with _state_lock:
        for key in list(_snapshots):
            if keep_tid is not None and key[0] == keep_tid:
                del _snapshots[key]
            elif alive_tids is not None and key[0] not in alive_tids:
                del _snapshots[key]


def _clear_triggered_for(tid: str) -> None:
    """清掉某批全部触发成功快照（批次无缺口自愈完成时调用）。"""
    with _state_lock:
        for key in [k for k in _last_triggered if k[0] == tid]:
            del _last_triggered[key]


def _prune_triggered() -> None:
    """触发成功快照表瘦身：只清「批次已删除」的条目。

    刻意不按 awaiting 状态清（2026-10-12 调整 A 的关键）：触发成功后批次
    转 running/pending_review 即离开 awaiting，若此时清掉条目，人工驳回/
    跳过使批次重回待补充时就丢了「快照未变不重复触发」的依据，等于推翻
    人工决定——正是本调整要修的边界。条目回收靠三处：批次删除（本函数）、
    工厂离开缺口（_scan_batch 按 gap 清）、批次无缺口（_clear_triggered_for）。
    """
    with _state_lock:
        tids = {k[0] for k in _last_triggered}
    for tid in tids:
        if batch_store.get_batch(tid) is None:
            _clear_triggered_for(tid)
