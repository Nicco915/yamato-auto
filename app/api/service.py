"""Service 层：LangGraph 跑图逻辑集中在这里，路由层保持极薄。

【Celery 迁移预留接缝】
当前为同步实现（由路由层用 asyncio.to_thread 包裹调用，避免阻塞事件循环）。
日后迁移 Celery+Redis 时：
1. 把 run_until_interrupt() 整体下沉为 Celery task（worker 内直接调用即可，
   函数签名不变）；
2. 路由改为 task.delay(...) 返回 task_id，再加一个轮询接口查 AsyncResult；
3. resume_order() 因只做写 Excel+落库，耗时短，可保持同步接口不变。
"""
import contextlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from copy import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ormsgpack
from langgraph.types import Command
from openpyxl import load_workbook
from sqlalchemy import select

from app.config import get_settings
from app.db import batch_store
from app.db.models import ReviewAudit
from app.db.session import get_session
from app.extraction.llm_client import FatalLLMError, usage_tracker
from app.orchestrator import copy_snapshot, discovery, factory_setup, source_diff
from app.extraction.session import SESSIONS_DIR
from app.graph import NODE2, NODE4, NODE5, NODE6, NODE7, get_graph
from app.logging_config import logging_context

logger = logging.getLogger(__name__)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _safe_div(total, qty):
    """安全除法：返回 (结果, 公式字符串, 错误信息)。"""
    try:
        formula = f"{total} / {qty}"
        return total / qty, formula, None
    except (ZeroDivisionError, TypeError) as e:
        return None, f"{total} / {qty}", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 节点级进度钩子（W4a）：graph.stream 的 updates 事件 → exec_progress 回调
# ---------------------------------------------------------------------------

def _make_progress_emitter(
    on_progress: Callable[[dict], None] | None,
    seed: dict[str, Any] | None = None,
) -> Callable[[dict], None]:
    """构造 stream 事件回调：从 updates 事件提取 node1/2/3/6 节点级进度。

    产出事件至少含 node/factory/done/total/message（type/tool/thread_id
    由调用方包装补充）。done/total 口径同 _summarize_snapshot：
    total = 装箱单工厂集 ∩ factory_filter；done = 已写回完成的工厂数
    （当前处理中的工厂不计入，node2 消息里以 done+1 作序号）。
    seed 用于 resume/rerun 场景预置跟踪器（resume 不再经过 node1，
    工厂全集/队列/当前工厂须从挂起现场取）。
    事件构造与回调整体 try/except：on_progress 抛异常绝不影响跑图。
    """
    from app.graph import NODE1, NODE2, NODE3, NODE4B, NODE6

    seed = seed or {}
    allow = set(seed["factory_filter"]) if seed.get("factory_filter") else None
    tracker: dict[str, Any] = {"total_set": set(), "pending": set(), "current": None}
    if seed.get("downstream_requirements"):
        total_set = set((seed.get("downstream_requirements") or {}).keys())
        if allow:
            total_set &= allow
        tracker["total_set"] = total_set
    tracker["pending"] = set(seed.get("pending_factories") or [])
    tracker["current"] = (seed.get("current_factory_data") or {}).get("factory_name")

    def emit(event: dict) -> None:
        if on_progress is None:
            return
        try:
            for node, value in event.items():
                if not isinstance(value, dict):
                    continue
                # 先更新跟踪器（updates 事件值即节点返回的 state 增量）
                if "downstream_requirements" in value:
                    total_set = set((value.get("downstream_requirements") or {}).keys())
                    if allow:
                        total_set &= allow
                    tracker["total_set"] = total_set
                if "pending_factories" in value:
                    tracker["pending"] = set(value.get("pending_factories") or [])
                current = (value.get("current_factory_data") or {}).get("factory_name")
                if current:
                    tracker["current"] = current

                total = len(tracker["total_set"])
                popped = len(tracker["total_set"] - tracker["pending"])
                factory = tracker["current"]
                if node == NODE1:
                    progress = {"node": node, "factory": None, "done": 0,
                                "total": total,
                                "message": f"装箱单解析完成，共 {total} 个工厂"}
                elif node == NODE2 and factory:
                    cur_data = value.get("current_factory_data") or {}
                    if cur_data.get("is_final_attempt"):
                        # W6a 暂缓二遍重试：不带序号（此时 pending 集合不变，
                        # done+1 序号口径会失真）
                        done = max(popped - 1, 0)
                        progress = {"node": node, "factory": factory, "done": done,
                                    "total": total,
                                    "message": f"开始重试 {factory}（暂缓重试）"}
                    else:
                        # 当前工厂刚出队列、在处理中，不计入 done；序号 = done+1
                        done = max(popped - 1, 0)
                        progress = {"node": node, "factory": factory, "done": done,
                                    "total": total,
                                    "message": f"开始处理 {factory}（{done + 1}/{total}）"}
                elif node == NODE3 and factory:
                    done = max(popped - 1, 0)
                    progress = {"node": node, "factory": factory, "done": done,
                                "total": total, "message": f"{factory} 提取完成"}
                elif node == NODE4B and factory:
                    # W6a 暂缓：done 不计、total 不变（暂缓厂未写回不算完成）
                    done = max(popped - 1, 0)
                    progress = {"node": node, "factory": factory, "done": done,
                                "total": total,
                                "message": f"{factory} 提取失败，已暂缓，将在其余工厂处理完后重试"}
                elif node == NODE6 and factory:
                    # 写回完成：当前工厂计入 done
                    progress = {"node": node, "factory": factory, "done": popped,
                                "total": total, "message": f"{factory} 写回完成"}
                else:
                    continue
                on_progress(progress)
        except Exception:  # noqa: BLE001 进度是辅助设施，抛异常绝不影响跑图
            logger.debug("on_progress 回调异常已忽略", exc_info=True)

    return emit


# ---------------------------------------------------------------------------
# 后台预提取（2026-07-28）：审核不阻塞提取
# ---------------------------------------------------------------------------
# 图首次 interrupt（工厂 A 挂起等审核）时，后台线程开始对剩余工厂逐个预提取。
# 每个工厂走：Node2 匹配文件夹 → target_identifier 识别正确单据 → Node3 调 LLM
# → 结果原子写入 sessions/{工厂}.json。图内 Node3 发现缓存命中时跳过 LLM。
# 后台线程串行（一次一个工厂），不并发打 API——只一个人用，不值得为并行 LLM
# 增加复杂度。提取并发只解决"审核时别闲着"的问题。
#
# 线程安全：预提取结果经原子 rename（tmp → target）落盘，图内 Node3 读缓存
# 时要么看到旧文件、要么看到完整新文件，不会读到半截 JSON。最坏情况是缓存
# 未命中（后台还没跑完），此时图 Node3 走正常提取流程，无数据丢失。

_PRE_EXTRACT_LOCK = threading.Lock()  # 保证同一批次只有一个预提取线程
_known_running: set[str] = set()      # 进程级已知在跑的批次


# ---------------------------------------------------------------------------
# 预提取进度落盘（2026-08-03）：对话页批次实时进度条的数据源
# ---------------------------------------------------------------------------
# 后台预提取线程每次状态变化即原子重写进度 JSON（tmp → rename，与 session
# 原子写模式一致），UI 批次端点原样带出给对话页轮询渲染——纯确定性数据，
# 不过 LLM。进度是辅助设施：写文件失败只记日志，绝不影响预提取本身。


def _write_batch_config(batch_id: str, config: dict[str, Any]) -> None:
    """写入批次配置文件 output/{batch_id}/batch_config.json。

    配置内容：thread_id、路径、创建时间、最后运行时间、运行次数。
    写文件失败只记日志，绝不影响主流程。
    """
    try:
        settings = get_settings()
        batch_dir = settings.batch_output_dir(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        config_path = batch_dir / "batch_config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        logger.exception("[batch_config] 写入批次配置文件失败 batch_id=%s", batch_id)


def _read_batch_config(batch_id: str) -> dict[str, Any] | None:
    """读取批次配置文件，不存在或损坏时返回 None。"""
    try:
        settings = get_settings()
        config_path = settings.batch_output_dir(batch_id) / "batch_config.json"
        if not config_path.exists():
            return None
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def update_batch_paths(
    batch_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    reset_checkpoint: bool = False,
) -> dict[str, Any]:
    """更新批次路径配置，可选重置 checkpoint。

    Args:
        batch_id: 批次号
        downstream_file_path: 新装箱单路径（None=不改）
        upstream_root: 新工厂文件夹路径（None=不改）
        reset_checkpoint: 是否重置 checkpoint（下次执行用新路径）

    Returns:
        {"updated": True/False, "config": {...}, "checkpoint_reset": True/False}

    Raises:
        ValueError: 批次不存在或路径无效
    """
    config = _read_batch_config(batch_id)
    if config is None:
        raise ValueError(f"批次不存在: {batch_id}")

    # 更新路径
    if downstream_file_path:
        d_path = Path(downstream_file_path).expanduser()
        if not d_path.is_file():
            raise ValueError(f"下游装箱单路径不存在或不是文件: {downstream_file_path}")
        config["downstream_file_path"] = str(d_path)
    if upstream_root:
        u_path = Path(upstream_root).expanduser()
        if not u_path.is_dir():
            raise ValueError(f"上游工厂文件夹路径不存在或不是目录: {upstream_root}")
        config["upstream_root"] = str(u_path)

    _write_batch_config(batch_id, config)

    # 决策 9：换了原件 → 指纹基线跟随新文件（旧文件指纹对新文件无意义）
    if downstream_file_path:
        source_diff.record_fingerprint(batch_id, config["downstream_file_path"])

    checkpoint_reset = False
    if reset_checkpoint:
        from langgraph.graph import START
        graph = get_graph()
        cfg = _config(batch_id)
        update = {}
        if downstream_file_path:
            update["downstream_file_path"] = config["downstream_file_path"]
        if upstream_root:
            update["upstream_root"] = config["upstream_root"]
        graph.update_state(cfg, update, as_node=START)
        checkpoint_reset = True

    return {
        "updated": True,
        "config": config,
        "checkpoint_reset": checkpoint_reset,
    }


def rerun_batch(
    batch_id: str,
    on_progress: Callable[[dict], None] | None = None,
    confirm_discard_manual: bool = False,
) -> dict[str, Any]:
    """完全重跑批次：先归档当前 output 产物到 _history/，再清空 sessions，再重置 checkpoint 重跑。

    重跑前把 output/{safe(batch_id)}/ 目录整体（containers/、declarations/、
    batch_state.json、batch_config.json）+ 顶层 {safe(batch_id)}_filled.xlsx
    一起搬到 output/_history/{safe(batch_id)}/r{N}_{ts}/，按 run_count 编号。

    然后清空 data/sessions/{safe(batch_id)}/*.json（不删目录）。

    这样每批次独立缓存；rerun 不污染历史产物，所有历史 run 可审计回看。

    决策 10（完全重跑警告确认门）：副本快照检测到人工改动痕迹（diff 非空或
    protected/tombstones 非空）且 confirm_discard_manual=False → 抛
    ManualDiscardError 拒绝执行（路由层转 409 code=discard_manual_changes，
    dispatcher 工具转 needs_confirm）；操作员确认后带 True 重入才放行。
    """
    import shutil

    # 注：settings.history_output_dir 方法由 config.py 提供（如果还没有，
    # 请在 Settings 类加一个返回 output/_history/{safe(batch_id)} 的方法）。
    config = _read_batch_config(batch_id)
    if config is None:
        raise ValueError(f"批次不存在: {batch_id}")
    run_count = config.get("run_count", 1)

    # 决策 10 确认门：副本人工改动痕迹未确认 → 拒绝（归档前先拦截，
    # 不产生任何副作用）
    if not confirm_discard_manual:
        traces = detect_manual_traces(batch_id)
        if traces:
            logger.warning(
                "[rerun] 批次 %s 副本检测到人工改动痕迹 %s，未确认拒绝重跑",
                batch_id, traces)
            raise ManualDiscardError(traces)

    settings = get_settings()
    batch_output_dir = settings.batch_output_dir(batch_id)

    # ① 归档 output（仅当上一轮产物存在）
    if batch_output_dir.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_run_dir = settings.history_output_dir(batch_id) / f"r{run_count}_{ts}"
        archive_run_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(batch_output_dir), str(archive_run_dir / "run"))
            logger.info("[rerun] 已归档 output 到 %s", archive_run_dir)
        except (OSError, shutil.Error) as e:
            logger.error("[rerun] 归档失败，拒绝重跑: %s: %s", type(e).__name__, e)
            raise RuntimeError(f"output 归档失败，已拒绝 rerun: {e}") from e

    # ② 清空 sessions 目录下文件（不删目录）
    sessions_dir = SESSIONS_DIR / settings.safe_path_tag(batch_id)
    if sessions_dir.is_dir():
        for p in sessions_dir.glob("*.json"):
            try:
                p.unlink()
            except OSError as e:
                logger.warning("[rerun] 清理 session %s 失败（跳过）: %s: %s",
                               p.name, type(e).__name__, e)
        logger.info("[rerun] 已清空 sessions 目录: %s", sessions_dir)

    # ③ 原逻辑：重置 checkpoint、run_count + 1、_write_batch_config、调 run_until_interrupt
    from langgraph.graph import START
    graph = get_graph()
    cfg = _config(batch_id)
    graph.update_state(
        cfg,
        {
            "downstream_file_path": config["downstream_file_path"],
            "upstream_root": config["upstream_root"],
            "batch_id": batch_id,
        },
        as_node=START,
    )

    config["last_run_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    config["run_count"] = run_count + 1
    _write_batch_config(batch_id, config)
    # 决策 9：新 run 以当前原件为基准重记指纹（旧指纹已随 output 归档；
    # config dict 是归档前读的，残留旧指纹键必须覆盖）
    source_diff.record_fingerprint(batch_id, config["downstream_file_path"])

    return run_until_interrupt(
        thread_id=batch_id,
        downstream_file_path=config["downstream_file_path"],
        upstream_root=config["upstream_root"],
        on_progress=on_progress,
    )


class SourceChangesError(Exception):
    """原件差异确认门信号（写入侧人工改动感知 决策 9）。

    原件在批次运行后被人工改单（已完成/重试工厂有增行/删行/数量改）且
    未确认 → 不入图/不重试，路由层转 HTTP 409 结构体
    （{"code": "source_changes", ...}）；操作员确认后带
    confirm_source_changes=True 重发才吸收。
    changes 按厂明细：[{"factory", "added", "removed", "qty_changed"}]。
    """

    def __init__(self, changes: list[dict]):
        self.changes = list(changes)
        sdiff = source_diff.SourceDiff(changes=tuple(self.changes))
        super().__init__(
            f"原件中已完成工厂有变更（{sdiff.describe()}），是否吸收重新写入")


class ManualDiscardError(Exception):
    """完全重跑警告确认门信号（写入侧人工改动感知 决策 10）。

    rerun_batch 会重建 output 副本（旧副本整体归档 _history/），副本快照
    检测到人工改动痕迹（diff 非空或 protected/tombstones 非空）且未确认
    → 拒绝执行；操作员确认后带 confirm_discard_manual=True 重发才放行。
    traces 为计数摘要 dict（detect_manual_traces 的返回）。
    """

    def __init__(self, traces: dict):
        self.traces = dict(traces)
        super().__init__(
            "完全重跑将重建 output 副本，副本上的全部人工改动将丢失"
            "（旧副本归档至 _history/ 可查回），是否继续")


class ManualConflictError(Exception):
    """补充工厂通道 B 确认门信号（写入侧人工改动感知 决策 1）。

    跑图前 diff 检测到待写入行有人工非空修改 → 不入图，路由层转 HTTP 409
    结构体（{"code": "manual_conflicts", ...}）；操作员确认后带
    confirm_overwrite=true 重发同端点才覆写。
    conflicts 逐字段明细：[{factory, sku, row, field, old, new}]，
    old=快照值（系统上次认知），new=副本人工值。
    """

    def __init__(self, conflicts: list[dict]):
        self.conflicts = list(conflicts)
        super().__init__(f"检测到 {len(self.conflicts)} 行人工修改")


def _sync_output_copy_rows(
    batch_id: str,
    downstream_path: str,
    factories: set[str],
    row_map: dict[str, dict[str, list[int]]],
    mark_manual: set[str] | None = None,
) -> dict[str, int]:
    """把装箱单中待补充工厂的行同步进 output 副本（快照驱动三分支，决策 8）。

    对 refresh 工厂的每个 row_key（(工厂, SKU, seq)，行号不消费）：
    - 副本有 → 已同步，跳过；
    - 快照有过该 row_key 或该键在 tombstones → 人工删除，**绝不复活**
      （MX1 复活事故修复核心；系统自身不删行，行消失只可能是人工删除），
      记 info 计入 skipped_manual_deleted；
    - 快照/墓碑都没有 → 原件真·新增行（尾部追加式改单）→ 追加到副本尾部
      （不按同序号行对位）：从原件按表头名整行拷贝各列值，副本多出的
      中文品名/净重/毛重三列留空待 writer 填，行格式照搬上一数据行。

    落盘后全量重扫重建快照（更新点 4，含 tombstones 结转——本次消失行键
    入碑；决策 4 阶段五：mark_manual 键集打 origin="manual_added"——
    调用方在写前 diff 检出并纳入管理的人工新增行）。输出副本不存在
    （尚无工厂写入）时无需同步。快照缺失/损坏时退化：无墓碑可查，缺失行
    一律按真新增追加（同旧行为），不落半同步状态（追加失败抛错前不落盘
    由调用方跑图前置保证）。

    Returns:
        {"synced": 追加行数, "skipped_manual_deleted": 人工删除不复活行数}
    """
    result = {"synced": 0, "skipped_manual_deleted": 0}
    if not factories:
        # 无原件行需同步时照常收工；但决策 4（阶段五）纯人工新增行场景
        # （原件未变，factories 空）也要重建快照打 origin="manual_added"，
        # 否则下次 diff 重复检出同一批 added_rows
        if mark_manual:
            copy_snapshot.refresh_snapshot(batch_id, mark_manual=mark_manual)
        return result
    settings = get_settings()
    src = Path(downstream_path)
    dst = settings.batch_containers_dir(batch_id) / f"{src.stem}_filled{src.suffix}"
    if not dst.exists():
        return result

    # 快照驱动的「人工删除」判定基准；无快照 → 全按真新增（保守退化）
    snap = copy_snapshot.load_snapshot(batch_id)
    snap_keys: set[str] = set()
    tombstones: set[str] = set()
    if snap:
        for r in snap.get("rows") or []:
            k = r.get("key") or {}
            snap_keys.add(copy_snapshot.row_key_str(
                (str(k.get("factory") or ""), str(k.get("sku") or ""),
                 int(k.get("seq") or 0))))
        tombstones = set(snap.get("tombstones") or [])

    # 决策 2（阶段四）：写前 diff 检出整厂剔除/对称恢复（审计留痕，幂等）。
    # row_map 为重解析原件全集，键即 requirements 口径；diff/检出失败
    # 返回 None/[] 时静默放行，绝不阻塞同步主流程
    _diff = copy_snapshot.diff_against_copy(batch_id)
    if _diff is not None:
        copy_snapshot._detect_factory_removals(batch_id, _diff, set(row_map))

    wb_src = load_workbook(src)
    ws_src = wb_src[wb_src.sheetnames[0]]
    wb_dst = load_workbook(dst)
    ws_dst = wb_dst[wb_dst.sheetnames[0]]

    src_header = [c.value for c in ws_src[1]]
    dst_header = [c.value for c in ws_dst[1]]
    # 按表头名映射 源列 → 副本列（列位可能因三列插入而右移，绝不按列号硬拷）
    col_pairs = [(i + 1, dst_header.index(name) + 1)
                 for i, name in enumerate(src_header) if name in dst_header]
    # 副本多出、原件没有的列（中文品名/净重/毛重三列）：值留空，格式照搬
    src_names = set(src_header)
    dst_only_cols = [j for j, name in enumerate(dst_header, start=1)
                     if name not in src_names]

    # 副本当前行标识索引（决策 3 重定位，人工插删行移位免疫）
    dst_index = copy_snapshot.build_row_index(ws_dst)

    for factory in sorted(factories):
        for sku, rows in (row_map.get(factory) or {}).items():
            key_base = (copy_snapshot.norm_key_text(factory),
                        copy_snapshot.norm_key_text(sku))
            for seq, src_row in enumerate(sorted(rows), start=1):
                key = (*key_base, seq)
                if key in dst_index:
                    continue  # 副本有 → 已同步（如同一批次重复补充）
                key_str = copy_snapshot.row_key_str(key)
                if key_str in snap_keys or key_str in tombstones:
                    # 人工删除，绝不复活（MX1 事故修复核心分支）
                    result["skipped_manual_deleted"] += 1
                    logger.info(
                        "[补充工厂] 工厂「%s」SKU「%s」第 %d 行曾被人工删除"
                        "（快照/墓碑在案），不复活", factory, sku, seq)
                    continue
                # 原件真·新增行 → 追加副本尾部（消费侧全按行遍历，行序无影响）
                new_row = ws_dst.max_row + 1
                fmt_row = new_row - 1  # 行格式照搬上一数据行
                for c_src, c_dst in col_pairs:
                    cell = ws_dst.cell(
                        row=new_row, column=c_dst,
                        value=ws_src.cell(row=src_row, column=c_src).value)
                    cell._style = copy(ws_dst.cell(row=fmt_row,
                                                   column=c_dst)._style)
                for c_dst in dst_only_cols:
                    cell = ws_dst.cell(row=new_row, column=c_dst)
                    cell._style = copy(ws_dst.cell(row=fmt_row,
                                                   column=c_dst)._style)
                result["synced"] += 1
    if result["synced"]:
        wb_dst.save(dst)
        logger.info("[补充工厂] 已向输出副本尾部追加 %d 行（%s）",
                    result["synced"], dst)
    # 快照更新点 4（决策 5）：sync 后全量重扫重建（含 tombstones 结转——
    # 即使本次零追加也要重建：人工删除的行键在此入碑；决策 4 阶段五：
    # 当次纳入管理的人工新增行键经 mark_manual 打 origin="manual_added"）
    copy_snapshot.refresh_snapshot(batch_id, mark_manual=mark_manual)
    return result


def force_complete_batch(thread_id: str) -> dict[str, Any]:
    """强制结案（2026-10-12 待补充状态）：待补充批次直接结案。

    校验：批次存在且自愈后的权威状态为 awaiting_factories，否则 ValueError
    （路由层转 400）。动作：batches.force_completed=1（持久标志，pipeline_state
    自愈不得打回）+ status 置 completed（completed_at 自动填充），并向
    review_audits 插一条 result_status="batch_force_completed" 留痕
    （factory_name=None；留痕失败只 warning 不阻塞，与 _write_audit 同哲学）。

    Returns:
        {"ok": True, "status": "completed", "message": ...}

    Raises:
        ValueError: 批次不存在 / 当前状态不是待补充
    """
    tid = (thread_id or "").strip()
    batch = batch_store.get_batch(tid)
    if batch is None:
        raise ValueError(f"批次不存在: {tid}")

    # 权威状态以 pipeline_state 自愈结果为准（batches 表滞留旧值时先校正）
    from app.orchestrator.pipeline_state import get_pipeline_state
    ps = get_pipeline_state(tid)
    current = ((ps.get("batch") or {}).get("status")) if ps.get("exists") else None
    if current != "awaiting_factories":
        raise ValueError(
            f"批次当前状态为「{current or '未知'}」，仅「待补充」状态可强制结案")

    if not batch_store.set_force_completed(tid):
        raise ValueError(f"批次 {tid} 强制结案标志写入失败，请查看服务端日志")
    batch_store.update_status(tid, "completed")

    try:
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=tid,
                factory_name=None,
                approved=False,
                edited_count=0,
                changes_json=json.dumps([{"force_complete": True}],
                                        ensure_ascii=False),
                new_skus_json="[]",
                result_status="batch_force_completed",
            ))
            session.commit()
    except Exception as e:  # noqa: BLE001 留痕失败不阻塞已完成的结案
        logger.warning("⚠️⚠️ [审计落库失败] thread=%s "
                       "批次已强制结案，但 batch_force_completed 留痕写入失败：%s: %s",
                       tid, type(e).__name__, e)

    logger.info("批次强制结案 | thread_id=%s", tid)
    return {"ok": True, "status": "completed",
            "message": "已强制结案：剩余缺资料工厂不再补齐"}


def _auto_create_factory_folders(
    upstream: Path,
    factories: list[str],
    *,
    check_folder_match: bool,
    overrides: dict[str, str] | None = None,
) -> None:
    """自动为缺资料工厂建空文件夹（2026-10-12 用户二次确认，调整 B）。

    命名与看板手动「预建工厂文件夹」完全一致（factory_setup 共享 helper
    canonical_folder_name / ensure_factory_folder）；幂等（exist_ok=True）、
    逐个 info 日志、单个失败只 warning 不阻断主流程。CLAUDE.md 写操作
    确认门不适用于此系统内部幂等动作（代码注释留档于 factory_setup）。

    check_folder_match=True（补充工厂路径）：先与现有文件夹按 Node2 同口径
    重匹配，有任何命中（含 fuzzy）都视为已有文件夹不建——只给完全未匹配
    的工厂建，避免 fuzzy 命中的工厂被新建空文件夹抢走提取源。
    False（建批路径）：调用方 prescan 已判定 unmatched，直接建。
    """
    if not factories:
        return
    if not upstream.is_dir():
        logger.warning("自动建夹跳过：上游工厂目录不存在 %s", upstream)
        return
    to_create = list(factories)
    if check_folder_match:
        from app.factory_match import (
            load_alias_map, load_folder_match_candidates, match_factory_folder)
        try:
            folders = [d.name for d in upstream.iterdir() if d.is_dir()]
        except OSError as e:
            logger.warning("自动建夹跳过：上游工厂目录不可读 %s", e)
            return
        alias_map = load_alias_map()
        folder_candidates = load_folder_match_candidates()
        cutoff = get_settings().fuzzy_match_score_cutoff
        to_create = []
        for factory in factories:
            hit, _score, _method = match_factory_folder(
                factory, folders, alias_map, cutoff=cutoff,
                overrides=overrides, folder_candidates=folder_candidates)
            if hit:
                continue  # 已有匹配文件夹（含 fuzzy），不建
            to_create.append(factory)
    for factory in to_create:
        factory_setup.ensure_factory_folder(upstream, factory)


def _manually_removed_factories(thread_id: str) -> set[str]:
    """人工剔除名单（写入侧人工改动感知 决策 2/阶段四）：review_audits
    每厂最新一条审计口径（遍历抄 pipeline_state._classify_incomplete_batch），
    最新一条 result_status == "factory_manually_removed" 即在名单中；
    对称恢复的 factory_manually_readded 成为最新一条即自动出名单。
    零新表零新列，与批次详情页 role 推导同口径。

    辅助设施口径：查询失败只 warning 返回空集（按无剔除处理，缺口判定
    退回旧行为），绝不阻塞主流程。
    """
    try:
        with get_session() as session:
            rows = session.scalars(
                select(ReviewAudit)
                .where(ReviewAudit.thread_id == thread_id)
                .order_by(ReviewAudit.audit_id)
            ).all()
        latest: dict[str, Any] = {}
        for r in rows:  # audit_id 升序遍历，后者覆盖前者 = 每厂最新一条
            if r.factory_name:
                latest[r.factory_name] = r
        return {name for name, r in latest.items()
                if r.result_status == "factory_manually_removed"}
    except Exception as e:  # noqa: BLE001 辅助设施失败静默降级
        logger.warning("批次 %s 人工剔除名单查询失败（按无剔除处理）: %s: %s",
                       thread_id, type(e).__name__, e)
        return set()


def _removed_factories_hint(newly_removed: list[str]) -> str:
    """剔除透出文案（决策 2/阶段四）：「工厂 X 已被人工剔除，本批次不再
    等待它」；多厂用「它们」。"""
    names = "、".join(newly_removed)
    pronoun = "它" if len(newly_removed) == 1 else "它们"
    return f"工厂 {names} 已被人工剔除，本批次不再等待{pronoun}"


def _manual_rows_incomplete_hint(batch_id: str,
                                 source_path: str | None) -> tuple[int, str]:
    """C 态透出（决策 4/阶段五）：活扫副本缺关键列（管理番号/工厂/SKU）且
    未纳入管理的人工新增行，返回 (行数, 完成摘要提示文案)；无 → (0, "")。
    扫描失败包死返回 (0, "")，绝不阻塞完成主流程。"""
    rows = copy_snapshot.scan_incomplete_rows(batch_id, source_path)
    if not rows:
        return 0, ""
    return len(rows), (f"{len(rows)} 行人工新增的行缺少关键列"
                       f"（管理番号/工厂/SKU），未纳入批次管理，请补全")


def _gap_factories(final_values: dict) -> list[str]:
    """图跑完后的缺口工厂名单（口径与 pipeline_state._extract_phase 一致）。

    缺口 = 装箱单工厂全集（有 factory_filter 则取交集）− factory_outputs.keys()
    （只有 Approved 写入的工厂才进快照）。仅图跑完（next 为空）时有意义——
    运行中/挂起中差集只是「还没轮到」。返回排序后的名单，可为空。

    决策 2（阶段四）：人工剔除厂减除出缺口——完成文案不再把它算进
    「仍有 N 家待补充」。
    """
    requirements = final_values.get("downstream_requirements") or {}
    total = set(requirements)
    factory_filter = final_values.get("factory_filter")
    if factory_filter:
        total &= set(factory_filter)
    gaps = total - set((final_values.get("factory_outputs") or {}).keys())
    batch_id = final_values.get("batch_id")
    if batch_id:
        gaps -= _manually_removed_factories(batch_id)
    return sorted(gaps)


def _merge_factories_into_state(
    values: dict,
    req_updates: dict[str, list],
    row_map_updates: dict[str, dict],
    pending_add=(),
) -> dict[str, Any]:
    """工厂需求 state 合并公共段（从 add_factories_to_batch 提取，两处复用：
    补充工厂重解析覆盖 + 决策 4 阶段五人工新增行 A/B 态并入；Node6 writer
    节点返回值合并也走这里）。

    - req_updates / row_map_updates：{工厂: 全量替换值}——调用方负责语义
      （重解析覆盖 or 追加合成后的完整列表），此处只做整体替换；
    - pending_add：追加进 pending_factories 的工厂（去重，保序）；
    - 批次带 factory_filter 时同步扩充（否则新工厂不进进度统计口径）。

    返回可直接喂 graph.update_state / 节点返回值的 update dict。
    """
    merged_requirements = dict(values.get("downstream_requirements") or {})
    merged_row_map = dict(values.get("downstream_row_map") or {})
    for f, skus in (req_updates or {}).items():
        merged_requirements[f] = skus
    for f, rm in (row_map_updates or {}).items():
        merged_row_map[f] = rm
    merged_pending = list(values.get("pending_factories") or [])
    for f in pending_add or ():
        if f not in merged_pending:
            merged_pending.append(f)
    update: dict[str, Any] = {
        "downstream_requirements": merged_requirements,
        "downstream_row_map": merged_row_map,
        "pending_factories": merged_pending,
    }
    factory_filter = values.get("factory_filter")
    if factory_filter:
        update["factory_filter"] = list(factory_filter) + [
            f for f in (pending_add or ()) if f not in set(factory_filter)]
    return update


def _manual_write_traces(
    values: dict, factories,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """从 factory_outputs 快照取当次写入的人工改动留痕（决策 1 通道分流 +
    决策 4 人工新增行）。

    返回 (kept, overwritten, qty_changes, kept_rows)——均已附审计 source：
    - kept：通道 C 保留的人工修改（source="manual_override_kept"，
      old=快照值/系统认知，new=被保留的副本人工值）；
    - overwritten：通道 A 覆盖的人工旧值（source="manual_value_overwritten"，
      old=被盖掉的人工值，new=系统写入值）；
    - qty_changes：数量列人工改动（source="manual_qty_changed"，告警不阻断）；
    - kept_rows：A 态人工新增行无提取结果支撑（source=
      "manual_row_kept_no_extract"），保留人工行未写入。
    factories 为 None 时统计全部工厂。
    """
    kept: list[dict] = []
    overwritten: list[dict] = []
    qty_changes: list[dict] = []
    kept_rows: list[dict] = []
    outputs = (values or {}).get("factory_outputs") or {}
    names = factories if factories is not None else list(outputs)
    for name in names:
        fo = outputs.get(name)
        if not isinstance(fo, dict):
            continue
        for c in fo.get("manual_conflicts") or []:
            kept.append({**c, "source": "manual_override_kept"})
        for c in fo.get("manual_overwritten_cells") or []:
            overwritten.append({**c, "source": "manual_value_overwritten"})
        for c in fo.get("qty_manual_changes") or []:
            qty_changes.append({**c, "source": "manual_qty_changed"})
        for c in fo.get("manual_rows_kept_no_extract") or []:
            kept_rows.append({**c, "source": "manual_row_kept_no_extract"})
    return kept, overwritten, qty_changes, kept_rows


def _diff_factory_source_changes(
    batch_id: str,
    values: dict,
    factories,
) -> list[dict]:
    """决策 9 共用：对指定工厂做原件行级 diff（指纹变了才解析，零成本短路）。

    返回 SourceDiff.changes 的 list（[{"factory","added","removed",
    "qty_changed"}]）；指纹未变/无差异/无法判定 → []。辅助设施包死：
    任何失败只记 error 并按无差异放行，绝不阻塞主流程。
    """
    if not factories:
        return []
    try:
        if not source_diff.source_fingerprint_changed(batch_id):
            return []
        src_path = (values.get("downstream_file_path")
                    or (_read_batch_config(batch_id) or {}).get(
                        "downstream_file_path"))
        if not src_path:
            return []
        new_rows = source_diff.parse_source_rows(src_path)
        fp = source_diff.read_fingerprint(batch_id) or {}
        sdiff = source_diff.diff_source_requirements(
            values.get("downstream_row_map") or {},
            new_rows,
            old_qty_map=fp.get("qty_map") or {},
            factories=factories,
        )
        return list(sdiff.changes)
    except Exception:  # noqa: BLE001 辅助设施失败静默降级
        logger.error("[原件diff] 批次 %s 原件差异检测失败（按无差异放行）",
                     batch_id, exc_info=True)
        return []


def _source_changes_warning(
    thread_id: str,
    factory_name: str,
    values: dict,
) -> dict | None:
    """reopen 警告条数据（决策 9 入口三）：指纹变了且该厂有结构差异时给出，
    不阻断编辑。无差异/检测失败 → None（包死，绝不影响 reopen 主流程）。
    """
    try:
        changes = _diff_factory_source_changes(thread_id, values, [factory_name])
        if not changes:
            return None
        desc = source_diff.SourceDiff(changes=tuple(changes)).describe()
        return {
            "message": f"原件已在批次运行后被修改（{desc}），请核对后再提交",
            "changes": changes,
        }
    except Exception:  # noqa: BLE001
        logger.warning("[原件diff] reopen 警告检测失败（不阻断）"
                       "thread=%s factory=%s", thread_id, factory_name,
                       exc_info=True)
        return None


def factory_source_changes_summary(
    thread_id: str,
    factory_name: str,
) -> str | None:
    """dispatcher 写工具 preview 用的单厂原件差异一句化警告（决策 9）。

    返回 None = 无差异/批次不存在/检测失败（preview 静默略过）。
    """
    try:
        graph = get_graph()
        values = graph.get_state(_config(thread_id)).values or {}
        warning = _source_changes_warning(thread_id, factory_name, values)
        return warning["message"] if warning else None
    except Exception:  # noqa: BLE001 preview 辅助，绝不抛出
        logger.warning("[原件diff] 单厂差异摘要失败 thread=%s factory=%s",
                       thread_id, factory_name, exc_info=True)
        return None


def detect_manual_traces(batch_id: str) -> dict | None:
    """决策 10：副本快照中的人工改动痕迹计数（完全重跑警告的数据源）。

    痕迹口径（任一非零即需确认）：写前 diff 非空（改值/数量/删行/新增）
    或快照 protected/tombstones 非空。返回计数 dict；无痕迹/无快照/无副本/
    检测失败 → None（放行重跑；旧副本归档 _history/ 本身已是兜底）。
    """
    try:
        snap = copy_snapshot.load_snapshot(batch_id)
        if snap is None:
            return None
        protected = sum(len(r.get("protected_fields") or [])
                        for r in snap.get("rows") or [])
        tombstones = len(snap.get("tombstones") or [])
        diff = copy_snapshot.diff_against_copy(batch_id)
        traces = {
            "value_conflicts": len(diff.value_conflicts) if diff else 0,
            "qty_changes": len(diff.qty_changes) if diff else 0,
            "deleted_rows": len(diff.deleted_rows) if diff else 0,
            "added_rows": len(diff.added_rows) if diff else 0,
            "protected_cells": protected,
            "tombstones": tombstones,
        }
        return traces if any(traces.values()) else None
    except Exception:  # noqa: BLE001 辅助设施失败静默降级（放行重跑）
        logger.error("[rerun] 批次 %s 人工改动痕迹检测失败（按无痕迹放行）",
                     batch_id, exc_info=True)
        return None


def _absorb_source_changes(
    batch_id: str,
    values: dict,
    factories: list[str],
    new_rows: dict,
    new_row_map: dict,
) -> dict:
    """决策 9 吸收：已完成工厂原件变更确认后的副本重写（决策 1 通道 A 语义）。

    每厂三步：
    1. 数量列回写：按 row_key 索引把原件新数量写进副本对应行
       （人工插删行移位免疫；新增行已被 _sync_output_copy_rows 追加）；
    2. 快照重建（让 writer 写前 diff 以新数量为基准，避免把系统自己
       回写的数量误判为「人工改数量」告警）；
    3. writer._write_excel(channel="rework") 用 factory_outputs 的
       calculated_items 重写三列（按新数量重算；被盖人工旧值逐单元格
       留痕 manual_value_overwritten），审计 result_status="source_absorbed"。

    原件整厂消失/副本不存在/单厂重写失败 → 记日志跳过该厂，绝不阻塞主流程
    （吸收是确认后的辅助动作，失败可由 reopen/单厂重试兜底）。
    """
    from app.nodes import writer as writer_mod  # 延迟导入避免循环

    absorbed = {"factories": [], "qty_cells_updated": 0}
    copy_path = copy_snapshot.find_copy(batch_id)
    if copy_path is None:
        logger.warning("[原件吸收] 批次 %s 无输出副本，跳过副本重写", batch_id)
        return absorbed
    settings = get_settings()
    outputs = values.get("factory_outputs") or {}

    # ① 数量列回写（原件 → 副本）
    try:
        wb = load_workbook(copy_path)
        ws = wb[wb.sheetnames[0]]
        header = [c.value for c in ws[1]]
        col_qty = (header.index(settings.col_qty) + 1
                   if settings.col_qty in header else None)
        if col_qty is not None:
            row_index = copy_snapshot.build_row_index(ws)
            dirty = False
            for factory in factories:
                for sku, qtys in (new_rows.get(factory) or {}).items():
                    key_base = (copy_snapshot.norm_key_text(factory),
                                copy_snapshot.norm_key_text(sku))
                    for seq, new_qty in enumerate(qtys, start=1):
                        row_no = row_index.get((*key_base, seq))
                        if row_no is None:
                            continue  # 原件删行/未同步行：副本侧不动
                        cell = ws.cell(row=row_no, column=col_qty)
                        cur_qty = copy_snapshot.round2(cell.value)
                        if not source_diff._qty_equal(cur_qty, new_qty):
                            cell.value = new_qty
                            absorbed["qty_cells_updated"] += 1
                            dirty = True
                            logger.info(
                                "[原件吸收] 批次 %s 行 %d（%s/%s）数量按原件回写"
                                "（%s→%s）", batch_id, row_no, factory, sku,
                                cur_qty, new_qty)
            if dirty:
                wb.save(copy_path)
        wb.close()
    except Exception:  # noqa: BLE001 单步失败不阻塞后续重写
        logger.error("[原件吸收] 批次 %s 数量列回写失败", batch_id,
                     exc_info=True)

    # ② 快照重建：以新数量为写前 diff 基准（防误报「人工改数量」）
    copy_snapshot.refresh_snapshot(batch_id)

    # ③ 逐厂通道 A 重写三列 + 审计留痕
    for factory in factories:
        try:
            raw = outputs.get(factory)
            items = (raw.get("calculated_items")
                     if isinstance(raw, dict) else raw) or []
            if not items:
                logger.warning("[原件吸收] 批次 %s 工厂「%s」无已审核数据快照，"
                               "跳过三列重写（可 reopen/单厂重试补齐）",
                               batch_id, factory)
                continue
            fake_state: dict[str, Any] = {
                "batch_id": batch_id,
                "downstream_file_path": values.get("downstream_file_path"),
                "downstream_row_map": {factory: new_row_map.get(factory) or {}},
                "validation_status": "Approved",
                "current_factory_data": {
                    "factory_name": factory,
                    "calculated_items": items,
                },
            }
            writer_mod._write_excel(fake_state, copy_path, channel="rework")
            overwritten = list(
                fake_state["current_factory_data"].get(
                    "manual_overwritten_cells") or [])
            absorbed["factories"].append(factory)
            _write_audit({
                "thread_id": batch_id,
                "factory_name": factory,
                "approved": True,
                "edited_count": len(overwritten),
                "changes": ([{"factory_absorbed": True,
                              "note": "原件变更确认吸收，按通道 A 语义重写副本"}]
                            + [{**c, "source": "manual_value_overwritten"}
                               for c in overwritten]),
                "new_skus": [],
            }, "source_absorbed")
        except Exception:  # noqa: BLE001 单厂失败不拖累其余工厂
            logger.error("[原件吸收] 批次 %s 工厂「%s」重写失败",
                         batch_id, factory, exc_info=True)

    logger.info("[原件吸收] 批次 %s 吸收完成：%s（数量回写 %d 格）",
                batch_id, absorbed["factories"], absorbed["qty_cells_updated"])
    return absorbed


def add_factories_to_batch(
    batch_id: str,
    on_progress: Callable[[dict], None] | None = None,
    confirm_overwrite: bool = False,
    confirm_source_changes: bool = False,
) -> dict[str, Any]:
    """补充工厂：重新解析装箱单，把「未写入」的工厂补进本批次继续处理。

    待补充工厂 = 装箱单工厂全集 − factory_outputs.keys()（已 approve 写入的）。
    装箱单新增的、被「跳过本工厂」的、被驳回的工厂都能补进来；已写入的绝不重复。

    决策 9 入口一（原件差异检测，先于通道 B）：利用本函数既有全文件重解析，
    对已完成工厂（factory_outputs）与 state 比对——有增行/删行/数量改且
    confirm_source_changes=False → 抛 SourceChangesError（路由转 409
    code=source_changes）；确认后吸收：state 合并该厂最新 requirements/row_map
    + 副本重写（sync 追加新行 + 数量列回写 + writer 通道 A 语义重写三列）
    + 审计 source_absorbed + 指纹基线更新。
    门槛顺序契约（前端）：一次 409 只报一种 code，先 source_changes 后
    manual_conflicts。

    通道 B 确认门（写入侧人工改动感知 决策 1）：跑图前对 output 副本做
    写前 diff，待写入工厂的行存在人工非空修改且 confirm_overwrite=False
    → 不入图，抛 ManualConflictError（路由层转 HTTP 409 结构体，前端弹
    确认窗）；操作员确认后带 confirm_overwrite=True 重发 → 冲突工厂写入
    state.overwrite_manual_factories，Node6 按通道 A（rework）语义覆写，
    被盖旧值留痕 manual_value_overwritten。

    续跑语义（langgraph 1.2.9 实测）：
    - completed（next 为空）：update_state(as_node=NODE6) 让 Node6 条件边
      _route_after_writer 重新路由（pending 非空 → NODE2），stream(None) 续跑
      到下一个 Node5 挂起；Node6 本身不重跑，已写入工厂不会重复写；
    - pending_review（挂在 Node5 interrupt）：update_state 无论 as_node 是什么
      都会销毁 interrupt 任务，且销毁后直接 resume 会让当前工厂静默跳过写入
      （validation_status 残留 Pending）。因此用 as_node=NODE4 重定向
      （条件边重新路由回 NODE5）+ stream(None) 立即重挂起——Node5 用未变的
      current_factory_data 重建等价 payload 再次 interrupt，审核页刷新后照常
      审核，当前工厂不丢；
    - running（next 非空且无 interrupt）：RuntimeError（路由层转 409）稍后再试。
    - aborted（2026-10-14 复活，协作停止在 Node2/Node7 入口：节点未执行、
      snap.next 指向该节点、无 interrupt）：放行进入主流程，正式续跑前
      update_status(tid,"running") 解除 pipeline_state 自愈守卫 +
      防御性再清 cancel_requested；state 无需合并（无新工厂可补且图还有
      未跑节点）时跳过 update_state 直接 stream(None) 原样恢复挂起任务，
      否则同 completed 走 as_node=NODE6 锚点重路由。跑完/挂起后状态由
      _write_batch_state 与自愈驱动（pending_review→人审；全补齐→completed；
      仍缺→awaiting_factories，factory_watch 监控随之恢复）；期间用户可再次
      终止（状态已回 running，abort 走协作检查点，最终落回 aborted）。

    Returns:
        {"added": int, "factories": [...], "status": ..., "message": ...?}

    Raises:
        ValueError: 批次不存在 / 装箱单解析失败
        RuntimeError: 批次正在运行中
        ManualConflictError: 待写入行检测到人工非空修改且未确认（通道 B
            确认门；路由层转 HTTP 409 结构体，确认后 confirm_overwrite=True
            重发）
    """
    from app.nodes.parse_downstream import parse_downstream_file

    config = _read_batch_config(batch_id)
    if config is None:
        raise ValueError(f"批次不存在: {batch_id}")

    # 重新解析装箱单（用户可能已编辑：新增工厂/调整行）
    try:
        new_requirements, new_row_map = parse_downstream_file(
            config["downstream_file_path"])
    except Exception as e:  # noqa: BLE001 解析失败统一转 ValueError（路由 400）
        raise ValueError(
            f"装箱单解析失败，无法补充工厂: {type(e).__name__}: {e}") from e

    # 读取当前 checkpoint state
    graph = get_graph()
    cfg = _config(batch_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"批次 {batch_id} 无 checkpoint state")

    values = snap.values
    has_interrupt = any(t.interrupts for t in snap.tasks)
    # 2026-10-14 aborted 复活：已终止批次 checkpoint 残留 next 非空属预期
    # （协作停止点在 Node2/Node7 入口，节点未执行、无 __interrupt__），
    # 不按「运行中」409 挡下；其余 next 非空形态照旧视为运行中
    batch_row = batch_store.get_batch(batch_id)
    was_aborted = bool(batch_row) and batch_row.get("status") == "aborted"
    if snap.next and not has_interrupt and not was_aborted:
        raise RuntimeError(
            f"批次 {batch_id} 正在运行中，请待其挂起审核或完成后再补充工厂")

    # 待补充 = 装箱单全集 − 已写入（factory_outputs 只有 Approved 工厂才进）
    done = set((values.get("factory_outputs") or {}).keys())
    # 决策 2（阶段四）：人工剔除厂不自动补回（名单=每厂最新一条审计），
    # 同时作为「剔除发生时」完成文案的对比基线
    removed_before = _manually_removed_factories(batch_id)
    for f in sorted(set(new_requirements) & removed_before):
        logger.info("[补充工厂] 工厂「%s」已被人工剔除，不再补回", f)
    to_add = [f for f in new_requirements
              if f not in done and f not in removed_before]

    # pending 去重：已在主队列 / 暂缓队列的不重复加；挂起中的当前工厂
    # （仅 pending_review 时有意义——completed 的 current 是上一轮的残留，
    # 若它当时被跳过则仍属待补充）不重复加
    existing_pending = list(values.get("pending_factories") or [])
    queued = set(existing_pending)
    deferred_names = {d.get("factory_name")
                      for d in (values.get("deferred_factories") or [])}
    current_name = ((values.get("current_factory_data") or {}).get("factory_name")
                    if has_interrupt else None)
    added = [f for f in to_add
             if f not in queued and f not in deferred_names and f != current_name]

    # 决策 9 入口一（原件差异检测，门槛顺序契约：先 source_changes 后
    # manual_conflicts）：已完成工厂的原件行级 diff（增行/删行/数量改）。
    # 指纹没变 → 零成本跳过解析；检测失败 → 按无差异放行（辅助设施包死）。
    absorbed_factories: list[str] = []
    new_source_rows: dict | None = None
    if done:
        try:
            if source_diff.source_fingerprint_changed(batch_id):
                new_source_rows = source_diff.parse_source_rows(
                    config["downstream_file_path"])
                fp = source_diff.read_fingerprint(batch_id) or {}
                sdiff = source_diff.diff_source_requirements(
                    values.get("downstream_row_map") or {},
                    new_source_rows,
                    old_qty_map=fp.get("qty_map") or {},
                    factories=done)
                if sdiff.any():
                    if not confirm_source_changes:
                        logger.warning(
                            "[补充工厂] 批次 %s 原件中已完成工厂有变更（%s），"
                            "未确认不入图", batch_id, sdiff.describe())
                        raise SourceChangesError(list(sdiff.changes))
                    absorbed_factories = [c["factory"] for c in sdiff.changes]
                    logger.info(
                        "[补充工厂] 批次 %s 原件变更已确认吸收：%s",
                        batch_id, sdiff.describe())
        except SourceChangesError:
            raise
        except Exception:  # noqa: BLE001 原件 diff 是辅助设施，不阻塞补充主流程
            logger.error("[原件diff] 批次 %s 入口一检测失败（按无差异放行）",
                         batch_id, exc_info=True)
            absorbed_factories = []

    # 决策 4（阶段五）：人工新增行预检出——副本存在快照没有的 row_key。
    # A 态（厂已在批次需求）并入该厂需求；B 态（全新厂）视同待补充入队；
    # C 态（缺管理番号）不纳入只提示（缺工厂/SKU 的行进不了快照 diff，
    # 由完成摘要/批次详情 scan_incomplete_rows 活扫透出）。命中墓碑的键
    # 随 _sync/写入后的 refresh 结转自动出碑再走三态。
    # 检出必须在「没有待补充的工厂」早退之前：仅人工新增行（原件未变）
    # 的场景也要能入批。diff 同时复用于下方通道 B 确认门，避免二次现扫。
    manual_classified: dict[str, list] = {"a": [], "b": [], "c": []}
    manual_keys: set[str] = set()
    pre_diff = copy_snapshot.diff_against_copy(batch_id)
    if pre_diff is not None and pre_diff.added_rows:
        manual_classified = copy_snapshot.classify_added_rows(
            pre_diff.added_rows,
            set(values.get("downstream_requirements") or {})
            | set(values.get("downstream_row_map") or {}))
        for c in manual_classified["c"]:
            logger.warning(
                "[补充工厂] 副本第 %s 行（%s/%s）缺管理番号（KANRI_NO），"
                "未纳入批次管理，请补全",
                c.get("row_no"), c.get("factory"), c.get("sku"))
        # B 态全新厂视同待补充入队（去重口径与原件待补一致）
        manual_b = sorted({str(r.get("factory") or "")
                           for r in manual_classified["b"]})
        manual_b = [f for f in manual_b
                    if f and f not in queued and f not in deferred_names
                    and f != current_name]
        added = added + manual_b
    manual_ab = bool(manual_classified["a"] or manual_classified["b"])

    if (not added and not absorbed_factories and not manual_ab
            and not (was_aborted and snap.next)):
        # 无新工厂且无待吸收变更：顺手把指纹基线对齐当前原件
        # （mtime 抖动/仅非业务列改动场景下次零成本短路）
        source_diff.record_fingerprint(batch_id, config["downstream_file_path"])
        no_add_msg = "没有待补充的工厂"
        if manual_classified["c"]:
            no_add_msg += (f"；{len(manual_classified['c'])} 行人工新增的行"
                           f"缺管理番号（KANRI_NO），未纳入批次管理，请补全")
        return {"added": 0, "factories": [], "message": no_add_msg}
    # aborted 复活且图还有未跑节点（停在 Node2 入口时主/暂缓队列未消，
    # 或停在 Node7 入口）：即使无新工厂可补，也照走续跑把剩余队列跑下去

    # 2026-10-12 用户确认（调整 B）：新增入队的未匹配工厂一并自动建夹，
    # 操作员按标准名放单据后监控即可接手；幂等，失败只 warning 不阻断
    try:
        _auto_create_factory_folders(
            Path(config["upstream_root"]).expanduser(), added,
            check_folder_match=True,
            overrides=values.get("factory_alias_overrides") or None)
    except Exception:  # noqa: BLE001 自动建夹是辅助设施，绝不阻断补充
        logger.warning("[补充工厂] 自动建夹异常（不阻断）", exc_info=True)

    # 对待补充工厂用新解析值覆盖 requirements/row_map（装箱单可能改过）；
    # 已写入工厂与挂起中的当前工厂保持原值不动
    refresh = set(to_add) - {current_name}

    # 通道 B 确认门（决策 1）：跑图前 diff 副本，待写入工厂的行存在人工
    # 非空修改 → 未确认不入图（抛 ManualConflictError，路由转 409 结构体）；
    # 确认后覆写（writer 按 overwrite_manual_factories 走通道 A，旧值留痕）。
    # diff/快照失败返回 None 时保守放行，绝不阻塞补充主流程。
    manual_conflicts: list[dict] = []
    diff = pre_diff  # 复用决策 4 预检出的写前 diff（同一份现扫，零二次开销）
    if diff is not None:
        for c in diff.value_conflicts:
            if c["factory"] in refresh:
                manual_conflicts.append({
                    "factory": c["factory"], "sku": c["sku"],
                    "row": c["row_no"], "field": c["field"],
                    "old": c["snapshot"], "new": c["copy"],
                })
        for q in diff.qty_changes:
            if q["factory"] in refresh:
                logger.warning(
                    "[补充工厂] 行 %s（%s/%s）数量被人工改过（%s→%s），"
                    "写入时将按副本新数量计算", q["row_no"], q["factory"],
                    q["sku"], q["snapshot_qty"], q["copy_qty"])
    if manual_conflicts and not confirm_overwrite:
        logger.warning("[补充工厂] 批次 %s 检测到 %d 处人工修改，未确认不入图",
                       batch_id, len(manual_conflicts))
        raise ManualConflictError(manual_conflicts)

    # state 合并公共段（决策 4 阶段五提取，与 Node6 writer 节点返回值
    # 合并共用 _merge_factories_into_state）：待补充工厂用新解析值覆盖
    # （装箱单可能改过）；决策 9 吸收工厂一并以重解析值覆盖 state 基准
    # （原件整厂消失的工厂不在 new_requirements → state 保留原样，
    #  副本行不删，由人工 reopen/剔除流程处理）；已写入工厂与挂起中的
    # 当前工厂保持原值不动；added 入 pending / factory_filter 扩充
    refresh_names = [f for f in sorted(refresh | set(absorbed_factories))
                     if f in new_requirements]
    update = _merge_factories_into_state(
        values,
        {f: new_requirements[f] for f in refresh_names},
        {f: new_row_map[f] for f in refresh_names},
        pending_add=added)
    if manual_conflicts and confirm_overwrite:
        # 通道 B 确认覆写：Node6 writer 对名单内工厂按通道 A（rework）语义
        # 覆盖人工修改（旧值留痕 manual_value_overwritten），消费后自清
        update["overwrite_manual_factories"] = sorted(
            {c["factory"] for c in manual_conflicts})
    # 决策 4（阶段五）：人工新增行并入叠加在重解析覆盖之上——A 态向
    # requirements 追加 SKU + row_map 挂等长占位列表；B 态新厂需求明细
    # 全部来自副本新增行（SKU 按行号序去重）。幂等：快照已纳入的行
    # 不再出现在 diff.added_rows，plan 对无变化工厂不吐 update。
    if manual_ab:
        plan = copy_snapshot.plan_manual_merge(
            manual_classified["a"], manual_classified["b"],
            update["downstream_requirements"], update["downstream_row_map"])
        for f, skus in plan["req_updates"].items():
            update["downstream_requirements"][f] = skus
        for f, rm in plan["row_map_updates"].items():
            update["downstream_row_map"][f] = rm
        manual_keys = plan["manual_keys"]
        if plan["b_factories"]:
            logger.info("[补充工厂] 检出人工新增全新工厂 %s 的副本行，"
                        "并入本批次待处理队列", plan["b_factories"])

    logger.info("[补充工厂] 批次 %s：补入 %d 家 %s（%s）",
                batch_id, len(added), sorted(added),
                "挂起中重挂起" if has_interrupt else
                ("aborted 复活续跑" if was_aborted else "已完成续跑"))

    if was_aborted:
        # 2026-10-14 aborted 复活：解除自愈守卫（pipeline_state 对 aborted
        # 一律不覆写），后续状态由跑图与自愈驱动；
        # cancel_requested 在 _mark_batch_aborted 已清，防御性再清一次
        batch_store.update_status(batch_id, "running")
        batch_store.clear_cancel_requested(batch_id)

    with logging_context(thread_id=batch_id):
        # 输出副本行同步：装箱单尾部追加的新行需先补进 output 副本，
        # 否则 writer 按新 row_map 行号写回会写出幽灵数据；
        # 决策 9 吸收工厂一并入同步范围（原件新增行追加副本尾部）
        _sync_output_copy_rows(batch_id, config["downstream_file_path"],
                               refresh | set(absorbed_factories), new_row_map,
                               mark_manual=manual_keys or None)

        # 决策 9 吸收副本重写（通道 A 语义）：数量列回写 + writer 重写三列
        # （被盖人工旧值留痕 manual_value_overwritten，审计 source_absorbed）
        if absorbed_factories:
            _absorb_source_changes(
                batch_id, values, absorbed_factories,
                new_source_rows if new_source_rows is not None
                else source_diff.parse_source_rows(config["downstream_file_path"]),
                new_row_map)

        # 决策 2（阶段四）：本次调用期间新检出的整厂剔除（sync/absorb 写前
        # diff 检出），完成/挂起文案透出「不再等待」
        newly_removed = sorted(
            _manually_removed_factories(batch_id) - removed_before)

        # as_node 锚点：pending_review 走 Node4 条件边重回 Node5 重挂起；
        # completed 走 Node6 条件边重路由（见 docstring 实测结论）；
        # aborted 无 interrupt 同走 Node6 锚点（next 残留指向 Node2/Node7
        # 入口，update_state 重算 next 后由条件边路由回 Node2 续跑）；
        # aborted 且无新工厂可补时 state 无需合并，跳过 update_state 直接
        # stream(None) 原样恢复 checkpoint 上的挂起任务（实测：Node2 只跑一次）；
        # completed 纯吸收（决策 9，无新工厂）锚 NODE7 终点——只合并 state，
        # next 重算为空，stream(None) 空转直接落完成分支，不触发导出重跑
        if added or absorbed_factories or not was_aborted:
            if has_interrupt:
                anchor = NODE4
            elif added or was_aborted:
                anchor = NODE6
            else:
                anchor = NODE7
            graph.update_state(cfg, update, as_node=anchor)

        # 决策 9：state 已反映当前原件（补充合并 + 确认吸收），更新指纹基线
        # （rerun/换路径/建批外的唯一常规更新点；未确认吸收时抛错到不了这里，
        #  指纹保持旧值，下次调用仍能检出）
        source_diff.record_fingerprint(batch_id, config["downstream_file_path"])

        emit = _make_progress_emitter(on_progress, seed=values)
        try:
            for event in graph.stream(None, cfg, stream_mode="updates"):
                emit(event)
                if "__interrupt__" in event:
                    # 挂起后继续后台预提取剩余工厂（幂等，同 resume_order）
                    _start_pre_extraction(batch_id)
                    current = graph.get_state(cfg)
                    _write_batch_state(batch_id, "pending_review", state=current.values)
                    result = {
                        "added": len(added),
                        "factories": sorted(added),
                        "status": "pending_human_review",
                        "thread_id": batch_id,
                        "review_data": event["__interrupt__"][0].value,
                        # 本次写入尚未发生（Node6 在审核提交后跑），恒为 0；
                        # 字段恒在是前端契约（manual_overrides_kept: N）
                        "manual_overrides_kept": 0,
                    }
                    if not added:
                        # aborted 复活无新工厂可补：给前端一句人话而非「已补充 0 家」
                        result["message"] = "批次已恢复执行，挂起等待审核"
                    if absorbed_factories:
                        result["source_changes_absorbed"] = sorted(absorbed_factories)
                    if newly_removed:
                        # 决策 2（阶段四）：剔除发生透出（本批次不再等待它）
                        result["manually_removed_factories"] = newly_removed
                        base = result.get("message") or "批次已恢复执行"
                        result["message"] = (
                            f"{base}；{_removed_factories_hint(newly_removed)}")
                    return result
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(batch_id)
            return {"status": "aborted", "thread_id": batch_id,
                    "message": "批次已终止，可稍后通过补充工厂恢复执行"}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(batch_id, "error", error=str(e))
            # 与 run_until_interrupt 同一不变量：批次因异常终止必须落原因
            _mark_batch_error(batch_id, e)
            raise

        # 防御性兜底：有待补充工厂正常必在 Node5 挂起；一路跑完则按完成处理
        final = graph.get_state(cfg)
        _write_batch_state(batch_id, "completed", state=final.values)
        gaps = _gap_factories(final.values)
        result = {
            "added": len(added),
            "factories": sorted(added),
            "status": "completed",
            "thread_id": batch_id,
            "awaiting_factories": gaps,
            "final_output_path": final.values.get("final_output_path"),
        }
        # 人工改动透出（决策 1/2.7）：通道 C 保留 / 通道 B 确认覆写计数摘要
        # （manual_overrides_kept 恒在是前端契约）；数量列人工改动逐行透出；
        # 决策 4（阶段五）：A 态无提取结果支撑的人工新增行保留计数透出
        kept, overwritten, qty_changes, kept_rows = _manual_write_traces(
            final.values, added)
        result["manual_overrides_kept"] = len(kept)
        if overwritten:
            result["manual_values_overwritten"] = len(overwritten)
        if qty_changes:
            result["qty_manual_changes"] = len(qty_changes)
        if kept_rows:
            result["manual_rows_kept_no_extract"] = len(kept_rows)
        if not added:
            result["message"] = "批次已恢复执行并处理完成"
        if absorbed_factories:
            # 决策 9 吸收透出：已完成工厂的原件变更已吸收重写
            result["source_changes_absorbed"] = sorted(absorbed_factories)
            absorb_hint = (f"已吸收原件变更并重写副本："
                           f"{len(absorbed_factories)} 家已完成工厂")
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = f"{base}；{absorb_hint}"
        if kept:
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = (
                f"{base}；{len(kept)} 处人工修改的单元格已保留未覆盖，"
                f"详见批次详情审计")
        for q in qty_changes:
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = (
                f"{base}；行 {q.get('row')} 数量被人工改过"
                f"（{q.get('old')}→{q.get('new')}），已按新数量写入")
        if newly_removed:
            # 决策 2（阶段四）：剔除发生透出（本批次不再等待它）
            result["manually_removed_factories"] = newly_removed
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = (
                f"{base}；{_removed_factories_hint(newly_removed)}")
        if gaps:
            # 缺口未补齐（其余工厂仍待补充）：完成文案必须带出来，
            # 否则审核页/调度会误报「本批次全部完成」
            hint = (f"本批次仍有 {len(gaps)} 家工厂待补充，"
                    f"单据到齐后可「补充工厂」继续")
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = f"{base}；{hint}"
        # 决策 4（阶段五）C 态透出：缺关键列的人工新增行未纳入，完成摘要提示
        n_inc, inc_hint = _manual_rows_incomplete_hint(
            batch_id, config["downstream_file_path"])
        if n_inc:
            result["manual_rows_incomplete"] = n_inc
            base = result.get("message") or "补充的工厂已处理完成"
            result["message"] = f"{base}；{inc_hint}"
        return result


def _write_batch_state(
    batch_id: str,
    status: str,
    state: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """写入批次状态文件 output/{batch_id}/batch_state.json（关键状态转换时调用）。

    写入时机：批次启动（running）、Node5 挂起（pending_review）、
    Node7 完成（completed）、异常（error）。
    写文件失败只记日志，绝不影响跑图。
    """
    try:
        settings = get_settings()
        batch_dir = settings.batch_output_dir(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        state_path = batch_dir / "batch_state.json"

        # 读取已有状态（合并 timestamps）
        existing: dict[str, Any] = {}
        if state_path.exists():
            try:
                existing = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        new_state: dict[str, Any] = {
            "batch_id": batch_id,
            "status": status,
            "updated_at": now,
        }

        # timestamps：started_at 仅在首次写入或 running 时设置
        if "started_at" not in existing or status == "running":
            new_state["started_at"] = now
        else:
            new_state["started_at"] = existing.get("started_at")
        new_state["completed_at"] = now if status == "completed" else existing.get("completed_at")

        # 工厂信息（从 state 提取）
        if state:
            total_set = set((state.get("downstream_requirements") or {}).keys())
            pending_set = set(state.get("pending_factories") or [])
            new_state["factories"] = {
                "total": sorted(total_set),
                "done": sorted(total_set - pending_set),
                "pending": sorted(pending_set),
                "current": (state.get("current_factory_data") or {}).get("factory_name"),
                "deferred": [
                    d.get("factory_name", "") for d in (state.get("deferred_factories") or [])
                ],
            }
            # 输出路径
            output: dict[str, str] = {}
            if state.get("final_output_path"):
                output["containers"] = state["final_output_path"]
            output["declarations_dir"] = str(settings.batch_declarations_dir(batch_id))
            new_state["output"] = output
        else:
            new_state["factories"] = existing.get("factories", {})
            new_state["output"] = existing.get("output", {})

        if error:
            new_state["error"] = error

        state_path.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 状态文件是辅助设施，写失败不影响主流程
        logger.exception("[batch_state] 写入批次状态文件失败 batch_id=%s", batch_id)


def _preextract_progress_path(thread_id: str) -> Path:
    """预提取进度文件路径：SESSIONS_DIR/_preextract_progress_{安全tag}.json。"""
    return SESSIONS_DIR / f"_preextract_progress_{get_settings().safe_path_tag(thread_id)}.json"


class _PreExtractProgress:
    """预提取进度落盘器：线程内维护状态，每次变化原子重写 JSON。

    文件结构：
      {"thread_id": ..., "updated_at": ISO时间,
       "factories": [{"factory": ..., "status": "pending|running|cached|done|failed",
                      "error": str|null, "ts": ISO时间}, ...]}
    """

    def __init__(self, thread_id: str, factories: list[str]) -> None:
        self._path = _preextract_progress_path(thread_id)
        self._state: dict[str, Any] = {
            "thread_id": thread_id,
            "updated_at": self._now(),
            "factories": [
                {"factory": f, "status": "pending", "error": None, "ts": None}
                for f in factories
            ],
        }
        self._by_name = {f["factory"]: f for f in self._state["factories"]}
        self._flush()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def update(self, factory: str, status: str, error: str | None = None) -> None:
        """状态流转：pending → running → cached/done/failed；每次变化即重写。"""
        entry = self._by_name.get(factory)
        if entry is None:
            return
        entry["status"] = status
        entry["error"] = error
        entry["ts"] = self._now()
        self._flush()

    def _flush(self) -> None:
        """原子重写进度文件（tmp → rename）；失败只记日志，绝不抛出。"""
        try:
            self._state["updated_at"] = self._now()
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".tmp", prefix=self._path.stem + ".", dir=str(SESSIONS_DIR))
            try:
                os.write(tmp_fd, json.dumps(self._state, ensure_ascii=False,
                                            indent=1).encode("utf-8"))
            finally:
                os.close(tmp_fd)
            os.replace(tmp_path, self._path)  # POSIX 原子 rename
        except OSError as e:
            logger.warning("[预提取] 进度文件写入失败（不影响预提取）：%s: %s",
                           type(e).__name__, e)


def load_pre_extraction_progress(thread_id: str) -> dict[str, Any] | None:
    """读预提取进度文件（批次端点用）：不存在/损坏返回 None（静默，绝不 500）。"""
    path = _preextract_progress_path(thread_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏的进度文件按无进度处理
        return None
    if not isinstance(data, dict) or not isinstance(data.get("factories"), list):
        return None
    return data


def _start_pre_extraction(thread_id: str) -> None:
    """为当前批次启动后台预提取 daemon 线程（幂等：同一批次只跑一个）。"""
    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    values = snap.values or {}
    pending = list(values.get("pending_factories") or [])
    if not pending:
        return

    upstream_root = values.get("upstream_root") or get_settings().upstream_root
    requirements = values.get("downstream_requirements") or {}

    if thread_id in _known_running:
        return
    _known_running.add(thread_id)

    def _pre_extract():
        try:
            _pre_extract_factories(pending, upstream_root, requirements,
                                   thread_id=thread_id)
        finally:
            _known_running.discard(thread_id)

    t = threading.Thread(target=_pre_extract, daemon=True,
                         name=f"pre-extract-{thread_id}")
    t.start()
    logger.info("[预提取] 批次 %s：后台线程已启动，%d 个工厂待预提取",
                thread_id, len(pending))


def _pre_extract_factories(
    factories: list[str],
    upstream_root: str,
    requirements: dict[str, list[str]],
    thread_id: str | None = None,
) -> None:
    """后台线程主体：逐个工厂串行提取，结果存入 session JSON。

    thread_id 给出时同步落盘预提取进度（对话页批次进度条数据源）：
    启动全部 pending → 每个工厂 running → 缓存跳过 cached / 成功 done /
    异常 failed（error 记异常类型+摘要，截 200 字符）。
    """
    from app.factory_match import (
        load_alias_map, load_folder_match_candidates, match_factory_folder)
    from app.nodes.extraction_node import _run_factory_session, _try_load_cached_session

    root_path = Path(upstream_root).expanduser()
    if not root_path.is_dir():
        logger.warning("[预提取] 上游目录 %s 不存在，跳过", upstream_root)
        return

    folders = [d.name for d in root_path.iterdir() if d.is_dir()]
    alias_map = load_alias_map()
    folder_candidates = load_folder_match_candidates()
    cutoff = get_settings().fuzzy_match_score_cutoff
    progress = _PreExtractProgress(thread_id, factories) if thread_id else None

    for idx, factory in enumerate(factories):
        # 协作式终止检查点（2026-10-14）：逐厂开头查终止标志，命中即静默
        # 退出——预提取是缓存预热，停了无副作用，靠 reset 清缓存重来
        if thread_id and batch_store.is_cancel_requested(thread_id):
            logger.info("[预提取] 批次 %s 收到终止标志，提前退出", thread_id)
            return

        # 缓存已存在且新鲜（路径证据仍在当前批次 upstream_root 之下）则跳过；
        # 新鲜度校验是 rerun_with_paths 改路径重跑场景的兜底：旧根目录留下
        # 的缓存视为陈旧，照常重提，不误用上一批次数据
        if _try_load_cached_session(thread_id, factory, str(root_path)) is not None:
            logger.info("[预提取] 工厂「%s」：缓存已存在，跳过", factory)
            if progress:
                progress.update(factory, "cached")
            continue

        # Node2 逻辑：匹配文件夹
        folder_name, score, method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff,
            folder_candidates=folder_candidates)
        if not folder_name:
            logger.warning("[预提取] 工厂「%s」：未匹配到文件夹，跳过", factory)
            if progress:
                progress.update(factory, "failed", "未匹配到工厂文件夹")
            continue
        folder_path = str(root_path / folder_name)
        expected_skus = requirements.get(factory, [])

        if progress:
            progress.update(factory, "running")
        try:
            session = _run_factory_session(thread_id, folder_path, factory, expected_skus)
        except FatalLLMError as e:
            # 熔断：LLM 致命错误（403/401 账号级）整批预提取终止，
            # 该厂及剩余厂全部标 failed，不再烧无效 token
            logger.error("[预提取] 工厂「%s」LLM 致命错误，终止整批预提取：%s",
                         factory, e)
            if progress:
                progress.update(factory, "failed", f"FatalLLMError: {e}"[:200])
                for rest in factories[idx + 1:]:
                    progress.update(rest, "failed",
                                    "LLM 致命错误，整批预提取已终止")
            return
        except Exception as e:
            logger.exception("[预提取] 工厂「%s」：异常 %s，跳过", factory, e)
            if progress:
                progress.update(factory, "failed",
                                f"{type(e).__name__}: {e}"[:200])
            continue

        # 预提取谎报修复：提取完成但 0 条 → 标 failed（原因「提取结果为空」），
        # 且删掉刚落盘的空 session 缓存，防图内 Node3 命中空缓存直接出占位
        if not session.items and not session.no_code_items:
            logger.warning("[预提取] 工厂「%s」：提取结果为空，标记失败并清除空缓存",
                           factory)
            try:
                from app.extraction.session import batch_session_path
                batch_session_path(thread_id, factory).unlink(missing_ok=True)
            except OSError as e:
                logger.warning("[预提取] 工厂「%s」：清除空 session 缓存失败（%s: %s）",
                               factory, type(e).__name__, e)
            if progress:
                progress.update(factory, "failed", "提取结果为空")
            continue

        logger.info("[预提取] 工厂「%s」：完成（%s，得分 %.1f）",
                    factory, method, score)
        if progress:
            progress.update(factory, "done")


# ---------------------------------------------------------------------------
# 每批次独立 session 缓存目录（2026-08-10）
# ---------------------------------------------------------------------------
# 背景：早期实现把 sessions/*.json 全部平铺在 SESSIONS_DIR 顶层，按工厂
# 名命名、无批次维度。新批次启动时 Node3 缓存命中和后台预提取会沿用
# 上一批次的结果，导致错配。新设计：每批次独立目录
# data/sessions/{safe(batch_id)}/{factory}.json，跨批次物理隔离、零冲突。
# 新批次启动时幂等创建目录，目录已存在则复用——把里面的 *.json 留作下次
# run 增量（rerun 显式清空是另一条路径，由 rerun_batch 负责）。


def _ensure_batch_session_dir(thread_id: str) -> Path:
    """批次启动时确保 data/sessions/{safe(batch_id)}/ 目录存在。

    每批次独立缓存目录。新批次启动会幂等创建，不做归档。
    目录不存在则 mkdir，目录已存在则直接复用，文件留作下次 run 增量。
    返回该目录路径供调用方使用。
    """
    settings = get_settings()
    batch_dir = SESSIONS_DIR / settings.safe_path_tag(thread_id)
    batch_dir.mkdir(parents=True, exist_ok=True)
    return batch_dir


def _mark_batch_error(thread_id: str, exc: Exception) -> None:
    """批次因异常终止时把原因落到 batches 表（看板卡片直出原因）。

    LLM 致命错误（403/401 账号级，重试无意义）给固定中文指引；
    其余异常留「类型: 摘要」（mark_error 内部截 500 字符）。
    mark_error 内部自带 try/except，绝不抛出，不影响原异常上抛。
    """
    if isinstance(exc, FatalLLMError):
        # 2026-10-14：按 FatalLLMError.kind 区分指引文案；
        # kind 为空（历史构造点）落 403 旧文案兜底
        if getattr(exc, "kind", None) == "401":
            batch_store.mark_error(
                thread_id,
                "LLM 调用被拒绝：API 密钥无效或已过期（401 Unauthorized），"
                "请检查 .env 中的 API key 配置")
        else:
            batch_store.mark_error(
                thread_id,
                "LLM 调用被拒绝：模型无访问权限或额度不足，"
                "请检查百炼账号配置（403 AccessDenied）")
    else:
        batch_store.mark_error(thread_id, f"{type(exc).__name__}: {exc}"[:500])


def _mark_batch_aborted(thread_id: str) -> None:
    """批次被用户终止时落 aborted 状态（2026-10-14 批次终止）。

    三件事：update_status(tid,"aborted") + 清终止标志 + 状态文件落 aborted
    + info 日志。aborted 不填 completed_at、不触发结案备份
    （update_status 的备份挂钩只认 completed）。
    与 _mark_batch_error 同哲学：自带 try/except 绝不抛出。
    """
    try:
        batch_store.update_status(thread_id, "aborted")
        batch_store.clear_cancel_requested(thread_id)
        _write_batch_state(thread_id, "aborted")
        logger.info("批次已终止 | thread_id=%s", thread_id)
    except Exception:  # noqa: BLE001 收尸是辅助设施，绝不抛出
        logger.warning("批次终止落状态失败 | thread_id=%s", thread_id,
                       exc_info=True)


def run_until_interrupt(
    thread_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
    factory_alias_overrides: dict[str, str] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """启动流程，执行 Node1-4 直到 Node5 的 interrupt 挂起（或全程无挂起跑完）。

    factory_alias_overrides：批次级「仅本次生效」工厂名对照（W5），
    非空才写进 initial_state（Node2 匹配的最高优先档，不落盘）。
    on_progress：节点级进度回调（W4a），收到至少含
    node/factory/done/total/message 的事件 dict；抛异常不影响跑图。

    返回：
      - {"status": "pending_human_review", "review_data": payload}
      - {"status": "completed", "final_state": ...}
    """
    # L2 日志关联：绑定批次号，图内节点日志（context 拷贝传播）自动携带；
    # logging_context 退出时恢复前值（嵌套调用安全，不抹外层绑定）
    with logging_context(thread_id=thread_id):
        graph = get_graph()
        initial_state: dict[str, Any] = {}
        if downstream_file_path:
            initial_state["downstream_file_path"] = downstream_file_path
        if upstream_root:
            initial_state["upstream_root"] = upstream_root
        if factory_filter:
            initial_state["factory_filter"] = factory_filter
        if factory_alias_overrides:
            initial_state["factory_alias_overrides"] = factory_alias_overrides
        initial_state["batch_id"] = thread_id
        _ensure_batch_session_dir(thread_id)
        snap = graph.get_state(_config(thread_id))
        # 新一轮运行清掉旧终止标志（2026-10-14）： aborted 退回未执行后
        # 重启同 thread_id 时，残留标志会在首个 Node2 检查点误拦
        batch_store.clear_cancel_requested(thread_id)
        _write_batch_state(thread_id, "running", state=initial_state)

        emit = _make_progress_emitter(on_progress, seed=initial_state)
        try:
            for event in graph.stream(initial_state, _config(thread_id), stream_mode="updates"):
                emit(event)
                if "__interrupt__" in event:
                    payload = event["__interrupt__"][0].value
                    # 首次 interrupt 后启动后台预提取：
                    # 工厂 A 挂起等审核 → 后台线程开始提取工厂 B、C、D…
                    _start_pre_extraction(thread_id)
                    # 从 checkpoint 读取当前 state 写入批次状态
                    current = graph.get_state(_config(thread_id))
                    _write_batch_state(thread_id, "pending_review", state=current.values)
                    return {
                        "status": "pending_human_review",
                        "thread_id": thread_id,
                        "review_data": payload,
                    }
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(thread_id)
            return {"status": "aborted", "thread_id": thread_id}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(thread_id, "error", error=str(e))
            # 熔断落状态：批次因异常终止，看板卡片必须能看到原因
            _mark_batch_error(thread_id, e)
            raise

        final = graph.get_state(_config(thread_id))
        _write_batch_state(thread_id, "completed", state=final.values)
        return {
            "status": "completed",
            "thread_id": thread_id,
            # 缺口工厂名单（全程无挂起跑完也可能有跳过/缺资料的工厂），
            # 调度端摘要据此避免误报「全部工厂处理完成」
            "awaiting_factories": _gap_factories(final.values),
            "final_output_path": final.values.get("final_output_path"),
        }


def resume_order(thread_id: str, resume_data: dict,
                 on_progress: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """用人工反馈数据唤醒挂起的图，继续执行 Node6/7（写 Excel + 落库）。

    resume_data 结构见 nodes/human_review.py 的 docstring。
    on_progress：节点级进度回调（W4a），跟踪器以挂起现场为种子
    （resume 不再经过 node1，工厂全集/队列从 state 预置）。

    审计：stream 前 _prepare_audit 抓取原始 payload 做 diff 快照，
    返回前 _write_audit 落 review_audits（失败只警告，绝不阻塞 resume）。
    """
    graph = get_graph()
    state = graph.get_state(_config(thread_id))
    if not state.next:
        raise ValueError("该任务没有处于等待审核状态，或已完成。")

    # L2 日志关联：resume 与首次运行是不同的请求/context，需重新绑定；
    # 工厂名从挂起现场取（首次运行的绑定已随 logging_context 退出恢复），
    # 保证审核后续节点（Node5 收尾/Node6 写回）日志仍带工厂名
    factory = (state.values.get("current_factory_data") or {}).get("factory_name")
    # 决策 2（阶段四）：剔除发生时的完成文案对比基线（Node6 写前 diff 检出）
    removed_before = _manually_removed_factories(thread_id)
    with logging_context(thread_id=thread_id, factory=factory):
        prepared = _prepare_audit(thread_id, resume_data)

        # 恢复执行，直到下一个 interrupt（多工厂循环时）或 END
        emit = _make_progress_emitter(on_progress, seed=state.values)
        try:
            for event in graph.stream(
                Command(resume=resume_data), _config(thread_id), stream_mode="updates"
            ):
                emit(event)
                if "__interrupt__" in event:
                    payload = event["__interrupt__"][0].value
                    # 每次 interrupt 后继续后台预提取剩余工厂
                    # （工厂 B 审核中 → 后台提取 C、D…）
                    _start_pre_extraction(thread_id)
                    current = graph.get_state(_config(thread_id))
                    _write_batch_state(thread_id, "pending_review", state=current.values)
                    result = {
                        "status": "pending_human_review",
                        "thread_id": thread_id,
                        "review_data": payload,
                    }
                    # 人工改动透出（决策 1）：通道 C 保留/通道 A 覆写留痕入审计；
                    # 决策 4（阶段五）：A 态无提取支撑的人工新增行一并入审计
                    kept, overwritten, qty_changes, kept_rows = \
                        _manual_write_traces(
                            current.values, [factory] if factory else None)
                    if prepared is not None:
                        prepared["changes"].extend(kept + overwritten + kept_rows)
                    if kept:
                        result["manual_overrides_kept"] = len(kept)
                    if qty_changes:
                        result["qty_manual_changes"] = len(qty_changes)
                    if kept_rows:
                        result["manual_rows_kept_no_extract"] = len(kept_rows)
                    _write_audit(prepared, _audit_result_status(prepared, result))
                    return result
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(thread_id)
            return {"status": "aborted", "thread_id": thread_id}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(thread_id, "error", error=str(e))
            # 与 run_until_interrupt 同一不变量：批次因异常终止必须落原因
            _mark_batch_error(thread_id, e)
            raise

        final = graph.get_state(_config(thread_id))
        _write_batch_state(thread_id, "completed", state=final.values)
        gaps = _gap_factories(final.values)
        # 人工改动透出（决策 1 通道 C）：保留的人工修改计入审计 +
        # 结果带 manual_overrides_kept + 完成文案提示（详情页可「覆盖写入」）；
        # 数量列人工改动（决策 1 附则）告警不阻断，完成摘要逐行透出；
        # 决策 4（阶段五）：A 态无提取支撑的人工新增行入审计 + 摘要提示
        kept, overwritten, qty_changes, kept_rows = _manual_write_traces(
            final.values, [factory] if factory else None)
        if prepared is not None:
            prepared["changes"].extend(kept + overwritten + kept_rows)
        if gaps:
            # 图跑到 END 但缺口未补齐（其余工厂缺资料待补充）：
            # status 保持 "success"（接口契约不变），靠 awaiting_factories
            # 字段 + message 把「未全部完成」透给审核页/调度
            message = (f"当前工厂已落库并写入下游表格；本批次仍有 {len(gaps)} 家"
                       f"工厂待补充，单据到齐后可「补充工厂」继续")
        else:
            message = "数据已成功落库并写入下游表格"
        if kept:
            message += (f"；{len(kept)} 处人工修改的单元格已保留未覆盖，"
                        f"详见批次详情审计")
        for q in qty_changes:
            message += (f"；行 {q.get('row')} 数量被人工改过"
                        f"（{q.get('old')}→{q.get('new')}），已按新数量写入")
        # 决策 2（阶段四）：本次 resume 期间检出整厂剔除 → 完成文案透出
        newly_removed = sorted(
            _manually_removed_factories(thread_id) - removed_before)
        if newly_removed:
            message += f"；{_removed_factories_hint(newly_removed)}"
        # 决策 4（阶段五）：A 态保留行 + C 态缺关键列行 → 完成摘要透出
        if kept_rows:
            message += (f"；{len(kept_rows)} 行人工新增的行无提取结果支撑，"
                        f"保留人工行未写入，详见批次详情审计")
        n_inc, inc_hint = _manual_rows_incomplete_hint(
            thread_id, final.values.get("downstream_file_path"))
        if n_inc:
            message += f"；{inc_hint}"
        result = {
            "status": "success",
            "message": message,
            "awaiting_factories": gaps,
            "manual_overrides_kept": len(kept),
            "qty_manual_changes": len(qty_changes),
            "final_validation_status": final.values.get("validation_status"),
            "final_output_path": final.values.get("final_output_path"),
        }
        if kept_rows:
            result["manual_rows_kept_no_extract"] = len(kept_rows)
        if n_inc:
            result["manual_rows_incomplete"] = n_inc
        if newly_removed:
            result["manually_removed_factories"] = newly_removed
        _write_audit(prepared, _audit_result_status(prepared, result))
        return result


def rerun_with_paths(
    thread_id: str,
    upstream_root: str | None = None,
    downstream_file_path: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """对话改路径后的当前批次重跑：带新路径从 Node1 重新执行到 Node5 挂起。

    机制（langgraph 1.2.9 实测）：挂起线程有未完成的 interrupt 任务，
    Command(goto=...) 输入会被旧任务卡死；必须先 update_state(as_node=START)
    写入新路径并作废旧现场（下一个 checkpoint 从 Node1 重新开始），
    再 invoke(None) 触发 Node1→Node5 全链重跑，Node5 产生新 interrupt payload。

    仅当 thread 处于挂起等待状态时允许重跑；已完成的批次抛错
    （路径修改已写 .env，对后续批次生效）。
    """
    from langgraph.graph import START

    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")
    if not snap.next:
        raise ValueError(
            f"thread {thread_id} 已完成，无法重跑（路径修改已对后续批次生效）"
        )

    update: dict[str, Any] = {}
    if upstream_root:
        update["upstream_root"] = upstream_root
    if downstream_file_path:
        update["downstream_file_path"] = downstream_file_path

    # L2 日志关联：重跑也是一次完整跑图，绑定批次号（工厂名由 Node2 重绑）
    with logging_context(thread_id=thread_id):
        graph.update_state(cfg, update, as_node=START)
        emit = _make_progress_emitter(on_progress, seed=snap.values)
        try:
            for event in graph.stream(None, cfg, stream_mode="updates"):
                emit(event)
                if "__interrupt__" in event:
                    _start_pre_extraction(thread_id)
                    return {
                        "status": "pending_human_review",
                        "thread_id": thread_id,
                        "review_data": event["__interrupt__"][0].value,
                    }
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(thread_id)
            return {"status": "aborted", "thread_id": thread_id}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(thread_id, "error", error=str(e))
            # 与 run_until_interrupt 同一不变量：批次因异常终止必须落原因
            _mark_batch_error(thread_id, e)
            raise

        final = graph.get_state(cfg)
        return {
            "status": "completed",
            "thread_id": thread_id,
            "awaiting_factories": _gap_factories(final.values),
            "final_output_path": final.values.get("final_output_path"),
        }


def retry_factory_extraction(
    thread_id: str,
    folder: str | None = None,
    save: bool = False,
    on_progress: Callable[[dict], None] | None = None,
    confirm_source_changes: bool = False,
) -> dict[str, Any]:
    """单厂重试：只重跑当前挂起工厂的 Node3→Node5，已审核工厂不受影响。

    机制同 rerun_with_paths（langgraph 1.2.9 坑）：update_state(as_node=NODE2)
    作废 Node5 interrupt 现场，invoke 从 Node3 继续。Node2 仅作锚点不会重跑，
    current_factory_data 仍在 state 中供 Node3 使用。

    决策 9 入口二（原件差异检测）：触发前查原件指纹，变了且该厂有行级差异
    （增行/删行/数量改）→ 未确认抛 SourceChangesError（dispatcher 工具转
    needs_confirm 提示，操作员确认后带 confirm_source_changes=True 重入）；
    确认后吸收该厂最新需求进 state（requirements/row_map 合并 + 副本行同步）。
    注意：指纹基线不在此更新——单厂吸收不代表 state 已反映整份原件，
    其余工厂的差异仍应由入口一检出。

    对照注入（W6b）：
    - folder 非 None 时，先经 factory_match.validate_subfolder 校验
      （upstream_root 下现存一级子目录，ValueError 直接上抛），
      再写入 current_factory_data.folder_path 与批次级
      factory_alias_overrides[factory]=folder——仅本次批次生效，不落盘。
    - save=True 且 folder 非 None：save_alias_entries 永久落 alias_map.json，
      后续批次自动生效；返回 dict 带 "alias_saved"。save 异常不阻断重试
      主流程——捕获记 warning，返回里带 "alias_save_error"。
    - save=True 但 folder 为 None：ValueError（无对照可保存，防误用）。
    """
    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")
    if NODE5 not in (snap.next or ()):
        raise ValueError(f"thread {thread_id} 当前未挂起待审核，无法单厂重试")

    cur = snap.values.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    if not factory:
        raise ValueError(f"thread {thread_id} 缺少当前工厂数据，无法单厂重试")

    if save and folder is None:
        raise ValueError("save=true 必须同时提供 folder（无对照可保存）")

    # 决策 9 入口二：原件指纹变了且该厂有行级差异 → 确认门（未确认抛
    # SourceChangesError；确认后吸收该厂最新需求进 state + 副本行同步）
    src_changes = _diff_factory_source_changes(thread_id, snap.values, [factory])
    if src_changes and not confirm_source_changes:
        logger.warning(
            "[单厂重试] 批次 %s 工厂「%s」原件有变更（%s），未确认不重试",
            thread_id, factory,
            source_diff.SourceDiff(changes=tuple(src_changes)).describe())
        raise SourceChangesError(src_changes)
    src_absorb: tuple | None = None
    if src_changes and confirm_source_changes:
        src_path = (snap.values.get("downstream_file_path")
                    or (_read_batch_config(thread_id) or {}).get(
                        "downstream_file_path"))
        if src_path:
            from app.nodes.parse_downstream import parse_downstream_file
            try:
                src_absorb = parse_downstream_file(src_path)
                logger.info("[单厂重试] 批次 %s 工厂「%s」原件变更已确认吸收",
                            thread_id, factory)
            except Exception:  # noqa: BLE001 吸收失败不阻塞重试主流程
                logger.warning("[单厂重试] 批次 %s 原件重解析失败"
                               "（按旧需求继续重试）", thread_id, exc_info=True)
                src_absorb = None

    # 对照注入 / 自动重匹配：先尝试确定 folder_path
    validated_path: Path | None = None
    matched_folder_name: str | None = None

    if folder is not None:
        from app.factory_match import validate_subfolder
        upstream_root = (
            snap.values.get("upstream_root") or get_settings().upstream_root)
        validated_path = validate_subfolder(upstream_root, folder)
        matched_folder_name = folder
    elif not cur.get("folder_path"):
        # 该工厂首次 folder_router 未匹配到文件夹（folder_path 为空），
        # retry 时自动重跑一次文件夹匹配，避免 update_state(as_node=NODE2)
        # 跳过 Node2 后永远 no_folder_matched。
        from app.factory_match import (
            load_alias_map, load_folder_match_candidates, match_factory_folder)
        upstream_root = (
            snap.values.get("upstream_root") or get_settings().upstream_root)
        root_path = Path(upstream_root).expanduser()
        if root_path.is_dir():
            folders = [d.name for d in root_path.iterdir() if d.is_dir()]
            alias_map = load_alias_map()
            cutoff = get_settings().fuzzy_match_score_cutoff
            folder_name, score, method = match_factory_folder(
                factory, folders, alias_map, cutoff=cutoff,
                folder_candidates=load_folder_match_candidates())
            if folder_name:
                validated_path = root_path / folder_name
                matched_folder_name = folder_name
                logger.info(
                    "retry 自动重匹配到文件夹: %s -> %s (%s, %.1f)",
                    factory, folder_name, method, score)

    # L2 日志关联：单厂重试也是一次跑图，绑定批次号（工厂名由 Node3 重绑）
    with logging_context(thread_id=thread_id):
        # 置强制重提标志：Node3 跳过会话缓存重新走提取流程；
        # Node3 所有返回分支自清 force_reextract=False，不会残留到后续工厂。
        # 不清 extracted_items——Node3 各分支都会覆写 cur["extracted_items"]
        update: dict[str, Any] = {"force_reextract": True}
        if validated_path is not None:
            # 对照注入：folder_path 直接指向校验/重匹配后的目录；
            # 批次级 factory_alias_overrides 同步写入，仅本次生效不落盘
            cur2 = dict(cur)
            cur2["folder_path"] = str(validated_path)
            update["current_factory_data"] = cur2
            overrides = dict(snap.values.get("factory_alias_overrides") or {})
            overrides[factory] = matched_folder_name
            update["factory_alias_overrides"] = overrides
        if src_absorb is not None:
            # 决策 9 入口二吸收：该厂最新需求/行映射并入 state；
            # 原件新增行先同步进副本（追加尾部），Node6 写回才能定位
            new_reqs, new_rm = src_absorb
            if factory in new_reqs:
                merged_reqs = dict(
                    snap.values.get("downstream_requirements") or {})
                merged_rm = dict(snap.values.get("downstream_row_map") or {})
                merged_reqs[factory] = new_reqs[factory]
                merged_rm[factory] = new_rm.get(factory) or {}
                update["downstream_requirements"] = merged_reqs
                update["downstream_row_map"] = merged_rm
                try:
                    _sync_output_copy_rows(thread_id, src_path, {factory},
                                           new_rm)
                except Exception:  # noqa: BLE001 同步失败不阻塞重试主流程
                    logger.warning("[单厂重试] 批次 %s 副本行同步失败"
                                   "（新增行写回将按缺失跳过）",
                                   thread_id, exc_info=True)
        graph.update_state(cfg, update, as_node=NODE2)

        alias_saved: dict | None = None
        alias_save_error: str | None = None
        if save and validated_path is not None:
            from app.factory_match import save_alias_entries
            try:
                # 永久对照落盘（.bak 备份 + 原子写），后续批次自动生效
                alias_saved = save_alias_entries({factory: folder})
            except Exception as e:  # noqa: BLE001 落盘失败不阻断重试主流程
                logger.warning(
                    "对照永久保存失败（重试仍继续）: %s -> %s (%s)",
                    factory, folder, e)
                alias_save_error = f"{type(e).__name__}: {e}"

        emit = _make_progress_emitter(on_progress, seed=snap.values)
        try:
            for event in graph.stream(None, cfg, stream_mode="updates"):
                emit(event)
                if "__interrupt__" in event:
                    _start_pre_extraction(thread_id)
                    result: dict[str, Any] = {
                        "status": "pending_human_review",
                        "thread_id": thread_id,
                        "factory": factory,
                        "review_data": event["__interrupt__"][0].value,
                    }
                    if alias_saved is not None:
                        result["alias_saved"] = alias_saved
                    if alias_save_error is not None:
                        result["alias_save_error"] = alias_save_error
                    if src_absorb is not None:
                        result["source_changes_absorbed"] = src_changes
                    return result
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(thread_id)
            return {"status": "aborted", "thread_id": thread_id}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(thread_id, "error", error=str(e))
            # 与 run_until_interrupt 同一不变量：批次因异常终止必须落原因
            _mark_batch_error(thread_id, e)
            raise

        final = graph.get_state(cfg)
        result = {
            "status": "completed",
            "thread_id": thread_id,
            "factory": factory,
            "final_output_path": final.values.get("final_output_path"),
        }
        if alias_saved is not None:
            result["alias_saved"] = alias_saved
        if alias_save_error is not None:
            result["alias_save_error"] = alias_save_error
        if src_absorb is not None:
            result["source_changes_absorbed"] = src_changes
        return result


def force_extract_file(
    thread_id: str,
    file_path: str,
    pages: list[int] | None = None,
    force_vision: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """指定单个文件强制重新提取（跳过自动目标识别，可指定页码/强制视觉）。

    适用场景：
    - 自动识别选错了箱单（比如挑了报关汇总版）
    - 箱单是扫描件/图片，自动识别扫不出
    - 一份 PDF 混了多份单据，操作员只要其中几页
    - 文本层识别把数字读错，要换看图方式重新识别

    机制：
    1. 校验批次挂起（NODE5 in snap.next）
    2. 校验 file_path 在批次 upstream_root 之下（防路径穿越）
    3. 校验 pages（每个 1..≤500、去重升序、长度 ≤ 12）
    4. 从批次专属 session 目录加载 FactorySession
    5. 调 session.force_extract(session, file_path, pages=pages)
    6. 原子写回 session JSON（与 Node3 写策略一致）
    7. graph.update_state(as_node=NODE2) 不带 force_reextract=True
       ——要 Node3 命中缓存、读到刚写入的 force_extract 结果
    8. graph.stream 直到 __interrupt__，返回新 review_data
    """
    from app.extraction.session import (
        FactorySession,
        batch_session_dir,
        batch_session_path,
        force_extract as session_force_extract,
    )

    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")
    if NODE5 not in (snap.next or ()):
        raise ValueError(f"thread {thread_id} 当前未挂起待审核，无法指定文件提取")

    cur = snap.values.get("current_factory_data") or {}
    factory = cur.get("factory_name")
    if not factory:
        raise ValueError(f"thread {thread_id} 缺少当前工厂数据，无法指定文件提取")

    # 页码 sanity：1-based、去重升序、范围 1..500、长度 ≤ 12
    clean_pages: list[int] | None = None
    if pages is not None:
        if not isinstance(pages, list) or not all(isinstance(p, int) for p in pages):
            raise ValueError("pages 必须是整数列表")
        clean_pages = sorted(set(pages))
        if not clean_pages:
            clean_pages = None
        else:
            if clean_pages[0] < 1 or clean_pages[-1] > 500:
                raise ValueError(f"pages 必须在 1..500 范围内，实际={clean_pages}")
            if len(clean_pages) > 12:
                raise ValueError(
                    f"pages 长度 {len(clean_pages)} 超过上限 12，请缩小范围"
                )

    # 路径白名单：必须在本批次 upstream_root 之下，且是真实文件
    upstream_root = (
        snap.values.get("upstream_root") or get_settings().upstream_root)
    file_path_obj = Path(file_path).expanduser().resolve()
    if not file_path_obj.is_file():
        raise ValueError(f"文件不存在: {file_path}")
    upstream_root_obj = Path(upstream_root).expanduser().resolve()
    try:
        file_path_obj.relative_to(upstream_root_obj)
    except ValueError as e:
        raise ValueError(
            f"文件不在本批次上游工厂文件夹之下，禁止操作 | "
            f"file={file_path_obj} | upstream_root={upstream_root_obj}"
        ) from e

    # 加载批次专属 session JSON（不存在时报清晰错误，避免 FactorySession.load
    # 静默返回空 session 覆盖既有成果）
    sess_path = batch_session_path(thread_id, factory)
    if not sess_path.is_file():
        raise ValueError(
            f"该工厂尚无提取会话（{sess_path}），请先用 retry_factory 重跑整厂后再指定文件"
        )
    session_obj = FactorySession.load(str(sess_path))

    # 执行强制提取（force_vision 在 pages 有值时自动为 True）
    with logging_context(thread_id=thread_id):
        process_result = session_force_extract(
            session_obj, str(file_path_obj), pages=clean_pages,
            force_vision=force_vision,
        )
        if process_result.action == "channel_error":
            raise ValueError(process_result.message or "提取失败")

        # 原子写回 session JSON（与 extraction_node.py:141-148 一致）
        import os, tempfile as _tempfile
        sess_dir = batch_session_dir(thread_id)
        sess_dir.mkdir(parents=True, exist_ok=True)
        data = session_obj.to_dict()
        fd, tmp_path = _tempfile.mkstemp(
            dir=str(sess_dir), prefix=f".{factory}.", suffix=".json.tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                import json as _json
                _json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(sess_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # 不置 force_reextract=True：要让 Node3 命中缓存读回刚才写的内容
        graph.update_state(cfg, {}, as_node=NODE2)

        emit = _make_progress_emitter(on_progress, seed=snap.values)
        try:
            for event in graph.stream(None, cfg, stream_mode="updates"):
                emit(event)
                if "__interrupt__" in event:
                    _start_pre_extraction(thread_id)
                    return {
                        "status": "pending_human_review",
                        "thread_id": thread_id,
                        "factory": factory,
                        "file_path": str(file_path_obj),
                        "pages": clean_pages,
                        "review_data": event["__interrupt__"][0].value,
                        "message": process_result.message,
                    }
        except batch_store.BatchAbortedError:
            # 用户主动终止（2026-10-14）：控制流异常，不是系统异常——
            # 落 aborted 而非 error，不向上抛；新增跑图路径必须同样处理
            _mark_batch_aborted(thread_id)
            return {"status": "aborted", "thread_id": thread_id}
        except Exception as e:  # noqa: BLE001
            _write_batch_state(thread_id, "error", error=str(e))
            # 与 run_until_interrupt 同一不变量：批次因异常终止必须落原因
            _mark_batch_error(thread_id, e)
            raise

        final = graph.get_state(cfg)
        return {
            "status": "completed",
            "thread_id": thread_id,
            "factory": factory,
            "file_path": str(file_path_obj),
            "pages": clean_pages,
            "final_output_path": final.values.get("final_output_path"),
            "message": process_result.message,
        }


def get_order_state(thread_id: str) -> dict[str, Any]:
    """查询指定 thread_id 的当前状态（前端轮询/调试入口）。"""
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    return {
        "thread_id": thread_id,
        "exists": bool(snap.values),
        "next_nodes": list(snap.next),
        "values": snap.values,
    }


def get_review_payload(thread_id: str) -> dict[str, Any] | None:
    """从 checkpoint 读取当前挂起的 interrupt payload（审核界面刷新后恢复现场用）。

    注意：payload 不在 state.values 里，而是在 tasks[].interrupts 中。
    未挂起或 thread_id 不存在时返回 None。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    for task in snap.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


def _rebuild_items_from_output_excel(state: dict[str, Any], factory_name: str) -> list[dict]:
    """从最终输出 Excel 按工厂+SKU 聚合重建 calculated_items（兜底）。

    读取 state.values 中的 final_output_path，按 MAKER_MEI_KJ 过滤指定工厂，
    按 SHOHIN_CD 聚合 D_HACCHU_SU / 净重 / 毛重，并反算单件重量。
    读取失败时只记 warning，返回空列表，不阻塞 reopen。
    """
    path = state.get("final_output_path")
    if not path:
        return []
    try:
        wb = load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "reopen Excel 兜底读取失败: %s: %s", type(e).__name__, e
        )
        return []

    settings = get_settings()
    header = [c.value for c in ws[1]]

    def _idx(name: str) -> int | None:
        try:
            return header.index(name)
        except ValueError:
            return None

    col_factory = _idx(settings.col_factory)
    col_sku = _idx(settings.col_sku)
    col_qty = _idx("D_HACCHU_SU")
    col_net = _idx(settings.col_net)
    col_gross = _idx(settings.col_gross)
    col_name_kj = _idx("SHOHIN_MEI_KJ")
    col_name_e = _idx("SHOHIN_MEI_E")
    col_name_cn = _idx(settings.col_name_cn)

    if col_factory is None or col_sku is None or col_qty is None \
            or col_net is None or col_gross is None:
        logger.warning("reopen Excel 兜底缺少必要列")
        return []

    aggregated: dict[str, dict] = {}
    max_idx = max(col_factory, col_sku, col_qty, col_net, col_gross)
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) <= max_idx:
            continue
        if row[col_factory] != factory_name:
            continue
        sku = str(row[col_sku] or "")
        if not sku:
            continue
        try:
            qty = float(row[col_qty] or 0)
            net = float(row[col_net] or 0)
            gross = float(row[col_gross] or 0)
        except (TypeError, ValueError):
            continue
        if sku not in aggregated:
            sku_name = None
            if col_name_kj is not None:
                sku_name = row[col_name_kj]
            if not sku_name and col_name_e is not None:
                sku_name = row[col_name_e]
            aggregated[sku] = {
                "sku": sku,
                "qty": qty,
                "net": net,
                "gross": gross,
                "sku_name": sku_name,
                "name_cn": row[col_name_cn] if col_name_cn is not None else None,
            }
        else:
            aggregated[sku]["qty"] += qty
            aggregated[sku]["net"] += net
            aggregated[sku]["gross"] += gross

    out: list[dict] = []
    for agg in aggregated.values():
        qty = agg["qty"]
        net = agg["net"]
        gross = agg["gross"]
        unit_net, net_formula, _ = _safe_div(net, qty)
        unit_gross, gross_formula, _ = _safe_div(gross, qty)
        out.append({
            "sku": agg["sku"],
            "extracted_data": {
                "total_quantity": qty,
                "total_net_weight": net,
                "total_gross_weight": gross,
                "weight_unit": "KG",
                "source_file": "reconstructed_from_output_excel",
                "sku_name": agg["sku_name"],
            },
            "calculation": {
                "net_formula": net_formula,
                "gross_formula": gross_formula,
                "calculated_unit_net": unit_net,
                "calculated_unit_gross": unit_gross,
            },
            "status": "Normal",
            "error_msg": None,
            "is_human_edited": False,
            "is_new_sku": False,
            "unexpected_sku": False,
            "db_record": {},
            "name_cn": agg["name_cn"],
        })
    return out


def reopen_factory_for_edit(thread_id: str, factory_name: str) -> dict[str, Any] | None:
    """重开已审核工厂：复用 Node5 同一份 payload 构建逻辑，与首次审核字段一致。

    数据源优先级：
    1. factory_outputs[factory_name] 快照（dict 格式，新批次）
    2. factory_outputs[factory_name] 旧格式列表（兼容旧批次）
    3. Excel 兜底重建（snapshot 缺失）

    步骤：
    1. 取快照 → 兼容旧格式 → 缺失则 Excel 兜底
    2. 刷新 db_record（与首次审核对齐）
    3. 调 build_review_payload 走与 Node5 同一路径
    4. 标 reopen_mode=True
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    values = snap.values or {}
    cur = values.get("current_factory_data") or {}

    snapshot = _resolve_factory_snapshot(values, factory_name, cur)

    if snapshot is None:
        # 无快照且 Excel 兜底也无数据 → 404
        return None

    # 主库刷新：让老 SKU 的 db_record 与首次审核一致
    snapshot["calculated_items"] = _enrich_items_with_db_records(
        snapshot.get("calculated_items") or [], factory_name,
    )

    # 复用 Node5 同一份 payload 构建
    from app.nodes.review_payload import build_review_payload
    payload = build_review_payload(snapshot)
    payload["reopen_mode"] = True
    # 决策 9 入口三：原件指纹变了且该厂有结构差异 → 警告条数据（不阻断编辑，
    # 前端按「提取失败待补录」黄条先例渲染）；无差异/检测失败 → None
    payload["source_changes_warning"] = _source_changes_warning(
        thread_id, factory_name, values)
    return payload


def _resolve_factory_snapshot(
    values: dict[str, Any],
    factory_name: str,
    cur: dict[str, Any],
) -> dict[str, Any] | None:
    """解析 reopen 用的工厂快照（dict 格式，与 current_factory_data 同构）。

    优先级：
    1. factory_outputs[factory_name] 快照（dict）
    2. factory_outputs[factory_name] 旧格式（list）→ 包成 dict
    3. 当前工厂 current_factory_data（factory_name 匹配）
    4. Excel 兜底重建
    """
    factory_outputs = values.get("factory_outputs") or {}
    raw = factory_outputs.get(factory_name)

    # 1) 新格式快照
    if isinstance(raw, dict) and raw:
        # 浅拷贝避免污染原 state
        snap = dict(raw)
        snap.setdefault("factory_name", factory_name)
        return snap

    # 2) 旧格式列表（仅 calculated_items）→ 补默认上下文
    if isinstance(raw, list) and raw:
        folder_path = _resolve_reopen_factory_folder(values, factory_name)
        expected = set(
            (values.get("downstream_requirements") or {}).get(factory_name) or []
        )
        have = {str(i.get("sku") or "") for i in raw}
        return {
            "factory_name": factory_name,
            "calculated_items": raw,
            "folder_path": folder_path,
            "source_documents": _list_source_documents(folder_path),
            "missing_skus": sorted(expected - have),
            "extraction_issues": [],
            "extraction_coverage": {},
        }

    # 3) 当前工厂是目标工厂
    if cur.get("factory_name") == factory_name and cur.get("calculated_items"):
        return dict(cur)

    # 4) Excel 兜底重建
    items = _rebuild_items_from_output_excel(values, factory_name)
    if not items:
        return None
    folder_path = _resolve_reopen_factory_folder(values, factory_name)
    return {
        "factory_name": factory_name,
        "calculated_items": items,
        "folder_path": folder_path,
        "source_documents": _list_source_documents(folder_path),
        "missing_skus": [],
        "extraction_issues": [],
        "extraction_coverage": {},
    }


def _load_factory_db_records(factory_name: str) -> dict[str, dict]:
    """一次性取出该工厂全部 SKU 主数据（与 compute_align 同口径）。

    查询异常直接抛出，由调用方决定回退策略。
    """
    from app.db.models import Factory, FactorySKU

    db_records: dict[str, dict] = {}
    with get_session() as session:
        factory = session.scalar(
            select(Factory).where(Factory.factory_name == factory_name)
        )
        if factory:
            rows = session.scalars(
                select(FactorySKU).where(
                    FactorySKU.factory_id == factory.factory_id,
                )
            ).all()
            db_records = {
                r.sku_code: {
                    "name_cn": r.name_cn,
                    "name_en": r.name_en,
                    "name_jp": r.name_jp,
                    "hs_code": r.hs_code,
                    "inspection_required": r.inspection_required,
                    "unit_net_weight": (
                        float(r.unit_net_weight)
                        if r.unit_net_weight is not None else None
                    ),
                    "unit_gross_weight": (
                        float(r.unit_gross_weight)
                        if r.unit_gross_weight is not None else None
                    ),
                }
                for r in rows
            }
    return db_records


def _enrich_items_with_db_records(
    items: list[dict], factory_name: str,
) -> list[dict]:
    """用主库刷新 items 的 db_record，让 reopen 字段与首次审核对齐。

    老 SKU：合并主库字段到 db_record（与 compute_align.py 同口径）
    新 SKU：保持 db_record={}，由 build_review_payload 补 fields_to_fill
    """
    try:
        db_records = _load_factory_db_records(factory_name)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "reopen 主库刷新失败（继续使用 items 原 db_record）: "
            "factory=%s: %s: %s", factory_name, type(e).__name__, e,
        )
        return items

    for item in items:
        sku = str(item.get("sku") or "")
        record = db_records.get(sku)
        if record is None:
            # 新 SKU：保持 db_record={}，build_review_payload 补 fields_to_fill
            item["db_record"] = {}
        else:
            item["db_record"] = record
    return items


def _looks_like_path(value: str) -> bool:
    """判断字符串是否为真实文件路径（与 extraction_node 逻辑一致）。"""
    if "/" in value or "\\" in value:
        return True
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def _resolve_reopen_factory_folder(state: dict[str, Any], factory_name: str) -> str | None:
    """为重开的已审核工厂定位其上游文件夹。

    优先从 factory_outputs 里该工厂 item 的 source_file 反推原始文件夹；
    无法反推时回退到 match_factory_folder 在 upstream_root 下重新匹配。
    """
    settings = get_settings()
    upstream_root = Path(state.get("upstream_root") or settings.upstream_root).expanduser()

    # 1) 从已审核 item 的 source_file 反推
    factory_outputs = state.get("factory_outputs") or {}
    for item in factory_outputs.get(factory_name) or []:
        source_file = (item.get("extracted_data") or {}).get("source_file") or ""
        if not _looks_like_path(source_file):
            continue
        try:
            rel = Path(source_file).resolve().relative_to(upstream_root.resolve())
            folder_name = rel.parts[0]
            folder_path = upstream_root / folder_name
            if folder_path.is_dir():
                return str(folder_path)
        except (ValueError, IndexError):
            continue

    # 2) fallback：用工厂名重新匹配文件夹
    if upstream_root.is_dir():
        from app.factory_match import (
            load_alias_map, load_folder_match_candidates, match_factory_folder)
        folders = [d.name for d in upstream_root.iterdir() if d.is_dir()]
        alias_map = load_alias_map()
        folder_name, _score, _method = match_factory_folder(
            factory_name, folders, alias_map, cutoff=settings.fuzzy_match_score_cutoff,
            folder_candidates=load_folder_match_candidates(),
        )
        if folder_name:
            return str(upstream_root / folder_name)

    return None


def _list_source_documents(folder_path: str | None) -> list[str]:
    """按 folder_router 同样的扩展名过滤列出单据文件。"""
    if not folder_path:
        return []
    root = Path(folder_path).expanduser()
    if not root.is_dir():
        return []
    from app.nodes.folder_router import SUPPORTED_EXTS
    return sorted(
        str(p) for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )


def _build_reopen_items_payload(items: list[dict], thread_id: str,
                                factory_name: str) -> list[dict]:
    """把 calculated_items 转成 review_data.items 形状 + ReviewAudit 反向填充。

    反向填充：
    - changes_json 中的 diff → 覆写 extracted_data 对应字段
    - new_skus_json → 补 name_cn / hs_code / inspection_required / name_en / name_jp
    """
    audit = _latest_approved_audit(thread_id, factory_name)
    changes = json.loads(audit.changes_json or "[]") if audit else []
    new_skus = json.loads(audit.new_skus_json or "[]") if audit else []

    by_sku_changes: dict[str, list[dict]] = {}
    for c in changes:
        by_sku_changes.setdefault(str(c.get("sku") or ""), []).append(c)
    by_sku_new = {str(n.get("sku") or ""): n for n in new_skus}

    out: list[dict] = []
    for item in items:
        sku = str(item.get("sku") or "")
        ed = dict(item.get("extracted_data") or {})
        # 反向覆写人工改过的字段
        for c in by_sku_changes.get(sku, []):
            field = c.get("field")
            if field:
                ed[field] = c.get("new")
        # 反向补新 SKU 的人工补录字段
        ns = by_sku_new.get(sku)
        rebuilt = dict(item)
        rebuilt["extracted_data"] = ed
        rebuilt["is_human_edited"] = bool(by_sku_changes.get(sku)) or bool(ns)
        if ns:
            for f in ("name_cn", "name_en", "name_jp",
 "hs_code", "inspection_required"):
                v = ns.get(f)
                if v is not None:
                    rebuilt[f] = v
        out.append(rebuilt)
    return out


def _latest_approved_audit(thread_id: str, factory_name: str) -> ReviewAudit | None:
    """查本批次本工厂最近一条已批准的 ReviewAudit。无返回 None。"""
    try:
        with get_session() as db:
            return db.scalar(
                select(ReviewAudit)
                .where(
                    ReviewAudit.thread_id == thread_id,
                    ReviewAudit.factory_name == factory_name,
                    ReviewAudit.approved.is_(True),
                )
                .order_by(ReviewAudit.ts.desc())
                .limit(1)
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "查 ReviewAudit 失败（reopen 不阻塞）| thread=%s | factory=%s | %s",
            thread_id, factory_name, e,
        )
        return None


def apply_reopen_payload(thread_id: str, factory_name: str,
                         resume_data: dict) -> dict[str, Any]:
    """把 reopen 模式的编辑结果写回 Excel + master.db，不污染 LangGraph state。

    通过构造临时 state dict 复用现有 writer._write_excel / _upsert_db 逻辑，
    不重构 writer.py。调 _write_audit 留痕（同 resume_order 审计契约）。
    """
    from app.nodes import writer as writer_mod  # 延迟导入避免循环

    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    values = snap.values or {}
    cur = values.get("current_factory_data") or {}

    # 目标工厂不一定是当前工厂；允许 reopen 已审核过的其他工厂
    if not values.get("downstream_file_path"):
        raise ValueError(f"批次 {thread_id} 缺少 downstream_file_path，无法 reopen")

    # 构造临时 state（最小字段集供 _write_excel / _upsert_db 使用）
    raw_items = (resume_data or {}).get("items") or []
    # 人工删除的条目（审核页「删除条目」）：不进 calculated_items（不落库），
    # 但需记住 sku——此前已写入输出 Excel 的三列要清空（见下方 clear_sku_rows）
    items = [i for i in raw_items if not i.get("deleted")]
    deleted_skus = [str(i.get("orig_sku") or i.get("sku") or "")
                    for i in raw_items if i.get("deleted")]

    # 取原始 items 做 diff：当前工厂直接取；已审核工厂从快照/旧格式列表/Excel 重建
    if cur.get("factory_name") == factory_name:
        original_items = cur.get("calculated_items") or []
    else:
        snapshot = _resolve_factory_snapshot(values, factory_name, cur)
        original_items = (snapshot or {}).get("calculated_items") or []

    fake_state: dict[str, Any] = {
        "batch_id": values.get("batch_id") or thread_id,
        "downstream_file_path": values["downstream_file_path"],
        # 决策 4（阶段五）：_write_excel 的人工新增行三态分类/并入计划
        # 以真实需求集为基准（缺了会把 A 态误判成 B 态）
        "downstream_requirements": values.get("downstream_requirements") or {},
        "downstream_row_map": values.get("downstream_row_map") or {},
        "validation_status": "Approved",  # reopen 视为通过
        "current_factory_data": {
            **cur,
            "factory_name": factory_name,
            "calculated_items": items,
        },
    }

    with logging_context(thread_id=thread_id, factory=factory_name):
        out_path = writer_mod._ensure_output_copy(fake_state)
        # 通道 A（决策 1）：reopen 重新提交本身就是「要求重新覆盖」的意图，
        # 人工非空值直接覆盖/清空，被盖旧值逐单元格留痕 manual_value_overwritten
        written, _skipped_placeholder = writer_mod._write_excel(
            fake_state, out_path, channel="rework")
        inserted, updated = writer_mod._upsert_db(fake_state)
        # reopen 删除：清空已写入的三列单元格（主库历史单重不动）
        cleared = writer_mod.clear_sku_rows(
            fake_state, out_path, deleted_skus, channel="rework")
    manual_overwritten = list(
        fake_state["current_factory_data"].get("manual_overwritten_cells") or [])
    # 决策 4（阶段五）：reopen 写前 diff 的人工新增行并入计划（A 态需求
    # 追加由下方 update_state 持久化；B 态全新厂 reopen 不驱动跑图，提示
    # 走「补充工厂」入口）
    manual_merge = fake_state["current_factory_data"].pop(
        "_manual_added_merge", None)

    # 回写 factory_outputs 快照：否则 reopen 的修改只落在 Excel/DB，
    # 下次重开读到的仍是旧快照（reopen 数据源优先级 快照 > Excel 兜底）。
    # 仅批次已结束（无挂起 interrupt）时更新——挂起中 update_state 会销毁
    # Node5 interrupt 任务（langgraph 实测），进行中批次维持旧快照，该边界下
    # 重开仍显示改前的值，属已知取舍（数据本身已写入 Excel/DB）。
    # as_node 锚点必须用 NODE7（终点节点，出边只有 →END）：update_state 会按
    # as_node 的出边重算 next；锚 NODE6 会让条件边 _route_after_writer 重路由
    # 出 next=(NODE7,)，批次永远卡在「进行中」（2026-08-26 test-86 生产事故）。
    try:
        fresh = graph.get_state(_config(thread_id))
        fresh_outputs = ((fresh.values or {}).get("factory_outputs")) or {}
        raw = fresh_outputs.get(factory_name)
        if fresh.next:
            logger.info("[reopen] 批次仍挂起审核，跳过快照回写"
                        "（避免销毁 interrupt）：%s", factory_name)
        elif isinstance(raw, dict) and raw:
            snap_fac = dict(raw)
            snap_fac["calculated_items"] = items
            # 通道 A 覆写后清理人工改动留痕计数（已解决，防 export 摘要
            # 与下次 resume 审计重复透出），覆写计数沉淀为本次值
            snap_fac["manual_conflicts"] = []
            snap_fac["manual_override_kept"] = 0
            snap_fac["manual_overwritten_cells"] = []
            snap_fac["manual_values_overwritten"] = len(manual_overwritten)
            new_outputs = dict(fresh_outputs)
            new_outputs[factory_name] = snap_fac
            graph.update_state(_config(thread_id),
                               {"factory_outputs": new_outputs},
                               as_node=NODE7)
            logger.info("[reopen] 已回写 factory_outputs 快照：%s", factory_name)
        elif isinstance(raw, list) and raw:
            # 旧格式列表快照（仅 calculated_items）→ 整表替换
            new_outputs = dict(fresh_outputs)
            new_outputs[factory_name] = list(items)
            graph.update_state(_config(thread_id),
                               {"factory_outputs": new_outputs},
                               as_node=NODE7)
            logger.info("[reopen] 已回写 factory_outputs 旧格式快照：%s",
                        factory_name)
        # 无快照（reopen 走 Excel 兜底）：Excel 已更新，下次重开自一致，无需写
    except Exception as e:  # noqa: BLE001 快照回写是辅助设施，不推翻已完成的写入
        logger.warning("⚠️ reopen 快照回写失败: %s: %s", type(e).__name__, e)

    # 决策 4（阶段五）：reopen 检出的人工新增行并入计划持久化——A 态（厂在
    # 批次需求集）需求/行映射并入 state；B 态全新厂 reopen 不驱动跑图，
    # warning 提示走「补充工厂」入口。同样只在批次已结束时写（挂起中
    # update_state 会销毁 interrupt）。辅助设施包死，不推翻已完成的写入。
    if manual_merge is not None:
        try:
            fresh2 = graph.get_state(_config(thread_id))
            if fresh2.next:
                logger.info("[reopen] 批次仍挂起审核，人工新增行并入计划暂不"
                            "写入 state（避免销毁 interrupt）：%s", factory_name)
            else:
                reqs = ((fresh2.values or {})
                        .get("downstream_requirements") or {})
                a_req = {f: s for f, s in
                         (manual_merge["req_updates"] or {}).items()
                         if f in reqs}
                a_rm = {f: m for f, m in
                        (manual_merge["row_map_updates"] or {}).items()
                        if f in reqs}
                if a_req or a_rm:
                    graph.update_state(
                        _config(thread_id),
                        _merge_factories_into_state(fresh2.values, a_req, a_rm),
                        as_node=NODE7)
                    logger.info("[reopen] 人工新增行需求已并入 state：%s",
                                sorted(a_req))
                for bf in manual_merge["b_factories"]:
                    logger.warning("[reopen] 检出全新工厂「%s」的人工新增行，"
                                   "reopen 不入批，请走「补充工厂」入口", bf)
        except Exception as e:  # noqa: BLE001
            logger.warning("⚠️ reopen 人工新增行并入 state 失败: %s: %s",
                           type(e).__name__, e)

    # 审计留痕：复用 _prepare_audit + _write_audit（仅 approved=True 路径）
    try:
        prepared = {
            "thread_id": thread_id,
            "factory_name": factory_name,
            "approved": bool((resume_data or {}).get("approved", True)),
            "edited_count": sum(
                1 for i in items
                if i.get("is_human_edited") or i.get("is_new_sku")
            ) + len(deleted_skus),
            "changes": ([{"sku": s, "field": "条目",
                          "old": "识别条目", "new": "人工删除"}
                         for s in deleted_skus]
                        + _prepare_audit_changes_from_items(items, original_items)
                        + [{**c, "source": "manual_value_overwritten"}
                           for c in manual_overwritten]),
            "new_skus": _prepare_audit_new_skus(items),
        }
        _write_audit(prepared, "reopen")
    except Exception as e:  # noqa: BLE001 同 _write_audit 兜底
        logger.warning("⚠️ reopen 审计落库失败: %s: %s", type(e).__name__, e)

    logger.info(
        "[reopen] thread=%s factory=%s written=%d insert=%d update=%d cleared=%d",
        thread_id, factory_name, written, inserted, updated, cleared,
    )
    return {
        "status": "success",
        "message": (
            f"工厂「{factory_name}」重新提交完成："
            f"写入 {written} 行 Excel / INSERT {inserted} / UPDATE {updated}"
            + (f" / 删除 {cleared} 行" if cleared else "")
        ),
        "thread_id": thread_id,
        "factory_name": factory_name,
        "final_output_path": str(out_path),
        "written": written,
        "inserted": inserted,
        "updated": updated,
    }


def overwrite_protected_rows(thread_id: str) -> dict[str, Any]:
    """批次详情页「覆盖写入」确认入口（决策 1 通道 C 的补写，通道 A 语义）。

    对快照中 protected 行按系统数据（state.factory_outputs 的
    calculated_items）补写三列：数量按副本当前值计算（人工数量优先，
    与数量列附则同口径）；被盖掉的人工旧值逐单元格入审计 changes_json
    （source="manual_value_overwritten"，result_status="overwrite_protected"），
    覆写后 protected 标记解除（refresh_snapshot unprotect）。
    系统侧无该列数据的保护单元格保留人工值不动。

    Returns:
        {"ok": True, "overwritten": N}

    Raises:
        ValueError: 没有 protected 行 / 批次无输出副本（路由层转 400）
    """
    from app.nodes import writer as writer_mod  # 延迟导入避免循环

    graph = get_graph()
    values = graph.get_state(_config(thread_id)).values or {}
    outputs = values.get("factory_outputs") or {}

    snapshot = copy_snapshot.load_snapshot(thread_id)
    protected = [r for r in (snapshot or {}).get("rows") or []
                 if r.get("protected")]
    if not protected:
        raise ValueError("当前没有人工保留待覆盖的单元格")
    copy_path = copy_snapshot.find_copy(thread_id)
    if copy_path is None:
        raise ValueError(f"批次 {thread_id} 无输出副本，无法覆盖写入")

    settings = get_settings()
    wb = load_workbook(copy_path)
    ws = wb[wb.sheetnames[0]]
    header = [c.value for c in ws[1]]
    col_by_field = {
        "name_cn": header.index(settings.col_name_cn) + 1,
        "net": header.index(settings.col_net) + 1,
        "gross": header.index(settings.col_gross) + 1,
    }
    col_qty = header.index(settings.col_qty) + 1
    row_index = copy_snapshot.build_row_index(ws)

    # 系统值来源：factory_outputs 的 calculated_items（按 工厂×SKU 索引，
    # 键成分与快照同样过 norm_key_text 规整）
    items_by_fs: dict[tuple[str, str], dict] = {}
    for fname, fo in outputs.items():
        if not isinstance(fo, dict):
            continue
        for it in fo.get("calculated_items") or []:
            items_by_fs[(copy_snapshot.norm_key_text(fname),
                         copy_snapshot.norm_key_text(it.get("sku")))] = it

    overwritten = 0
    unprotect: set[tuple[str, str]] = set()
    audit_by_factory: dict[str, list[dict]] = {}
    for r in protected:
        k = r.get("key") or {}
        key = (copy_snapshot.norm_key_text(k.get("factory")),
               copy_snapshot.norm_key_text(k.get("sku")),
               int(k.get("seq") or 0))
        key_str = copy_snapshot.row_key_str(key)
        excel_row = row_index.get(key)
        if excel_row is None:
            continue  # 行已被人工删除：随快照结转入墓碑，无可补写
        item = items_by_fs.get((key[0], key[1])) or {}
        calc = item.get("calculation") or {}
        qty = ws.cell(row=excel_row, column=col_qty).value
        qty = float(qty) if isinstance(qty, (int, float)) else 0.0
        sys_vals: dict[str, Any] = {}
        name_cn = item.get("name_cn") or (item.get("db_record") or {}).get("name_cn")
        if name_cn:
            sys_vals["name_cn"] = name_cn
        if calc.get("calculated_unit_net") is not None:
            sys_vals["net"] = round(calc["calculated_unit_net"] * qty, 2)
        if calc.get("calculated_unit_gross") is not None:
            sys_vals["gross"] = round(calc["calculated_unit_gross"] * qty, 2)
        for field in r.get("protected_fields") or []:
            new_val = sys_vals.get(field)
            if new_val is None:
                continue  # 系统侧无该列数据，保留人工值不动
            col = col_by_field[field]
            old_val = ws.cell(row=excel_row, column=col).value
            cell = ws.cell(row=excel_row, column=col, value=new_val)
            writer_mod._apply_write_format(cell)
            audit_by_factory.setdefault(key[0], []).append({
                "sku": key[1], "field": field,
                "old": old_val, "new": new_val,
                "source": "manual_value_overwritten",
            })
            unprotect.add((key_str, field))
            overwritten += 1

    if overwritten:
        writer_mod._probe_writable(copy_path)
        wb.save(copy_path)
        # 覆写落盘后重建快照并解除保护（结转算法：记录值=副本当前值=系统值）
        copy_snapshot.refresh_snapshot(thread_id, unprotect=unprotect)
        # 被盖人工旧值逐单元格入审计（按工厂分组各一条；_write_audit 包死）
        for fname, changes in audit_by_factory.items():
            _write_audit({
                "thread_id": thread_id,
                "factory_name": fname,
                "approved": True,
                "edited_count": len(changes),
                "changes": changes,
                "new_skus": [],
            }, "overwrite_protected")
    logger.info("[覆盖写入] thread=%s 覆盖 protected 单元格 %d 处",
                thread_id, overwritten)
    return {"ok": True, "overwritten": overwritten}


def reextract_document(thread_id: str, path: str,
                       factory_name: str) -> dict[str, Any]:
    """D2「重新识别这个文件」：强制提取单个文件，返回 review item 形状。

    - 跳过 target_identifier 候选/去重判定（人工指定即强制），按后缀走
      _route_extract（excel/pdf/doc/vision 全通道）；
    - 提取结果过 A4 校验（^\\d{13}$ 同口径）：非法 sku 的条目仍返回
      （信息零丢失），标 needs_human_review=True + review_reason 注明；
    - 每个 item 用 _safe_div 计算单重（计算隔离：纯 Python，LLM 不算术），
      并查主库组装 is_new_sku / db_record（与 compute_align 同口径）；
    - 返回形状与 build_review_payload 的 items 元素同构，另附
      needs_human_review 顶置标记供前端合并策略使用；
    - 纯一次性计算：不写 LangGraph checkpoint、不写 session 缓存。

    路径白名单校验在 router 层完成（复用 _resolve_whitelisted，与 D1 一致）。
    失败抛错：ValueError（文件不存在/通道不支持/提取报错）→ router 转 400；
    其他异常（主库查询失败等）→ router 转 500。
    """
    from app.extraction.agent import _route_extract
    from app.extraction.session import _SKU_13_RE
    from app.nodes.compute_align import _safe_div
    from app.nodes.review_payload import build_review_items_payload

    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"文件不存在: {p.name}")

    try:
        res = _route_extract(str(p))
    except Exception as e:  # noqa: BLE001 通道报错统一转 ValueError（含 UnsupportedFileError）
        raise ValueError(f"提取通道失败: {type(e).__name__}: {e}") from e
    if getattr(res, "error", ""):
        raise ValueError(f"提取失败: {res.error}")

    # 主库联查（与 compute_align / reopen 同口径）；失败宁可报错也不误导
    # is_new_sku 判定（零容错：查不到记录≠新 SKU）
    db_records = _load_factory_db_records(factory_name)

    calc_items: list[dict] = []
    review_flags: list[bool] = []
    for it in res.items or []:
        d = it.model_dump() if hasattr(it, "model_dump") else dict(it)
        sku_code = d.get("sku_code")
        needs_review = bool(d.get("needs_human_review"))
        reason = d.get("review_reason") or ""
        if sku_code is not None and not _SKU_13_RE.fullmatch(str(sku_code).strip()):
            # A4 同口径：非法 sku 条目保留返回，标人工复核
            needs_review = True
            reason = ((reason + "；") if reason else "") + \
                "SKU_NON_13_DIGIT：非空 SKU 编码不是 13 位数字"
        # 与 compute_align 同口径：条码优先，无条码落品名交人工模糊匹配
        sku = str(sku_code or d.get("sku_name") or "").strip()
        qty = d.get("total_quantity")
        unit_net, net_formula, net_err = _safe_div(d.get("total_net_weight"), qty)
        unit_gross, gross_formula, gross_err = _safe_div(
            d.get("total_gross_weight"), qty)
        err = net_err or gross_err
        if err:
            status = "Error"
        elif needs_review:
            status = "Needs_Review"
        else:
            status = "Normal"
        record = db_records.get(sku)
        calc_items.append({
            "sku": sku,
            "extracted_data": {
                "total_quantity": qty,
                "total_net_weight": d.get("total_net_weight"),
                "total_gross_weight": d.get("total_gross_weight"),
                "weight_unit": d.get("weight_unit") or "KG",
                "source_file": str(p),
                "sku_name": d.get("sku_name"),
                "review_reason": reason or None,
            },
            "calculation": {
                "net_formula": net_formula,
                "gross_formula": gross_formula,
                "calculated_unit_net": unit_net,
                "calculated_unit_gross": unit_gross,
            },
            "status": status,
            "error_msg": err,
            "is_human_edited": False,
            "is_new_sku": record is None,
            "unexpected_sku": False,
            "db_record": record or {},
        })
        review_flags.append(needs_review)

    # 复用 review_payload 的构建函数，保证与 Node5 首次审核 items 严格同构
    items = build_review_items_payload(calc_items, factory_name)
    for entry, flag in zip(items, review_flags):
        entry["needs_human_review"] = flag

    logger.info("[reextract] 强制重提完成 | thread=%s 工厂=%s 文件=%s 条目=%d",
                thread_id, factory_name, p.name, len(items))
    return {"items": items, "source_file": str(p)}


def _prepare_audit_changes_from_items(new_items: list[dict],
                                     old_items: list[dict]) -> list[dict]:
    """对比 new_items 与 old_items，提取 diff（_prepare_audit 简化版）。

    SKU 改名（批次3 B）：新 sku 不在 old 集合里但携带 orig_sku（或 Node5 留痕
    的 sku_renamed_from）匹配旧项时，显式记一条 sku 改名 change（旧→新），
    数值 diff 也与旧项比对——不落入"找不到对应项"的静默分支。
    """
    old_by_sku = {str(i.get("sku") or ""): i for i in old_items}
    changes: list[dict] = []
    for item in new_items:
        sku = str(item.get("sku") or "")
        old = old_by_sku.get(sku)
        orig_sku = item.get("orig_sku") or item.get("sku_renamed_from")
        if old is None and orig_sku and str(orig_sku) != sku:
            old = old_by_sku.get(str(orig_sku))
            if old is not None:
                changes.append({"sku": sku, "field": "sku",
                                "old": str(orig_sku), "new": sku})
        old = old or {}
        old_ext = old.get("extracted_data") or {}
        new_ext = item.get("extracted_data") or {}
        for f in ("total_quantity", "total_net_weight", "total_gross_weight"):
            new_v = new_ext.get(f)
            if new_v is not None and new_v != old_ext.get(f):
                changes.append({"sku": sku, "field": f,
                                "old": old_ext.get(f), "new": new_v})
        # 提交时勾选「更新历史单重」：扁平结构留痕（ reopen 路径同口径）
        if item.get("update_history_weight"):
            changes.append({
                "sku": sku, "field": "历史单重",
                "old": ((old.get("db_record") or {}).get("unit_net_weight")),
                "new": ((item.get("calculation") or {})
                        .get("calculated_unit_net")),
            })
    return changes


def _prepare_audit_new_skus(items: list[dict]) -> list[dict]:
    """提取 new_skus_json 字段。"""
    out: list[dict] = []
    for item in items:
        if not item.get("is_new_sku"):
            continue
        out.append({
            "sku": item.get("sku"),
            "name_cn": item.get("name_cn"),
            "name_en": item.get("name_en"),
            "name_jp": item.get("name_jp"),
            "hs_code": item.get("hs_code"),
            "inspection_required": item.get("inspection_required"),
        })
    return out


# ---------------------------------------------------------------------------
# 批次管理 API（工作台/批次详情页）：checkpoint 只读枚举 + 状态推导
# ---------------------------------------------------------------------------


def _open_checkpoint_ro() -> sqlite3.Connection:
    """打开 checkpoints.db 的只读连接（URI mode=ro，Windows 兼容）。

    绝不复用 graph 单例内部的 saver 连接（graph.py 的连接无锁，
    跨线程并发读写会互相干扰）；这里每次新建独立只读连接。
    全新部署 db 文件不存在（或文件在但表未建）时先用 SqliteSaver.setup()
    建库建表——mode=ro 打不开不存在的文件、SELECT 打不了无表的库
    （首发批次 500 的坑）。
    """
    path = Path(get_settings().checkpoint_db_abs).resolve()
    conn = None
    if path.exists():
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        if has_table:
            return conn
        conn.close()
    # 文件不存在或无表：初始化 schema 后重开只读连接
    from langgraph.checkpoint.sqlite import SqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    rw = sqlite3.connect(str(path))
    try:
        SqliteSaver(rw).setup()
    finally:
        rw.close()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _list_thread_ids(conn: sqlite3.Connection) -> list[str]:
    """枚举全部批次 thread_id，按最早 checkpoint 倒序（最新批次在前）。

    过滤 "split-" 前缀：分票图与提取图共用同一 checkpoints 库
    （thread_id 约定 split-{批次号}），分票线程不是批次，混入会让
    工作台显示出 "split-xxx" 假批次条目——且在工作台删除它会连带
    删掉分票全部状态（2026-08-25 用户实测踩坑）。
    """
    rows = conn.execute(
        "SELECT thread_id FROM checkpoints "
        "GROUP BY thread_id ORDER BY MIN(checkpoint_id) DESC"
    ).fetchall()
    return [r[0] for r in rows if not r[0].startswith("split-")]


def _thread_created_ts(conn: sqlite3.Connection, thread_id: str) -> str | None:
    """取该 thread 最早 checkpoint 的创建时间（blob 内 msgpack 的 ts 字段）。

    checkpoint 表的 metadata 列没有时间戳，创建时间只能从
    checkpoint blob（ormsgpack 序列化）的 "ts" 键解出；解包失败返回 None。
    """
    row = conn.execute(
        "SELECT checkpoint FROM checkpoints "
        "WHERE thread_id = ? ORDER BY checkpoint_id ASC LIMIT 1",
        (thread_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return ormsgpack.unpackb(row[0])["ts"]
    except Exception:  # noqa: BLE001 序列化格式漂移不应拖垮列表
        return None


def _summarize_snapshot(thread_id: str, snap, created_ts: str | None) -> dict[str, Any]:
    """从 StateSnapshot 推导批次摘要：三态 status + 进度 + 当前工厂。"""
    values = snap.values or {}

    # ---- 状态三态 ----
    if not values:
        status = "unknown"
    elif any(t.interrupts for t in snap.tasks):
        status = "pending_review"   # 有待审核 interrupt
    elif snap.next:
        status = "running"          # 无 interrupt 但 next 非空（LLM 提取中）
    else:
        status = "completed"

    # ---- 进度：分母 = downstream_requirements 键集（有 filter 时取交集）----
    total_set = set((values.get("downstream_requirements") or {}).keys())
    factory_filter = values.get("factory_filter")
    if factory_filter:
        total_set &= set(factory_filter)
    pending = set(values.get("pending_factories") or [])
    current = (values.get("current_factory_data") or {}).get("factory_name")
    # 当前工厂已被 pop 出 pending；图还在跑（next 非空）时不算完成
    done = len(total_set - pending) - (1 if current and snap.next else 0)
    done = max(done, 0)

    return {
        "thread_id": thread_id,
        "status": status,
        "progress": {"done": done, "total": len(total_set), "current_factory": current},
        "current_factory": current,
        "created_at": created_ts,
        "updated_at": snap.created_at,
    }


def get_batch_summary(thread_id: str) -> dict[str, Any]:
    """单批次轻量摘要：三态 status + 进度 + 当前工厂（复用 _summarize_snapshot）。

    调度 Agent 开场提示等只需状态/工厂的轻量场景用；比 get_batch_detail
    轻——不查 audit、不加载工厂会话。thread 不存在时返回 status="unknown"、
    current_factory=None 的摘要（不抛异常，调用方自行判空）。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    return _summarize_snapshot(thread_id, snap, None)


def abort_batch(thread_id: str) -> dict[str, Any]:
    """终止批次（2026-10-14 批次终止）：运行中协作式停止，卡死立即解锁。

    协作路径（有线程存活迹象 / updated_at 新鲜）：置 cancel_requested
    标志，图内 Node2/Node7 检查点与预提取循环在厂边界停下，由六条
    graph.stream 包装层捕获 BatchAbortedError 落 aborted——当前厂成果
    保留、无半厂现场；
    立即路径（僵尸行：有 checkpoint 但无线程存活且 updated_at 超龄，
    或无 checkpoint 的启动早期死行）：直接标 aborted 解锁；
    二次点击（cancel_requested 已置位再点终止）：强制解锁走立即路径。

    已接受残留竞态：立即路径判死瞬间线程其实活着 → 该线程跑到下个
    检查点被标志拦下再走协作分支，_mark_batch_aborted 幂等重标
    aborted，最终一致；若它已在最后一个工厂直跑完成，pipeline_state
    自愈守卫保证状态滞留 aborted 不被覆写——用户退回未执行重跑即可，
    不追完美。

    异常契约（路由层转 HTTP）：
    - 批次不存在 → ValueError（404）；
    - status != "running" → RuntimeError（409，文案按状态区分）；
    - snap 有 interrupt（挂起审核中，自愈口径下不该是 running，防御）
      → RuntimeError（409）。
    """
    row = batch_store.get_batch(thread_id)
    if row is None:
        raise ValueError(f"批次不存在: {thread_id}")
    status = row.get("status")
    if status != "running":
        if status == "pending_review":
            raise RuntimeError(
                f"批次 {thread_id} 挂起审核中，无需终止，可退回未执行")
        if status == "aborted":
            raise RuntimeError(f"批次 {thread_id} 已终止")
        raise RuntimeError(
            f"仅运行中批次可终止（当前状态 {status}）: {thread_id}")

    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    if any(t.interrupts for t in snap.tasks):
        raise RuntimeError(
            f"批次 {thread_id} 挂起审核中，无需终止，可退回未执行")

    # 存活探测启发式：看板启动线程 / 预提取线程存活（线程枚举写法照
    # board.py 僵尸自愈段），或进程级已知在跑集合，或 updated_at 新鲜。
    # 宽容期 600 秒与 board.STALE_RUNNING_SECONDS 同源，本地重声明避免
    # orchestrator → api 反向依赖
    _STALE_RUNNING_SECONDS = 600
    alive = any(
        t.name in (f"board-start-{thread_id}", f"pre-extract-{thread_id}")
        and t.is_alive()
        for t in threading.enumerate())
    fresh = False
    try:
        updated = datetime.fromisoformat(row.get("updated_at"))
        fresh = ((datetime.utcnow() - updated).total_seconds()
                 < _STALE_RUNNING_SECONDS)
    except (TypeError, ValueError):
        fresh = False  # 解析失败视为超龄（与 board.py 僵尸自愈同口径）

    already_requested = bool(row.get("cancel_requested"))
    if (snap.next and not already_requested
            and (alive or thread_id in _known_running or fresh)):
        batch_store.set_cancel_requested(thread_id)
        logger.info("批次终止请求已记录（协作式） | thread_id=%s", thread_id)
        return {"ok": True, "mode": "cooperative",
                "message": "终止请求已记录，将在当前工厂处理完成后停止"
                           "（大模型调用最长约 10 分钟）；如长时间未停止可"
                           "再次点击终止强制解锁"}

    # 立即路径：僵尸行 / 启动早期死行 / 二次点击强制解锁——置标志防线程
    # 假死复活漏拦（若真活着，下个检查点被拦下走协作分支，最终一致），
    # 再直接标 aborted
    batch_store.set_cancel_requested(thread_id)
    batch_store.update_status(thread_id, "aborted")
    _write_batch_state(thread_id, "aborted")
    logger.info("批次已终止（立即解锁） | thread_id=%s", thread_id)
    return {"ok": True, "mode": "immediate",
            "message": "批次已终止，可退回未执行后重新启动"}


def get_batch_upstream_root(thread_id: str) -> str | None:
    """该批次 checkpoint state 里记录的 upstream_root；state 无此键时返回 None。

    供审核页白名单做批次级二级兜底（W1）：改全局路径后，旧批次的单据
    仍应按其创建时（已经过 is_dir 校验）的 root 放行。绝不接受请求参数，
    唯一来源是 checkpoint state。
    注意：state 无 upstream_root 表示该批次走 .env 缺省——由全局白名单
    （自动跟随 settings）覆盖，这里绝不回退 settings 当前值，否则调用方
    会把"当时的 settings"缓存成长期有效的批次 root，改路径后产生陈旧放行。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    root = (snap.values or {}).get("upstream_root")
    return str(root) if root else None


def list_batches() -> dict[str, Any]:
    """批次列表：只读连接枚举 thread，逐个 get_state 推导状态。

    单线程推导异常时降级为 status="error"（其余字段 None），不拖垮整表。
    """
    graph = get_graph()
    with contextlib.closing(_open_checkpoint_ro()) as conn:
        thread_ids = _list_thread_ids(conn)
        created_map = {tid: _thread_created_ts(conn, tid) for tid in thread_ids}

    batches: list[dict[str, Any]] = []
    for tid in thread_ids:
        try:
            snap = graph.get_state(_config(tid))
            batches.append(_summarize_snapshot(tid, snap, created_map[tid]))
        except Exception as e:  # noqa: BLE001 单个坏 thread 不影响整表
            batches.append({
                "thread_id": tid,
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "progress": None,
                "current_factory": None,
                "created_at": created_map[tid],
                "updated_at": None,
            })
    return {"batches": batches}


def _load_factory_session(batch_id: str, factory: str) -> dict[str, Any] | None:
    """加载指定批次的工厂提取会话摘要。

    读取路径为 per-batch 目录 ``data/sessions/{safe(batch_id)}/{factory}.json``，
    不再使用全局旧缓存 ``data/sessions/{factory}.json``，避免把上一批次文件错误
    展示给当前批次。per-batch 会话文件不存在或损坏时返回 None。

    coverage 由 items 键集 vs expected_skus 重算（与 extraction/session.py
    的 coverage() 口径一致）。
    """
    path = SESSIONS_DIR / get_settings().safe_path_tag(batch_id) / f"{factory}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏的会话文件不阻塞批次详情
        return None

    items = data.get("items") or {}
    expected = set(data.get("expected_skus") or [])
    have = set(items.keys())
    if expected:
        coverage = {
            "extracted": len(have & expected),
            "expected": len(expected),
            "missing": sorted(expected - have),
            "extra": sorted(have - expected),
        }
    else:
        coverage = {"extracted": len(have), "expected": None, "missing": [], "extra": []}

    return {
        "factory": factory,
        "status": data.get("status"),
        "updated_at": data.get("updated_at"),
        "issues": data.get("issues") or [],
        "history": data.get("history") or [],
        "file_records": data.get("file_records") or {},
        "targets": data.get("targets") or [],
        "deferred": data.get("deferred") or [],
        "expected_skus": sorted(expected),
        "items": items,
        "coverage": coverage,
    }


def _usage_with_scope() -> dict[str, Any]:
    """全局 LLM 用量摘要 + scope 标注（进程内累计，无 thread 标签）。"""
    summary = usage_tracker.summary()
    summary["scope"] = "process_lifetime"
    summary["note"] = "进程内累计，重启清零；无 thread 标签，为全局用量"
    # 提取失败告警闭环（§3.3）：透出失败分桶与最近失败原因供前端渲染；
    # setdefault 兜底旧版 tracker（summary 缺字段时不 500）
    summary.setdefault("failed_by_kind", {"fatal": 0, "rate_limit": 0, "other": 0})
    summary.setdefault("last_error", None)
    return summary


def get_batch_detail(thread_id: str) -> dict[str, Any]:
    """批次详情：state 摘要 + factories[]（角色+会话摘要）+ audit[] + usage。

    thread 不存在时抛 ValueError（路由层转 404）。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")

    with contextlib.closing(_open_checkpoint_ro()) as conn:
        created_ts = _thread_created_ts(conn, thread_id)

    detail = _summarize_snapshot(thread_id, snap, created_ts)
    values = snap.values

    total_set = set((values.get("downstream_requirements") or {}).keys())
    factory_filter = values.get("factory_filter")
    if factory_filter:
        total_set &= set(factory_filter)
    pending = set(values.get("pending_factories") or [])
    current = (values.get("current_factory_data") or {}).get("factory_name")

    # 审计先行：factories[] 的 skipped 角色判定依赖每厂最新一条 review_audit
    with get_session() as session:
        audit_rows = session.scalars(
            select(ReviewAudit)
            .where(ReviewAudit.thread_id == thread_id)
            .order_by(ReviewAudit.audit_id)
        ).all()
        audit = [{
            "ts": r.ts.isoformat() if r.ts else None,
            "factory_name": r.factory_name,
            "approved": r.approved,
            "edited_count": r.edited_count,
            "changes": json.loads(r.changes_json or "[]"),
            "new_skus": json.loads(r.new_skus_json or "[]"),
            "result_status": r.result_status,
        } for r in audit_rows]

    # 每厂最新一条审计（audit_id 升序遍历，后者覆盖前者；一次查询不 N+1）。
    # 最新一条 approved=true 即视为「已通过」——若之前被跳过后经「补充工厂」
    # 重审通过，更晚的 approved=true 行成为最新一条，角色自然回到 done
    latest_audit: dict[str, Any] = {}
    for r in audit_rows:
        if r.factory_name:
            latest_audit[r.factory_name] = r

    # 提取失败告警闭环（§3.1）：role 推导增加失败维度。
    # 失败口径与 export_node 的 factories_failed 一致（requirements 全集 −
    # factory_outputs；只有 Approved 工厂才进快照），再排除还会跑的
    # pending/current/暂缓（deferred 等待二遍重试，尚未定论）：
    #   - 最新审计为 factory_manually_removed → manually_removed（整厂人工
    #     剔除，决策 2/阶段四；total_set 即 requirements∩filter 天然满足
    #     「在 requirements 中」，前端渲染「人工剔除」徽章）；
    #   - 在快照里 → done（可 reopen）；
    #   - 不在快照但最新审计 approved（老批次/历史数据无快照）→ done 且
    #     can_reopen=False（前端据此不渲染「重新打开」，堵 reopen 404）；
    #   - 最新审计为 factory_auto_skipped → auto_skipped（缺资料自动跳过，
    #     2026-10-12 需求 1）；
    #   - 最新审计为 factory_skipped → skipped（人工跳过，补上后端从未产出
    #     的该 role，ROLE_MAP 徽章已有定义）；
    #   - 其余（提取失败耗尽重试/占位挂起被驳回）→ failed，
    #     前端显示「提取失败待补录」，不再一律「已完成」。
    outputs = values.get("factory_outputs") or {}
    deferred_names = {
        d.get("factory_name") for d in (values.get("deferred_factories") or [])
    }
    factories = []
    for name in sorted(total_set):
        last = latest_audit.get(name)
        if (last is not None
                and last.result_status == "factory_manually_removed"):
            # 决策 2（阶段四）：整厂人工剔除（最新审计 removed 且在
            # requirements 中）——剔除是操作员最新意图，优先于 done/pending
            role = "manually_removed"
        elif name == current and snap.next:
            role = "current"
        elif name in pending or name in deferred_names:
            # 主队列待处理 / 暂缓待二遍重试：还没定论，不算失败
            role = "pending"
        elif name in outputs:
            role = "done"
        else:
            if last is not None and last.approved:
                role = "done"
            elif last is not None and last.result_status == "factory_auto_skipped":
                role = "auto_skipped"
            elif last is not None and last.result_status == "factory_skipped":
                role = "skipped"
            else:
                role = "failed"
        entry = {
            "factory": name,
            "role": role,
            "session": _load_factory_session(thread_id, name),
        }
        if role == "done" and name not in outputs:
            entry["can_reopen"] = False
        factories.append(entry)

    detail.update({
        "downstream_file_path": values.get("downstream_file_path"),
        "upstream_root": values.get("upstream_root"),
        "final_output_path": values.get("final_output_path"),
        "validation_status": values.get("validation_status"),
        "factories": factories,
        "audit": audit,
        "usage": _usage_with_scope(),
    })

    # 人工保留未覆盖（protected）单元格透出（写入侧人工改动感知 决策 1
    # 通道 C）：前端批次详情页据此渲染「覆盖写入」入口；快照缺失/损坏 → 空。
    try:
        _snap = copy_snapshot.load_snapshot(thread_id)
        _prot = [r for r in (_snap or {}).get("rows") or [] if r.get("protected")]
        detail["manual_protected"] = {
            "count": len(_prot),
            "rows": [{
                "factory": (r.get("key") or {}).get("factory"),
                "sku": (r.get("key") or {}).get("sku"),
                "seq": (r.get("key") or {}).get("seq"),
                "row_no": r.get("row_no"),
                "protected_fields": r.get("protected_fields") or [],
                "name_cn": r.get("name_cn"),
                "net": r.get("net"),
                "gross": r.get("gross"),
            } for r in _prot],
        }
    except Exception as e:  # noqa: BLE001 透出是辅助信息，绝不让详情页 500
        logger.warning("批次详情 manual_protected 透出失败 %s: %s", thread_id, e)
        detail["manual_protected"] = {"count": 0, "rows": []}

    # 决策 4（阶段五）C 态透出：副本缺关键列（管理番号/工厂/SKU）且未纳入
    # 管理的人工新增行，前端批次详情页据此渲染警告条（契约：顶层
    # manual_rows_incomplete = [{row, factory, sku, missing:[...]}]，无则 []）；
    # 活扫失败包死为空数组，绝不让详情页 500
    try:
        detail["manual_rows_incomplete"] = copy_snapshot.scan_incomplete_rows(
            thread_id, values.get("downstream_file_path"))
    except Exception as e:  # noqa: BLE001
        logger.warning("批次详情 manual_rows_incomplete 透出失败 %s: %s",
                       thread_id, e)
        detail["manual_rows_incomplete"] = []

    # 权威状态覆盖（2026-10-12 待补充状态）：checkpoint 三态推导的 completed
    # 无法区分「全部完成」与「缺资料待补充」；以 pipeline_state 自愈后的
    # batches 表状态为准（awaiting_factories / completed_with_errors 等细分
    # 状态直接透出，前端徽章/按钮据此渲染）。覆盖失败保留 checkpoint 三态。
    try:
        from app.orchestrator.pipeline_state import get_pipeline_state
        healed = (get_pipeline_state(thread_id).get("batch") or {}).get("status")
        if healed:
            detail["status"] = healed
    except Exception as e:  # noqa: BLE001
        logger.warning("批次详情权威状态覆盖失败 %s: %s", thread_id, e)
    return detail


def _delete_batch_state(thread_id: str) -> dict[str, Any]:
    """纯删除批次主状态：checkpoints.db 的 checkpoints + writes 行 + batches 业务行。

    不含防护校验、不含审计留痕——调用方各自负责（delete_batch 留
    batch_deleted；看板退回未执行留 batch_reset）。
    batches 行业务行一并删除（内部已兜底：失败只记 warning 不抛出），
    避免已删批次留孤儿行被看板配对成死链接。
    """
    path = Path(get_settings().checkpoint_db_abs).resolve()
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        writes_removed = cur.rowcount
        cur = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        checkpoints_removed = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    batch_store.delete_batch(thread_id)
    return {"checkpoints_removed": checkpoints_removed,
            "writes_removed": writes_removed}


def delete_batch(thread_id: str) -> dict[str, Any]:
    """删除过往批次：清掉 checkpoints.db 里该 thread_id 的全部状态（checkpoints + writes）。

    状态语义（与 _summarize_snapshot 一致）：
      - snap.values 为空 → ValueError（路由转 404）；
      - 有 task.interrupts → pending_review（允许删，废弃场景）；
      - next 非空且无 interrupt → running → RuntimeError（路由转 409）；
      - next 为空 → completed（允许删）。

    删除用独立 rw 连接（不碰 graph 单例 saver 连接，WAL 下并发安全）；
    sessions/*.json、主数据、输出文件一律不碰；batches 表业务行随
    checkpoint 一并删除（否则孤儿行会被监控看板配对成死链接）。
    删除成功后再向 review_audits 插一条 batch_deleted 留痕行
    （既有审计记录一律保留；留痕失败只警告，不反过来搞挂已完成的删除）。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    if not snap.values:
        raise ValueError(f"thread {thread_id} 不存在")
    if not any(t.interrupts for t in snap.tasks) and snap.next:
        raise RuntimeError(f"批次正在运行，禁止删除: {thread_id}")

    # L2 日志关联：删除动作及其审计留痕日志携带批次号
    with logging_context(thread_id=thread_id):
        removed = _delete_batch_state(thread_id)

        # 审计留痕（顺序：先删成功再留痕；失败只警告，不阻塞返回）
        try:
            with get_session() as session:
                session.add(ReviewAudit(
                    thread_id=thread_id,
                    factory_name=None,
                    approved=False,
                    edited_count=0,
                    changes_json="[]",
                    new_skus_json="[]",
                    result_status="batch_deleted",
                ))
                session.commit()
        except Exception as e:  # noqa: BLE001 与 _write_audit 同哲学：留痕失败不阻塞
            logger.warning("⚠️⚠️ [审计落库失败] thread=%s "
                           "批次已删除，但 batch_deleted 留痕写入失败：%s: %s",
                           thread_id, type(e).__name__, e)

        return {
            "deleted": thread_id,
            "checkpoints_removed": removed["checkpoints_removed"],
            "writes_removed": removed["writes_removed"],
        }


def batch_delete_batches(thread_ids: list[str]) -> dict[str, Any]:
    """批量删除批次：逐个调用 delete_batch，部分失败不影响其他。

    返回 {deleted: [str], failed: [{id, reason}]}。
    """
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for tid in thread_ids:
        try:
            delete_batch(tid)
            deleted.append(tid)
        except (ValueError, RuntimeError) as e:
            failed.append({"id": tid, "reason": str(e)})
    return {"deleted": deleted, "failed": failed}


# ---------------------------------------------------------------------------
# 重复处理预检（W4b）：sessions 完成态 + 审核落库记录，四档判定工厂是否已处理
# ---------------------------------------------------------------------------

# 会话完成态（与 extraction/session.py 状态机口径一致）
_SESSION_DONE_STATUSES = ("complete_auto", "complete_manual")
# 会话进行态：提过但没提完，不算已处理，也不算"从没碰过"
_SESSION_PARTIAL_STATUSES = ("collecting", "waiting_pl")


def _session_status_light(
    batch_id: str,
    factory: str,
) -> tuple[str | None, str | None]:
    """轻量读 sessions/{safe(batch_id)}/{factory}.json，只取 (status, updated_at)。

    仅查本批次目录。每个批次独立缓存，跨批次不回看历史。文件不存在/JSON 损坏
    一律静默回落，返回 (None, None)。
    """
    settings = get_settings()
    path = SESSIONS_DIR / settings.safe_path_tag(batch_id) / f"{factory}.json"
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    return data.get("status"), data.get("updated_at")


def check_processed_factories(
    thread_id: str,                                  # ← 新增必填
    downstream_file_path: str | None = None,
    factory_names: list[str] | None = None,
) -> dict[str, Any]:
    """四档预检（仅查本批次）。

    工厂集合：给出 factory_names 时直接用之（不解析文件，调用方已知名单）；
    缺省时解析装箱单（parse_requirements），解析失败抛 ValueError（路由层
    转 422）。

    level 四档：
      - audited：本批次 review_audits 有 approved=true
      - session_complete：本批次 sessions/{batch_id_safe}/{factory}.json 为 complete_*
      - partial：collecting / waiting_pl
      - none：无任何记录
    单工厂查询异常降级 level="none"，不拖垮整表。
    """
    if factory_names is not None:
        names = list(dict.fromkeys(factory_names))  # 保序去重
    else:
        from app.nodes.parse_downstream import parse_requirements

        downstream = downstream_file_path or get_settings().downstream_file_path
        try:
            requirements, _ = parse_requirements(str(Path(downstream).expanduser()))
        except Exception as e:  # noqa: BLE001 统一转 ValueError，调用方转 422
            raise ValueError(
                f"装箱单解析失败，无法预检重复处理: {type(e).__name__}: {e}"
            ) from e
        names = list(requirements.keys())

    # 审核落库记录：一次性查全部工厂的 approved 记录（ts 倒序），逐厂取首条
    last_audit: dict[str, dict[str, Any]] = {}
    if names:
        try:
            with get_session() as session:
                rows = session.scalars(
                    select(ReviewAudit)
                    .where(ReviewAudit.factory_name.in_(names),
                           ReviewAudit.thread_id == thread_id,             # ← 新增：限本批次
                           ReviewAudit.approved.is_(True))
                    .order_by(ReviewAudit.ts.desc())
                ).all()
            for r in rows:
                if r.factory_name in last_audit:
                    continue
                last_audit[r.factory_name] = {
                    "ts": r.ts.isoformat() if r.ts else None,
                    "thread_id": r.thread_id,
                    "result_status": r.result_status,
                }
        except Exception as e:  # noqa: BLE001 审计查询失败按无审核记录处理
            logger.warning("⚠️ [预检] review_audits 查询失败，按无审核记录处理：%s: %s",
                           type(e).__name__, e)

    factories: list[dict[str, Any]] = []
    processed_count = 0
    for name in names:
        try:
            status, updated_at = _session_status_light(thread_id, name)
            audit = last_audit.get(name)
            if audit is not None:
                level = "audited"
            elif status in _SESSION_DONE_STATUSES:
                level = "session_complete"
            elif status in _SESSION_PARTIAL_STATUSES:
                level = "partial"
            else:
                level = "none"
            processed = level in ("audited", "session_complete")
            if processed:
                processed_count += 1
            factories.append({
                "factory": name,
                "processed": processed,
                "session_status": status,
                "session_updated_at": updated_at,
                "last_audit": audit,
                "level": level,
            })
        except Exception as e:  # noqa: BLE001 单厂异常降级 none，不拖垮整表
            logger.warning("⚠️ [预检] 工厂 %s 查询异常，按未处理降级：%s: %s",
                           name, type(e).__name__, e)
            factories.append({
                "factory": name,
                "processed": False,
                "session_status": None,
                "session_updated_at": None,
                "last_audit": None,
                "level": "none",
            })

    return {
        "factories": factories,
        "processed_count": processed_count,
        "total_count": len(factories),
    }


def _watch_folder_of(*paths: Path) -> tuple[str, str] | None:
    """路径落在监控目录某一级子文件夹内时返回 (watch_dir, folder_name)，否则 None。

    建批时把「监控目录文件夹 ↔ 批次」显式写进 batches 行——手动建批与
    扫描建批同口径，看板配对不再只靠路径归属兜底。任何异常静默返回
    None：关联是体验优化，绝不阻塞建批。
    """
    try:
        watch_str = get_settings().watch_dir
        if not watch_str:
            return None
        watch = Path(watch_str).expanduser().resolve()
        if not watch.is_dir():
            return None
        for p in paths:
            try:
                rp = Path(p).expanduser().resolve()
            except OSError:
                continue
            if rp == watch or watch not in rp.parents:
                continue
            rel = rp.relative_to(watch)
            folder = watch / rel.parts[0]
            if folder.is_dir():
                return str(watch), rel.parts[0]
        return None
    except Exception:  # noqa: BLE001
        return None


def create_batch(
    thread_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
    factory_alias_overrides: dict[str, str] | None = None,
    skip_processed: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """发起新批次：thread_id 查重 + 路径存在性校验后复用 run_until_interrupt。

    - thread_id 空白 → ValueError；
    - checkpoints 已有同名 thread → FileExistsError（路由层转 409）；
    - 路径缺省取 settings；downstream 必须是文件、upstream 必须是目录
      （Path(p).expanduser() 兼容 Windows 盘符路径），否则 ValueError
      且消息指明哪个路径不存在（路由层转 422）；
    - factory_filter 只处理指定工厂（调试/冒烟/跳过已处理用），透传
      run_until_interrupt，None=全部工厂；
    - factory_alias_overrides 批次级「仅本次生效」工厂名对照（W5），
      非空才写入 state（Node2 最高优先匹配档，不落盘）；
    - skip_processed=True 时先跑 check_processed_factories 预检（W4b），
      取「未处理工厂」差集作 factory_filter（与显式 factory_filter 互斥，
      factory_filter 优先）；差集为空时不跑图，直接返回 skipped_all
      （规避 Node2 空队列假审核包）。每次调用实时重算差集，不沿用任何
      预览结论。
    """
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id 不能为空")

    with contextlib.closing(_open_checkpoint_ro()) as conn:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
    if row:
        raise FileExistsError(f"批次已存在，请换一个 thread_id: {thread_id}")

    settings = get_settings()
    downstream = downstream_file_path or settings.downstream_file_path
    upstream = upstream_root or settings.upstream_root

    d_path = Path(downstream).expanduser()
    if not d_path.is_file():
        raise ValueError(f"下游装箱单路径不存在或不是文件: {downstream}")
    u_path = Path(upstream).expanduser()
    if not u_path.is_dir():
        raise ValueError(f"上游工厂文件夹路径不存在或不是目录: {upstream}")
    # 上游目录与装箱单工厂的匹配预扫（用 W5 prescan 干跑一遍）。
    # 2026-10-12 起放宽：一家都匹配不到不再拒绝建批——全部工厂按缺资料
    # 自动跳过，批次跑完进「待补充」（awaiting_factories），操作员补文件夹
    # 后经「补充工厂」/监控自动补齐；只记 warning 放行。
    # 测试场景（空 tmpdir）可设 FACTORY_DIR_STRICT=0 跳过整个预扫。
    if os.environ.get("FACTORY_DIR_STRICT", "1") == "1":
        try:
            _scan = prescan_factory_aliases(
                downstream_file_path=str(d_path), upstream_root=str(u_path))
            _resolved = _scan.get("resolved") or {}
            _candidates = _scan.get("candidates") or {}
            _unmatched = _scan.get("unmatched") or []
            _total = len(_resolved) + len(_candidates) + len(_unmatched)
            if _total > 0 and not _resolved:
                logger.warning(
                    "上游路径下未匹配到任何工厂文件夹: %s"
                    "（全部工厂将按缺资料自动跳过，批次进待补充）", upstream)
            # 2026-10-12 用户确认（调整 B）：建批时自动为 prescan 未匹配
            # 工厂建空文件夹（与看板预建同一命名口径），操作员按标准名放
            # 单据后监控/补充工厂接手；幂等，单个失败只 warning 不阻断建批
            if _unmatched:
                _auto_create_factory_folders(u_path, _unmatched,
                                             check_folder_match=False)
        except Exception:
            # 装箱单解析失败 / W5 异常 → 软警告，不阻断（交给 dispatcher preview 兜底）
            pass

    # W4b：skip_processed 实时重算差集（不沿用预览结论，防 TTL 内竞态）；
    # 显式 factory_filter 优先（互斥语义）
    effective_filter = factory_filter
    skipped: list[str] = []
    if skip_processed and not factory_filter:
        precheck = check_processed_factories(
            thread_id=thread_id,                              # ← 新增
            downstream_file_path=str(d_path),
        )
        skipped = [f["factory"] for f in precheck["factories"] if f["processed"]]
        remaining = [f["factory"] for f in precheck["factories"]
                     if not f["processed"]]
        if not remaining:
            logger.info("[W4b] 批次 %s 全部工厂均已处理，跳过建图", thread_id)
            return {
                "status": "skipped_all",
                "message": "全部工厂均已处理，无可提取工厂",
                "processed": skipped,
            }
        effective_filter = remaining

    result = run_until_interrupt(thread_id, str(d_path), str(u_path),
                                 factory_filter=effective_filter,
                                 factory_alias_overrides=factory_alias_overrides,
                                 on_progress=on_progress)
    if skipped:
        result["skipped_processed"] = skipped

    # 写入批次配置
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    batch_config = {
        "thread_id": thread_id,
        "downstream_file_path": str(d_path),
        "upstream_root": str(u_path),
        "created_at": now,
        "last_run_at": now,
        "run_count": 1,
    }
    _write_batch_config(thread_id, batch_config)

    # 决策 9：建批即记录原件指纹基线（mtime_ns+size+数量基线，
    # 供补充工厂/单厂重试/reopen 三入口的原件差异检测短路比对）
    source_diff.record_fingerprint(thread_id, str(d_path))

    # 同步 batches 业务表（端到端升级新增）；路径落进监控目录子文件夹时
    # 顺带写 folder_name/watch_dir 显式关联（与扫描建批同口径）
    assoc = _watch_folder_of(u_path, d_path)
    batch_store.upsert_batch(
        thread_id,
        downstream_file_path=str(d_path),
        upstream_root=str(u_path),
        status="pending_review" if result.get("status") == "pending_human_review" else "running",
        **({"watch_dir": assoc[0], "folder_name": assoc[1]} if assoc else {}),
    )
    return result


# ---------------------------------------------------------------------------
# 端到端批次发现与启动（Release 1）
# ---------------------------------------------------------------------------

def scan_new_batches() -> list[dict[str, Any]]:
    """扫描监控目录返回候选批次列表。"""
    return discovery.scan_new_batches()


def start_batch_from_scan(
    folder_name: str,
    thread_id: str | None = None,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """从扫描候选中启动一个批次。

    - 默认用 folder_name 作为 thread_id；
    - 默认在监控目录 folder_name 下查找 downstream 文件；
    - 默认按装箱单实际所在目录推断 upstream_root（其下有「工厂」子目录则取它）；
    - on_progress：节点级进度回调，透传 create_batch（对话确认门执行时
      前端才有「正在提取工厂X」级进度，否则全程只有「正在执行…」）。
    """
    settings = get_settings()
    watch_dir = Path(settings.watch_dir).expanduser()
    if not watch_dir.is_dir():
        raise ValueError(f"监控目录未配置或不存在: {settings.watch_dir}")

    subfolder = watch_dir / folder_name
    if not subfolder.is_dir():
        raise ValueError(f"子文件夹不存在: {subfolder}")

    # 下游表：用户显式指定 > 自动匹配 > 报错
    downstream = downstream_file_path
    if not downstream:
        candidates = discovery.discover_downstream_files(subfolder)
        if len(candidates) == 1:
            downstream = str(candidates[0])
        elif len(candidates) > 1:
            raise ValueError(
                f"发现多个下游装箱单，请指定：{[p.name for p in candidates]}"
            )
        else:
            raise ValueError(f"子文件夹 {folder_name} 中未找到 ContentsOfTheContainer 文件")

    # 上游根：用户显式指定 > 按装箱单实际所在目录推断（自动探测与显式
    # 选择 downstream_file_path 两条来源统一口径——装箱单常落在嵌套中间层
    # XD…/93/ 下，与「工厂」目录同层，不能退回批次子文件夹找）；
    # pick_upstream_root 再按「工厂」子目录约定收敛（存在即取它）
    if upstream_root:
        upstream = upstream_root
    else:
        upstream = str(discovery.pick_upstream_root(Path(downstream).parent))

    tid = (thread_id or folder_name).strip()
    if not tid:
        raise ValueError("thread_id 不能为空")

    result = create_batch(tid, downstream, upstream, on_progress=on_progress)
    batch_store.upsert_batch(
        tid,
        watch_dir=str(watch_dir),
        folder_name=folder_name,
        downstream_file_path=downstream,
        upstream_root=upstream,
        status="pending_review" if result.get("status") == "pending_human_review" else "running",
    )
    return result


def undo_alias_write(factory_name: str, folder_name: str) -> bool:
    """撤销 prepare_factory_folders 自动写入的别名。"""
    return factory_setup.undo_alias_write(factory_name, folder_name)


# ---------------------------------------------------------------------------
# 工厂名对照预扫（W5）：发起批次前一次性把装箱单工厂分三档，供确认门展示
# ---------------------------------------------------------------------------

# 「确定命中」分档线：alias/alias_ci/exact 记 100 直接确定；
# fuzzy 命中须 >= 85 才算确定，否则降级为「低置信推荐」
_PRESCAN_CERTAIN_SCORE = 85.0


def prescan_factory_aliases(
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
) -> dict[str, Any]:
    """工厂名对照预扫：解析装箱单工厂集合，逐个匹配上游一级子目录，分三档返回。

    返回：
      - resolved:   {工厂: {"folder", "score", "method"}} 确定命中
                    （alias/alias_ci/exact，或 fuzzy 得分 >= 85）；
      - candidates: {工厂: [{"folder", "score", "signals"}]} 低置信推荐
                    （fuzzy 40~85 或包含信号），需人工确认；
      - unmatched:  [工厂, ...] 无任何候选，需人工指定；
      - warnings:   [str, ...] 非致命问题。

    缺省参数取 settings。装箱单解析失败 / 目录不可读只进 warnings 不抛出；
    目录不存在时 warnings 且全部工厂进 unmatched。
    """
    from app.factory_match import (
        load_alias_map, load_folder_match_candidates,
        match_factory_folder, recommend_candidates)
    from app.nodes.parse_downstream import parse_requirements

    settings = get_settings()
    downstream = downstream_file_path or settings.downstream_file_path
    upstream = upstream_root or settings.upstream_root

    result: dict[str, Any] = {
        "resolved": {}, "candidates": {}, "unmatched": [], "warnings": []}

    try:
        requirements, _ = parse_requirements(str(Path(downstream).expanduser()))
    except Exception as e:  # noqa: BLE001 预扫是辅助设施，解析失败不抛出
        result["warnings"].append(
            f"装箱单解析失败，无法预扫工厂对照: {type(e).__name__}: {e}")
        return result

    factories = list(requirements.keys())
    if factory_filter:
        allow = set(factory_filter)
        factories = [f for f in factories if f in allow]

    u_path = Path(upstream).expanduser()
    if not u_path.is_dir():
        result["warnings"].append(f"上游工厂文件夹不存在或不是目录: {upstream}")
        result["unmatched"] = factories
        return result
    try:
        folders = [d.name for d in u_path.iterdir() if d.is_dir()]
    except OSError as e:
        result["warnings"].append(f"上游工厂文件夹不可读: {e}")
        result["unmatched"] = factories
        return result

    alias_map = load_alias_map()
    folder_candidates = load_folder_match_candidates()
    cutoff = settings.fuzzy_match_score_cutoff
    for factory in factories:
        folder, score, method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff,
            folder_candidates=folder_candidates)
        if folder and (method != "fuzzy" or score >= _PRESCAN_CERTAIN_SCORE):
            result["resolved"][factory] = {
                "folder": folder, "score": score, "method": method}
            continue
        candidates = recommend_candidates(factory, folders, cutoff=cutoff)
        if candidates:
            result["candidates"][factory] = candidates
        else:
            result["unmatched"].append(factory)
    return result


# ---------------------------------------------------------------------------
# 审核审计：resume 前抓取原始 payload 做 diff 快照，成功后落 review_audits
# ---------------------------------------------------------------------------

# 人工可修改的底层数值字段（None 与数值严格区分：None 表示未提交/未改动）
_AUDIT_NUMERIC_FIELDS = ("total_quantity", "total_net_weight", "total_gross_weight")


def _prepare_audit(thread_id: str, resume_data: dict) -> dict[str, Any] | None:
    """resume 前准备审计快照：diff 提交 items vs 原始 payload items。

    - changes：数值字段（_AUDIT_NUMERIC_FIELDS）old→new 对照，
      None 与数值严格区分（提交值 None 视为未改动，不计 diff）；
    - new_skus：原始 payload 中 is_new_sku 项的人工补录字段
      （name_cn/hs_code/inspection_required/name_en/name_jp）；
    - 原始 payload 拿不到（未挂起等）返回 None。
    """
    payload = get_review_payload(thread_id)
    if payload is None:
        return None

    orig_items = {i.get("sku"): i for i in payload.get("items") or []}
    changes: list[dict[str, Any]] = []
    new_skus: list[dict[str, Any]] = []
    edited_count = 0

    for item in (resume_data or {}).get("items") or []:
        sku = item.get("sku")

        # 人工删除条目（审核页「删除条目」）：扁平结构留痕，与其他 change 同构
        if item.get("deleted"):
            changes.append({"sku": item.get("orig_sku") or sku,
                            "field": "条目", "old": "识别条目", "new": "人工删除"})
            edited_count += 1
            continue

        orig = orig_items.get(sku) or {}
        orig_ext = orig.get("extracted_data") or {}
        new_ext = item.get("extracted_data") or {}

        sku_changed = False
        for f in _AUDIT_NUMERIC_FIELDS:
            new_v = new_ext.get(f)
            if new_v is not None and new_v != orig_ext.get(f):
                sku_changed = True
                # 扁平结构（冻结契约）：batch.html 审计区按 c.field/c.old/c.new 渲染
                changes.append({"sku": sku, "field": f, "old": orig_ext.get(f), "new": new_v})
        if sku_changed:
            edited_count += 1

        # 提交时勾选「更新历史单重」：扁平结构留痕（即使数值未人工改动也记，
        # 便于事后追查历史基准何时被谁刷新）
        if item.get("update_history_weight"):
            changes.append({
                "sku": sku, "field": "历史单重",
                "old": ((orig.get("db_record") or {}).get("unit_net_weight")),
                "new": ((item.get("calculation") or {})
                        .get("calculated_unit_net")),
            })
            if not sku_changed:
                edited_count += 1

        if orig.get("is_new_sku"):
            new_skus.append({
                "sku": sku,
                "name_cn": item.get("name_cn"),
                "hs_code": item.get("hs_code"),
                "inspection_required": item.get("inspection_required"),
                "name_en": item.get("name_en"),
                "name_jp": item.get("name_jp"),
            })

    return {
        "thread_id": thread_id,
        "factory_name": payload.get("factory_name"),
        "approved": bool((resume_data or {}).get("approved", False)),
        # 「跳过本工厂」标记：resume_order 据此把 result_status 落为 factory_skipped
        # （approved 保持 False，天然不误计入"已处理"档位）
        "skipped": bool((resume_data or {}).get("skipped")),
        "edited_count": edited_count,
        "changes": changes,
        "new_skus": new_skus,
    }


def _audit_result_status(prepared: dict[str, Any] | None,
                         result: dict[str, Any]) -> str | None:
    """resume 审计的 result_status：「跳过本工厂」固化为 factory_skipped
    （approved=False + result_status=factory_skipped 是批次详情页 skipped 徽章、
    以及后续「补充工厂」判定的权威留痕）；其余沿用图执行结果。"""
    if prepared and prepared.get("skipped"):
        return "factory_skipped"
    return result.get("status")


def _write_audit(prepared: dict[str, Any] | None, result_status: str | None) -> None:
    """把审计快照写入 review_audits。

    审计是辅助设施，不能反过来搞挂已成功的 resume：本函数 try/except
    包死，任何失败只记警告日志，绝不向外抛出。
    """
    if not prepared:
        return
    try:
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=prepared["thread_id"],
                factory_name=prepared.get("factory_name"),
                approved=prepared.get("approved", False),
                edited_count=prepared.get("edited_count", 0),
                changes_json=json.dumps(prepared.get("changes") or [], ensure_ascii=False),
                new_skus_json=json.dumps(prepared.get("new_skus") or [], ensure_ascii=False),
                result_status=result_status,
            ))
            session.commit()
    except Exception as e:  # noqa: BLE001 故意包死，见 docstring
        logger.warning("⚠️⚠️ [审计落库失败] thread=%s "
                       "resume 已成功，但 review_audits 写入失败：%s: %s",
                       prepared.get('thread_id'), type(e).__name__, e)
