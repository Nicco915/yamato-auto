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
import sqlite3
from pathlib import Path
from typing import Any

import ormsgpack
from langgraph.types import Command
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ReviewAudit
from app.db.session import get_session
from app.extraction.llm_client import usage_tracker
from app.extraction.session import SESSIONS_DIR
from app.graph import get_graph
from app.logging_config import logging_context

logger = logging.getLogger(__name__)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def run_until_interrupt(
    thread_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
    factory_alias_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """启动流程，执行 Node1-4 直到 Node5 的 interrupt 挂起（或全程无挂起跑完）。

    factory_alias_overrides：批次级「仅本次生效」工厂名对照（W5），
    非空才写进 initial_state（Node2 匹配的最高优先档，不落盘）。

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

        for event in graph.stream(initial_state, _config(thread_id), stream_mode="updates"):
            if "__interrupt__" in event:
                payload = event["__interrupt__"][0].value
                return {
                    "status": "pending_human_review",
                    "thread_id": thread_id,
                    "review_data": payload,
                }

        final = graph.get_state(_config(thread_id))
        return {
            "status": "completed",
            "thread_id": thread_id,
            "final_output_path": final.values.get("final_output_path"),
        }


def resume_order(thread_id: str, resume_data: dict) -> dict[str, Any]:
    """用人工反馈数据唤醒挂起的图，继续执行 Node6/7（写 Excel + 落库）。

    resume_data 结构见 nodes/human_review.py 的 docstring。

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
    with logging_context(thread_id=thread_id, factory=factory):
        prepared = _prepare_audit(thread_id, resume_data)

        # 恢复执行，直到下一个 interrupt（多工厂循环时）或 END
        for event in graph.stream(
            Command(resume=resume_data), _config(thread_id), stream_mode="updates"
        ):
            if "__interrupt__" in event:
                payload = event["__interrupt__"][0].value
                result = {
                    "status": "pending_human_review",
                    "thread_id": thread_id,
                    "review_data": payload,
                }
                _write_audit(prepared, result.get("status"))
                return result

        final = graph.get_state(_config(thread_id))
        result = {
            "status": "success",
            "message": "数据已成功落库并写入下游表格",
            "final_validation_status": final.values.get("validation_status"),
            "final_output_path": final.values.get("final_output_path"),
        }
        _write_audit(prepared, result.get("status"))
        return result


def rerun_with_paths(
    thread_id: str,
    upstream_root: str | None = None,
    downstream_file_path: str | None = None,
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
        for event in graph.stream(None, cfg, stream_mode="updates"):
            if "__interrupt__" in event:
                return {
                    "status": "pending_human_review",
                    "thread_id": thread_id,
                    "review_data": event["__interrupt__"][0].value,
                }

        final = graph.get_state(cfg)
        return {
            "status": "completed",
            "thread_id": thread_id,
            "final_output_path": final.values.get("final_output_path"),
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

    注意：payload 不在 state.values 里，而在 tasks[].interrupts 中。
    未挂起或 thread_id 不存在时返回 None。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    for task in snap.tasks:
        if task.interrupts:
            return task.interrupts[0].value
    return None


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
    """枚举全部批次 thread_id，按最早 checkpoint 倒序（最新批次在前）。"""
    rows = conn.execute(
        "SELECT thread_id FROM checkpoints "
        "GROUP BY thread_id ORDER BY MIN(checkpoint_id) DESC"
    ).fetchall()
    return [r[0] for r in rows]


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


def get_batch_upstream_root(thread_id: str) -> str:
    """该批次 checkpoint state 里记录的 upstream_root；无 state 时回退 settings 当前值。

    供审核页白名单做批次级二级兜底（W1）：改全局路径后，旧批次的单据
    仍应按其创建时（已经过 is_dir 校验）的 root 放行。绝不接受请求参数，
    唯一来源是 checkpoint state / settings。
    """
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    root = (snap.values or {}).get("upstream_root")
    if root:
        return str(root)
    return get_settings().upstream_root


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


def _load_factory_session(factory: str) -> dict[str, Any] | None:
    """加载工厂提取会话摘要（data/sessions/{工厂名}.json 同名匹配）。

    会话语义是"该工厂最近一次提取记录"，不专属任何批次。
    文件不存在或损坏时返回 None。coverage 由 items 键集 vs expected_skus
    重算（与 extraction/session.py 的 coverage() 口径一致）。
    """
    path = SESSIONS_DIR / f"{factory}.json"
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

    factories = []
    for name in sorted(total_set):
        if name == current and snap.next:
            role = "current"
        elif name in pending:
            role = "pending"
        else:
            role = "done"
        factories.append({
            "factory": name,
            "role": role,
            "session": _load_factory_session(name),
        })

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

    detail.update({
        "downstream_file_path": values.get("downstream_file_path"),
        "upstream_root": values.get("upstream_root"),
        "final_output_path": values.get("final_output_path"),
        "validation_status": values.get("validation_status"),
        "factories": factories,
        "audit": audit,
        "usage": _usage_with_scope(),
    })
    return detail


def delete_batch(thread_id: str) -> dict[str, Any]:
    """删除过往批次：清掉 checkpoints.db 里该 thread_id 的全部状态（checkpoints + writes）。

    状态语义（与 _summarize_snapshot 一致）：
      - snap.values 为空 → ValueError（路由转 404）；
      - 有 task.interrupts → pending_review（允许删，废弃场景）；
      - next 非空且无 interrupt → running → RuntimeError（路由转 409）；
      - next 为空 → completed（允许删）。

    删除用独立 rw 连接（不碰 graph 单例 saver 连接，WAL 下并发安全）；
    只动 checkpoints.db——sessions/*.json、主数据、输出文件一律不碰。
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
            "checkpoints_removed": checkpoints_removed,
            "writes_removed": writes_removed,
        }


def create_batch(
    thread_id: str,
    downstream_file_path: str | None = None,
    upstream_root: str | None = None,
    factory_filter: list[str] | None = None,
    factory_alias_overrides: dict[str, str] | None = None,
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
      非空才写入 state（Node2 最高优先匹配档，不落盘）。
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

    return run_until_interrupt(thread_id, str(d_path), str(u_path),
                               factory_filter=factory_filter,
                               factory_alias_overrides=factory_alias_overrides)


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
        load_alias_map, match_factory_folder, recommend_candidates)
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
    cutoff = settings.fuzzy_match_score_cutoff
    for factory in factories:
        folder, score, method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff)
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
        "edited_count": edited_count,
        "changes": changes,
        "new_skus": new_skus,
    }


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
