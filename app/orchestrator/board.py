# -*- coding: utf-8 -*-
"""监控目录看板服务。

把「监控目录总览」从调度 Agent 对话能力提为常驻可视化看板（/board）：
- board_state：三档分类（已完成/执行中/未执行候选），复用
  discovery.match_watch_folders 三重匹配；未完成批次顺带借
  get_pipeline_state 以 checkpoint 自愈校正滞留状态，并附工厂进度；
- mark_done：未执行候选 → 已完成（历史文件夹标记，扫描不再列出）；
- start_from_board：看板一键启动批次——确认动作发生在看板弹窗
  （一次一确认的确认门由前端弹窗承担），这里预写 running 批次行后
  后台线程跑 start_batch_from_scan，HTTP 立即返回不阻塞；
- reset_to_todo：退回未执行——把文件夹关联的批次从执行中(挂起/异常)/
  已完成回退到未执行，清掉全部执行痕迹，看板重新把它列为候选。
- folder_plan / prepare_folders：预建工厂文件夹（提取失败告警闭环 §3.4，
  源头预防 no_folder_matched）——解析装箱单工厂名单，与上游「工厂」目录
  现有子目录 diff（factory_aliases 同口径），确认后批量 mkdir。

所有路径处理使用 pathlib.Path，兼容 macOS/Windows。
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import batch_store
from app.orchestrator import discovery

logger = logging.getLogger(__name__)

# running 无 checkpoint 的宽容期（秒）：覆盖正常启动到首个 checkpoint 的
# 耗时 + 跨进程竞态缓冲；超过此期且后台线程已死才按僵尸行标 error
STALE_RUNNING_SECONDS = 600


def _has_checkpoint(thread_id: str) -> bool:
    """checkpoints.db 里是否有该批次的执行态（决定详情/对话是否可看）。

    DB/表不存在 → False（全新部署即无执行态）；其他异常 → 记 warning
    返回 True（基础设施故障时宁可按钮可用，不误锁）。
    """
    try:
        path = Path(get_settings().checkpoint_db_abs).resolve()
        if not path.exists():
            return False
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (thread_id,))
            return cur.fetchone() is not None
        except sqlite3.OperationalError:
            return False  # 表未建 = 无任何执行态
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("检查 checkpoint 失败 | thread_id=%s", thread_id,
                       exc_info=True)
        return True


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
    - 有 checkpoint 的行逐批 get_pipeline_state（非 completed 顺带自愈回写
      滞留状态），附 current_phase、工厂进度 done/total，以及卡片操作字段
      final_output_path / split_thread_id / declarations_ready；
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
        if rec is not None:
            rec["_has_checkpoint"] = _has_checkpoint(rec["thread_id"])
        if (rec is not None and rec.get("status") == "running"
                and rec.get("_has_checkpoint") is False):
            # 僵尸行自愈：running 但无 checkpoint——后台启动线程已死且
            # 行龄超宽容期，说明服务重启/线程异常退出，标 error 留信息
            alive = any(
                t.name == f"board-start-{rec['thread_id']}" and t.is_alive()
                for t in threading.enumerate())
            if not alive:
                stale = True
                updated_raw = rec.get("updated_at")
                try:
                    updated = datetime.fromisoformat(updated_raw)
                    age = (datetime.utcnow() - updated).total_seconds()
                    stale = age >= STALE_RUNNING_SECONDS
                except (TypeError, ValueError):
                    stale = True  # 解析失败视为超龄，宁可标错
                if stale and batch_store.mark_error(
                        rec["thread_id"],
                        "任务异常中断：服务可能重启或后台线程已退出，"
                        "可退回未执行后重新启动"):
                    logger.info(
                        "看板僵尸行自愈 | thread_id=%s | 标为 error",
                        rec["thread_id"])
                    # 重新取行以带出 error_message/error_at，并保持
                    # matched 引用一致；_has_checkpoint 是本地附加键需补回
                    rec = batch_store.get_batch(rec["thread_id"]) or rec
                    rec["_has_checkpoint"] = False
                    matched[child.name] = rec
        if rec is not None and rec["_has_checkpoint"]:
            # 以 checkpoint 为权威源取执行态：非 completed 行顺带自愈回写
            # 滞留状态；所有活批次透传卡片操作字段（输出路径/分票信息）。
            # 无 checkpoint 的残留行跳过——没有可推导的执行态，自愈只会
            # 把它误标成 running
            try:
                from app.orchestrator.pipeline_state import get_pipeline_state
                state = get_pipeline_state(rec["thread_id"])
                if rec.get("status") not in ("completed", "completed_with_errors"):
                    fresh = state.get("batch") or {}
                    if fresh.get("status") and fresh["status"] != rec.get("status"):
                        rec = batch_store.get_batch(rec["thread_id"]) or fresh
                        matched[child.name] = rec
                extract = state.get("extract") or {}
                split = state.get("split") or {}
                rec["_phase"] = state.get("current_phase")
                rec["_done_factories"] = len(extract.get("done_factories") or [])
                rec["_pending_factories"] = len(extract.get("pending_factories") or [])
                rec["_current_factory"] = extract.get("current_factory")
                rec["_factories_failed"] = extract.get("factories_failed") or 0
                rec["_final_output_path"] = extract.get("final_output_path")
                rec["_split_thread_id"] = split.get("split_thread_id")
                rec["_declarations_ready"] = split.get("declarations_ready", False)
            except Exception:  # noqa: BLE001 自愈/取数失败不阻塞看板
                pass
        # completed_with_errors 仍归「已完成」档（橙色徽章 + 失败工厂计数警示）
        if rec is not None and rec.get("status") in ("completed", "completed_with_errors"):
            done.append({"folder_name": child.name,
                         "thread_id": rec["thread_id"],
                         "status": rec.get("status"),
                         "completed_at": rec.get("completed_at"),
                         "factories_failed": rec.get("_factories_failed", 0),
                         "has_checkpoint": rec.get("_has_checkpoint", True),
                         "final_output_path": rec.get("_final_output_path"),
                         "split_thread_id": rec.get("_split_thread_id"),
                         "declarations_ready": rec.get("_declarations_ready", False)})
        elif rec is not None:
            in_progress.append({
                "folder_name": child.name,
                "thread_id": rec["thread_id"],
                "status": rec.get("status"),
                "current_phase": rec.get("_phase"),
                "done_factories": rec.get("_done_factories", 0),
                "pending_factories": rec.get("_pending_factories", 0),
                "current_factory": rec.get("_current_factory"),
                "has_checkpoint": rec.get("_has_checkpoint", True),
                "final_output_path": rec.get("_final_output_path"),
                "split_thread_id": rec.get("_split_thread_id"),
                "declarations_ready": rec.get("_declarations_ready", False),
                "error_message": rec.get("error_message"),
                "error_at": rec.get("error_at"),
                "updated_at": rec.get("updated_at"),
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


def mark_done(folder_name: str, thread_id: str | None = None) -> dict[str, Any]:
    """未执行候选 → 已完成；可选关联到已有已完成批次。

    thread_id 缺省：纯标记——只写 batches 表合成行（thread_id=文件夹名
    原文，与 mark_batch_done 工具/扫描去重同口径），不动文件夹；这类
    记录无 checkpoint，看板「详情」按钮置灰。

    thread_id 提供：绑定——把 folder_name/watch_dir 补写到该已有批次行
    （status/completed_at 不动），之后该文件夹的详情/对话都落到这个
    真批次上。校验：批次不存在 → FileNotFoundError；批次未完成 →
    ValueError；批次已绑定别的文件夹 → FileExistsError。

    查重统一用 match_watch_folders 三重匹配——手动建批（thread_id 与
    文件夹名无关）的文件夹也不能被重复标记。
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
        if existing.get("status") in ("completed", "completed_with_errors"):
            return {"ok": True, "linked": False,
                    "message": f"「{folder_name}」已是已完成状态",
                    "thread_id": existing["thread_id"]}
        raise FileExistsError(
            f"「{folder_name}」已有批次记录（{existing['thread_id']}，"
            f"状态 {existing.get('status')}），不能标记为已完成")

    tid = (thread_id or "").strip()
    if tid:
        target = batch_store.get_batch(tid)
        if target is None:
            raise FileNotFoundError(f"批次不存在: {tid}")
        if target.get("status") not in ("completed", "completed_with_errors"):
            raise ValueError(f"只能关联已完成批次（{tid} 当前状态 "
                             f"{target.get('status')}）")
        bound = target.get("folder_name")
        if bound and bound != folder_name:
            raise FileExistsError(
                f"批次 {tid} 已关联文件夹「{bound}」，不能再绑定「{folder_name}」")
        batch_store.upsert_batch(tid, watch_dir=str(watch),
                                 folder_name=folder_name)
        logger.info("看板标记完成并关联批次 | folder=%s | thread_id=%s",
                    folder_name, tid)
        return {"ok": True, "linked": True, "thread_id": tid,
                "message": f"已把「{folder_name}」关联到批次 {tid}，"
                           f"详情/对话将跳转到该批次"}

    batch_store.upsert_batch(
        folder_name,
        watch_dir=str(watch),
        folder_name=folder_name,
        status="completed",
    )
    batch_store.update_status(folder_name, "completed")  # 填充 completed_at
    logger.info("看板标记完成 | folder=%s", folder_name)
    return {"ok": True, "linked": False,
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
        except Exception as e:  # noqa: BLE001 后台线程异常不抛出，落状态 + 日志
            logger.exception("看板启动批次失败 | thread_id=%s", tid)
            batch_store.mark_error(tid, f"{type(e).__name__}: {e}"[:500])

    threading.Thread(target=_run, daemon=True,
                     name=f"board-start-{tid}").start()
    logger.info("看板启动批次 | folder=%s | thread_id=%s", folder_name, tid)
    return {"ok": True, "thread_id": tid,
            "message": f"批次 {tid} 已启动，可在执行中卡片打开对话跟踪进度"}


def _pre_extraction_alive(tid: str) -> bool:
    """后台预提取是否仍在跑：进程级已知集合 + 线程名双重判定。"""
    from app.api import service
    if tid in service._known_running:
        return True
    return any(t.name == f"pre-extract-{tid}" and t.is_alive()
               for t in threading.enumerate())


def reset_to_todo(folder_name: str) -> dict[str, Any]:
    """退回未执行：把文件夹关联的批次回退到未执行，清掉全部执行痕迹。

    与 mark_done 对称的逆向操作。完成后看板重新把文件夹列为未执行候选，
    可再次一键启动。

    两条路径：
    - 合成行（mark_done 纯标记，无 checkpoint）：只删 batches 业务行；
    - 真批次：防护校验 → 清分票 checkpoint → 清提取缓存 → 清审核记录
      （插 batch_reset 留痕）→ 解绑调度会话 → 删批次配置文件 →
      删主状态（checkpoints/writes/batches 行）。

    清理顺序铁律：主状态删除之前任何一步失败都抛出（不留下半清理状态）；
    留痕失败只 warning 不阻塞（与 delete_batch 同哲学）。

    异常契约（路由层转 HTTP）：
    - 文件夹不存在 → FileNotFoundError；
    - 文件夹无关联批次（已是未执行）→ ValueError；
    - 批次活跑中（图 next 非空且无 interrupt）→ RuntimeError；
    - 后台预提取线程仍在运行 → RuntimeError（消息含「预识别」）。
    """
    folder_name = (folder_name or "").strip()
    if not folder_name:
        raise ValueError("文件夹名不能为空")
    watch = _watch_path()
    folder = watch / folder_name
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder_name}")

    # 关联批次查找：与看板同源的三重匹配；无匹配 = 已是未执行
    matched = discovery.match_watch_folders(watch)
    rec = matched.get(folder_name)
    if rec is None:
        raise ValueError(f"「{folder_name}」已是未执行状态，无需退回")
    tid = rec["thread_id"]

    from app.api import service

    # 合成行：无 checkpoint 的纯标记/残留行，删 batches 行即完成回退；
    # 删前留一条 batch_reset 审计痕（留痕失败只 warning 不阻塞）
    if not service.get_order_state(tid).get("exists"):
        batch_store.delete_batch(tid)
        try:
            from app.db.models import ReviewAudit
            from app.db.session import get_session
            with get_session() as session:
                session.add(ReviewAudit(
                    thread_id=tid,
                    factory_name=None,
                    approved=False,
                    edited_count=0,
                    changes_json=json.dumps(
                        [{"folder_name": folder_name,
                          "error_message": rec.get("error_message")}],
                        ensure_ascii=False),
                    new_skus_json="[]",
                    result_status="batch_reset",
                ))
                session.commit()
        except Exception as e:  # noqa: BLE001 与 delete_batch 同哲学：留痕失败不阻塞
            logger.warning("⚠️⚠️ [审计落库失败] thread=%s "
                           "批次已退回，但 batch_reset 留痕写入失败：%s: %s",
                           tid, type(e).__name__, e)
        logger.info("看板退回未执行（合成行） | folder=%s | thread_id=%s",
                    folder_name, tid)
        return {"ok": True, "thread_id": tid,
                "message": f"已把「{folder_name}」退回未执行（清除了完成标记），"
                           f"可重新开始"}

    # ---- 防护（与 service.delete_batch 同规则）----
    graph = service.get_graph()
    snap = graph.get_state(service._config(tid))
    if not any(t.interrupts for t in snap.tasks) and snap.next:
        raise RuntimeError(f"批次正在运行，禁止退回: {tid}")
    if _pre_extraction_alive(tid):
        raise RuntimeError("后台预识别仍在运行，请稍后重试")

    cleaned: list[str] = []
    settings = get_settings()

    # ---- ① 清分票状态：同一 checkpoints.db 中 split-{tid} 的
    # checkpoints + writes 行（独立 rw 连接，不存在则跳过）----
    split_tid = f"split-{tid}"
    ckpt_path = Path(settings.checkpoint_db_abs).resolve()
    if ckpt_path.exists():
        conn = sqlite3.connect(str(ckpt_path))
        try:
            cur = conn.execute("DELETE FROM writes WHERE thread_id = ?",
                               (split_tid,))
            w = cur.rowcount
            cur = conn.execute("DELETE FROM checkpoints WHERE thread_id = ?",
                               (split_tid,))
            c = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if w or c:
            cleaned.append("分票记录")

    # ---- ② 清提取缓存：批次 session 目录 + 预提取进度文件
    # （不清则重跑命中旧缓存，跳过重新提取）----
    batch_session_dir = service.SESSIONS_DIR / settings.safe_path_tag(tid)
    if batch_session_dir.is_dir():
        shutil.rmtree(batch_session_dir)
        cleaned.append("提取缓存")
    progress_path = service._preextract_progress_path(tid)
    if progress_path.is_file():
        progress_path.unlink()
        cleaned.append("预识别进度")

    # ---- ③ 清审核记录 + 插 batch_reset 留痕
    # （不清则重跑时已审核工厂被 audited 档全部 skip，批次空跑）----
    from app.db.models import ChatSession, ReviewAudit
    from app.db.session import get_session
    with get_session() as session:
        removed = (session.query(ReviewAudit)
                   .filter(ReviewAudit.thread_id == tid)
                   .delete(synchronize_session=False))
        session.commit()
    if removed:
        cleaned.append("审核记录")
    try:
        with get_session() as session:
            session.add(ReviewAudit(
                thread_id=tid,
                factory_name=None,
                approved=False,
                edited_count=0,
                changes_json="[]",
                new_skus_json="[]",
                result_status="batch_reset",
            ))
            session.commit()
    except Exception as e:  # noqa: BLE001 与 delete_batch 同哲学：留痕失败不阻塞
        logger.warning("⚠️⚠️ [审计落库失败] thread=%s "
                       "批次已退回，但 batch_reset 留痕写入失败：%s: %s",
                       tid, type(e).__name__, e)

    # ---- ④ 解绑调度会话：pinned_thread_id 置 NULL（会话与历史保留）----
    with get_session() as session:
        unpinned = (session.query(ChatSession)
                    .filter(ChatSession.pinned_thread_id == tid)
                    .update({"pinned_thread_id": None},
                            synchronize_session=False))
        session.commit()
    if unpinned:
        cleaned.append("会话绑定")

    # ---- ⑤ 删批次配置文件（output/{tid}/batch_config.json；产物目录不动）----
    config_path = settings.batch_output_dir(tid) / "batch_config.json"
    if config_path.is_file():
        config_path.unlink()
        cleaned.append("批次配置")

    # ---- ⑥ 删主状态：checkpoints/writes/batches 行（纯删除，不留痕，
    # 本函数已在 ③ 留过 batch_reset，避免 batch_deleted 双留痕）----
    service._delete_batch_state(tid)
    cleaned.append("执行状态")

    logger.info("看板退回未执行 | folder=%s | thread_id=%s | 清理=%s",
                folder_name, tid, "/".join(cleaned))
    detail = ("、".join(cleaned)) if cleaned else "批次标记"
    return {"ok": True, "thread_id": tid,
            "message": f"已把「{folder_name}」退回未执行"
                       f"（清理了{detail}），可重新开始"}


# ---------------------------------------------------------------------------
# 预建工厂文件夹（提取失败告警闭环 §3.4）：源头预防 no_folder_matched
# ---------------------------------------------------------------------------

# diff「已有」判定只认确定性匹配档（与 Node2/prescan 同源）；fuzzy/contains
# 是概率猜测，照常按命名规则预建规范文件夹，避免「猜中的旧文件夹」与
# 「新建的规范文件夹」并存后匹配口径漂移
_PLAN_EXISTING_METHODS = ("alias", "alias_ci", "exact", "alias_folder")


def folder_plan(
    folder_name: str,
    downstream_file_path: str | None = None,
) -> dict[str, Any]:
    """预建工厂文件夹预览（只读）：装箱单工厂名单 vs 上游现有子目录 diff。

    流程（探测+解析与 service.start_batch_from_scan 同口径）：
    1. 监控目录下定位 folder_name，探测 ContentsOfTheContainer 装箱单
       （本层无命中向下一层钻取，复用 discovery.discover_downstream_files）；
    2. 解析装箱单拿工厂全名单（parse_downstream_file）；
    3. 上游根 = 装箱单所在目录（有「工厂」子目录则取它，
       discovery.pick_upstream_root）；
    4. 逐工厂 match_factory_folder：确定性档（alias/alias_ci/exact/
       alias_folder）命中现有子目录 → existing；否则按命名规则
       （factory_aliases 主数据 short_name 优先，无别名回退装箱单原名，
       安全化非法字符）生成待建 folder_name → missing。

    返回：
      - 正常：{"need_choice": False, "folder_name", "downstream_file_path",
        "upstream_root", "existing": [{factory, folder_name, method}],
        "missing": [{factory, folder_name}], "need_upstream": bool}
        need_upstream=True 表示装箱单旁没有标准「工厂」子目录，文件夹将
        直接建在装箱单同级（前端据此提示用户确认布局）；
      - 多装箱单候选且未指定：{"need_choice": True, "downstream_candidates":
        [路径...], "existing": [], "missing": [], "need_upstream": False}
        ——前端让用户选择后带 downstream_file_path 重调。

    异常契约（路由层转 HTTP）：文件夹不存在 → FileNotFoundError；
    装箱单找不到/解析失败/上游目录不存在或不可读 → ValueError。
    """
    from app.factory_match import (
        load_alias_map, load_folder_match_candidates, match_factory_folder)
    from app.nodes.parse_downstream import parse_downstream_file
    from app.orchestrator import factory_setup

    folder_name = (folder_name or "").strip()
    if not folder_name:
        raise ValueError("文件夹名不能为空")
    watch = _watch_path()
    folder = watch / folder_name
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在: {folder_name}")

    # 下游装箱单：显式指定 > 自动探测；多候选未指定 → 返回选择信号（不报错，
    # 只读预览让用户先看候选再选定重调）
    if downstream_file_path:
        chosen = Path(downstream_file_path).expanduser()
        if not chosen.is_file():
            raise ValueError(f"指定的装箱单不存在: {downstream_file_path}")
        candidates = [chosen]
    else:
        candidates = discovery.discover_downstream_files(folder)
        if not candidates:
            raise ValueError(
                f"子文件夹 {folder_name} 中未找到 ContentsOfTheContainer 装箱单文件")
        if len(candidates) > 1:
            return {"need_choice": True,
                    "downstream_candidates": [str(p) for p in candidates],
                    "existing": [], "missing": [], "need_upstream": False}
    downstream = candidates[0]

    # 上游根：与 start_batch_from_scan 同口径——装箱单实际所在目录
    # （装箱单常落在嵌套中间层，与「工厂」目录同层），有「工厂」子目录则取它
    base = downstream.parent
    upstream = discovery.pick_upstream_root(base)
    need_upstream = upstream == base  # 无标准「工厂」子目录，将建在装箱单同级
    if not upstream.is_dir():
        raise ValueError(f"上游工厂目录不存在: {upstream}")

    try:
        requirements, _ = parse_downstream_file(str(downstream))
    except Exception as e:  # noqa: BLE001 统一转 ValueError 给路由层 422
        raise ValueError(f"装箱单解析失败: {type(e).__name__}: {e}") from e

    try:
        folders = [d.name for d in upstream.iterdir() if d.is_dir()]
    except OSError as e:
        raise ValueError(f"上游工厂目录不可读: {e}") from e

    alias_map = load_alias_map()
    folder_candidates = load_folder_match_candidates()
    cutoff = get_settings().fuzzy_match_score_cutoff

    existing: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for factory in requirements:
        hit, _score, method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff,
            folder_candidates=folder_candidates)
        if hit and method in _PLAN_EXISTING_METHODS:
            existing.append({"factory": factory, "folder_name": hit,
                             "method": method})
            continue
        # 命名规则（已拍板方案 B）：主数据 short_name 优先，无别名回退
        # 装箱单原名；安全化与 factory_setup 建夹同一函数，口径一致
        desired = factory_setup._sanitize_folder_name(
            factory_setup._short_name_for_factory(factory) or factory)
        if desired in folders or any(m["folder_name"] == desired for m in missing):
            # 目标文件夹已存在（或两名工厂归并到同一待建名）：不重复创建
            existing.append({"factory": factory, "folder_name": desired,
                             "method": "desired_exists"})
            continue
        missing.append({"factory": factory, "folder_name": desired})

    return {"need_choice": False,
            "folder_name": folder_name,
            "downstream_file_path": str(downstream),
            "upstream_root": str(upstream),
            "existing": existing,
            "missing": missing,
            "need_upstream": need_upstream}


def prepare_folders(
    folder_name: str,
    downstream_file_path: str | None = None,
) -> dict[str, Any]:
    """预建工厂文件夹（写）：按 folder_plan 的 missing 清单批量 mkdir。

    幂等：已存在的目录跳过（进 skipped），重复调用安全。新建空文件夹本身
    不触发任何提取——操作员放文件后再启动批次。确认门由看板确认弹窗承担
    （一次一确认），本函数不再二次确认。

    异常契约同 folder_plan；多装箱单候选未指定时转 ValueError（写路径
    不返回选择信号，消息列出候选文件名）。mkdir 失败立即抛 ValueError
    （已建的不回滚——幂等，重试即可补齐）。
    """
    plan = folder_plan(folder_name, downstream_file_path)
    if plan.get("need_choice"):
        names = [Path(p).name for p in plan["downstream_candidates"]]
        raise ValueError(
            f"发现多个下游装箱单，请先指定其中一个：{'、'.join(names)}")

    upstream = Path(plan["upstream_root"])
    created: list[str] = []
    skipped: list[str] = []
    for item in plan["missing"]:
        target = upstream / item["folder_name"]
        if target.is_dir():
            skipped.append(item["folder_name"])
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(
                f"创建文件夹失败 {item['folder_name']}: {e}") from e
        created.append(item["folder_name"])

    logger.info("预建工厂文件夹 | folder=%s | 新建=%s | 已存在跳过=%s",
                folder_name, created, skipped)
    message = f"已创建 {len(created)} 个工厂文件夹"
    if skipped:
        message += f"，{len(skipped)} 个已存在跳过"
    if created:
        message += "。请把各工厂单据放入对应文件夹后再启动批次"
    return {"ok": True,
            "created": created,
            "skipped": skipped,
            "upstream_root": plan["upstream_root"],
            "message": message}
