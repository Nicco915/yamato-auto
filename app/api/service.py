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
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
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
from app.graph import NODE2, NODE5, get_graph
from app.logging_config import logging_context

logger = logging.getLogger(__name__)


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


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

# thread_id 是用户可输入的批次号，拼文件名前过滤路径分隔符等危险字符
# （防目录穿越）：只保留字母数字、中日韩字符、-_.，其余替换为 _
_PROGRESS_TAG_UNSAFE = re.compile(
    r"[^0-9A-Za-z一-鿿぀-ヿ가-힯._-]")


def _safe_path_tag(text: str) -> str:
    """把用户可输入的批次号过滤为安全的文件/目录名片段（防目录穿越）。

    预提取进度文件、会话归档目录共用同一套过滤规则。
    """
    tag = _PROGRESS_TAG_UNSAFE.sub("_", text)
    # 收缩连续下划线，并移除残留的 ..（防 .. 穿越到父目录）
    while ".." in tag:
        tag = tag.replace("..", "_")
    while "__" in tag:
        tag = tag.replace("__", "_")
    return tag


def _preextract_progress_path(thread_id: str) -> Path:
    """预提取进度文件路径：SESSIONS_DIR/_preextract_progress_{安全tag}.json。"""
    return SESSIONS_DIR / f"_preextract_progress_{_safe_path_tag(thread_id)}.json"


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
    from app.factory_match import load_alias_map, match_factory_folder
    from app.nodes.extraction_node import _run_factory_session, _try_load_cached_session

    root_path = Path(upstream_root).expanduser()
    if not root_path.is_dir():
        logger.warning("[预提取] 上游目录 %s 不存在，跳过", upstream_root)
        return

    folders = [d.name for d in root_path.iterdir() if d.is_dir()]
    alias_map = load_alias_map()
    cutoff = get_settings().fuzzy_match_score_cutoff
    progress = _PreExtractProgress(thread_id, factories) if thread_id else None

    for factory in factories:
        # 缓存已存在且新鲜（路径证据仍在当前批次 upstream_root 之下）则跳过；
        # 新鲜度校验是 rerun_with_paths 改路径重跑场景的兜底：旧根目录留下
        # 的缓存视为陈旧，照常重提，不误用上一批次数据
        if _try_load_cached_session(factory, str(root_path)) is not None:
            logger.info("[预提取] 工厂「%s」：缓存已存在，跳过", factory)
            if progress:
                progress.update(factory, "cached")
            continue

        # Node2 逻辑：匹配文件夹
        folder_name, score, method = match_factory_folder(
            factory, folders, alias_map, cutoff=cutoff)
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
            _run_factory_session(folder_path, factory, expected_skus)
            logger.info("[预提取] 工厂「%s」：完成（%s，得分 %.1f）",
                        factory, method, score)
            if progress:
                progress.update(factory, "done")
        except Exception as e:
            logger.exception("[预提取] 工厂「%s」：异常 %s，跳过", factory, e)
            if progress:
                progress.update(factory, "failed",
                                f"{type(e).__name__}: {e}"[:200])


# ---------------------------------------------------------------------------
# 批次启动归档旧 session 缓存（2026-08-05，陈旧会话缓存修复 方案A）
# ---------------------------------------------------------------------------
# 背景：sessions/{工厂}.json 只按工厂名命名、无批次维度。上一批次跑完后
# 这些 JSON 留在磁盘，新批次启动时 Node3 缓存命中逻辑和后台预提取会直接
# 命中旧缓存，导致新批次全部工厂沿用上一批次的提取结果和文件路径。
# 对策：新批次首次启动（checkpoint state 为空）时，把 sessions/ 下的
# *.json 整体移动到 sessions/_archive/{批次号}/——移动不删除，保留审计
# （零容错原则）。归档失败绝不阻塞批次启动。

_ARCHIVE_DIR_NAME = "_archive"


def _warn_if_other_batches_in_flight(thread_id: str) -> None:
    """归档后检查是否还有其他在途批次（pending_review/running），有则打醒目 warning。

    只读枚举（复用 list_batches），当前 thread_id 不计入。归档是可逆的移动
    操作，所以即使在途批次存在也照常归档，这里只负责提醒。检查本身失败
    只记 warning，绝不反过来影响批次启动。
    """
    try:
        batches = list_batches().get("batches") or []
    except Exception as e:  # noqa: BLE001 枚举失败不阻塞批次启动
        logger.warning("[会话归档] 在途批次检查失败（不影响归档结果）：%s: %s",
                       type(e).__name__, e)
        return
    in_flight = [b["thread_id"] for b in batches
                 if b.get("thread_id") != thread_id
                 and b.get("status") in ("pending_review", "running")]
    if in_flight:
        logger.warning("⚠️⚠️ [会话归档] 批次 %s 启动归档时仍有 %d 个在途批次"
                       "（%s）：这些批次的工厂会话已移入 _archive，"
                       "恢复时可能触发重新提取",
                       thread_id, len(in_flight), "、".join(in_flight))


def _archive_sessions_for_new_batch(thread_id: str) -> int:
    """批次启动前把 sessions/*.json 归档到 sessions/_archive/{批次号}/。

    只移动不删除（零容错审计）；归档目标目录已存在时直接并入（同名文件
    覆盖）；sessions 目录不存在或为空时静默跳过；单文件移动失败只记
    warning 继续，绝不阻塞批次启动。返回成功归档的文件数。
    """
    try:
        if not SESSIONS_DIR.is_dir():
            return 0
        # 只取顶层 *.json（_archive 是子目录，天然排除）
        files = sorted(p for p in SESSIONS_DIR.iterdir()
                       if p.is_file() and p.suffix == ".json")
        if not files:
            return 0

        dest_dir = SESSIONS_DIR / _ARCHIVE_DIR_NAME / _safe_path_tag(thread_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for src in files:
            try:
                # 同文件系统内 rename；os.replace 语义，同名旧档直接覆盖
                src.replace(dest_dir / src.name)
                moved += 1
            except OSError as e:
                logger.warning("[会话归档] 批次 %s：移动 %s 失败（跳过该文件，"
                               "不阻塞启动）：%s: %s",
                               thread_id, src.name, type(e).__name__, e)
        logger.info("[会话归档] 批次 %s：已归档 %d/%d 个会话文件到 %s",
                    thread_id, moved, len(files), dest_dir)
        if moved:
            # 有归档动作才做在途批次检查（避免无谓的全表枚举开销）
            _warn_if_other_batches_in_flight(thread_id)
        return moved
    except OSError as e:
        logger.warning("[会话归档] 批次 %s：归档异常（跳过归档，不阻塞启动）："
                       "%s: %s", thread_id, type(e).__name__, e)
        return 0


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
        # 方案A：新批次启动前归档上一批次遗留的 sessions/*.json。
        # 守卫：该 thread_id 已有 checkpoint state（重跑/异常重启续跑）时
        # 跳过归档——此时 sessions/ 里很可能就是本批次正在用的缓存，不能清。
        snap = graph.get_state(_config(thread_id))
        if snap.values:
            logger.info("[会话归档] 批次 %s 已有 checkpoint state（重跑/续跑场景），"
                        "跳过归档", thread_id)
        else:
            _archive_sessions_for_new_batch(thread_id)

        emit = _make_progress_emitter(on_progress, seed=initial_state)
        for event in graph.stream(initial_state, _config(thread_id), stream_mode="updates"):
            emit(event)
            if "__interrupt__" in event:
                payload = event["__interrupt__"][0].value
                # 首次 interrupt 后启动后台预提取：
                # 工厂 A 挂起等审核 → 后台线程开始提取工厂 B、C、D…
                _start_pre_extraction(thread_id)
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
    with logging_context(thread_id=thread_id, factory=factory):
        prepared = _prepare_audit(thread_id, resume_data)

        # 恢复执行，直到下一个 interrupt（多工厂循环时）或 END
        emit = _make_progress_emitter(on_progress, seed=state.values)
        for event in graph.stream(
            Command(resume=resume_data), _config(thread_id), stream_mode="updates"
        ):
            emit(event)
            if "__interrupt__" in event:
                payload = event["__interrupt__"][0].value
                # 每次 interrupt 后继续后台预提取剩余工厂
                # （工厂 B 审核中 → 后台提取 C、D…）
                _start_pre_extraction(thread_id)
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
        for event in graph.stream(None, cfg, stream_mode="updates"):
            emit(event)
            if "__interrupt__" in event:
                _start_pre_extraction(thread_id)
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


def retry_factory_extraction(
    thread_id: str,
    folder: str | None = None,
    save: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """单厂重试：只重跑当前挂起工厂的 Node3→Node5，已审核工厂不受影响。

    机制同 rerun_with_paths（langgraph 1.2.9 坑）：update_state(as_node=NODE2)
    作废 Node5 interrupt 现场，invoke 从 Node3 继续。Node2 仅作锚点不会重跑，
    current_factory_data 仍在 state 中供 Node3 使用。

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

    # 对照注入：校验 folder 并组装 update（校验失败 ValueError 直接上抛）
    validated_path: Path | None = None
    if folder is not None:
        from app.factory_match import validate_subfolder
        upstream_root = (
            snap.values.get("upstream_root") or get_settings().upstream_root)
        validated_path = validate_subfolder(upstream_root, folder)

    # L2 日志关联：单厂重试也是一次跑图，绑定批次号（工厂名由 Node3 重绑）
    with logging_context(thread_id=thread_id):
        # 置强制重提标志：Node3 跳过会话缓存重新走提取流程；
        # Node3 所有返回分支自清 force_reextract=False，不会残留到后续工厂。
        # 不清 extracted_items——Node3 各分支都会覆写 cur["extracted_items"]
        update: dict[str, Any] = {"force_reextract": True}
        if validated_path is not None:
            # 对照注入：folder_path 直接指向校验后的目录；
            # 批次级 factory_alias_overrides 同步写入，仅本次生效不落盘
            cur2 = dict(cur)
            cur2["folder_path"] = str(validated_path)
            update["current_factory_data"] = cur2
            overrides = dict(snap.values.get("factory_alias_overrides") or {})
            overrides[factory] = folder
            update["factory_alias_overrides"] = overrides
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
                return result

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
        return result


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


# ---------------------------------------------------------------------------
# 重复处理预检（W4b）：sessions 完成态 + 审核落库记录，四档判定工厂是否已处理
# ---------------------------------------------------------------------------

# 会话完成态（与 extraction/session.py 状态机口径一致）
_SESSION_DONE_STATUSES = ("complete_auto", "complete_manual")
# 会话进行态：提过但没提完，不算已处理，也不算"从没碰过"
_SESSION_PARTIAL_STATUSES = ("collecting", "waiting_pl")


def _latest_archived_session(factory: str) -> Path | None:
    """在 sessions/_archive/ 下找最新归档批次目录里的 {factory}.json。

    只在主路径未命中时被调用（避免每次预检都遍历归档）；归档批次目录
    按 mtime 取最新（实现最简单，且归档后目录不再变动，mtime 即归档时刻）。
    _archive 不存在/为空/任何 IO 异常都返回 None（视为无证据），绝不抛异常。
    """
    archive_root = SESSIONS_DIR / _ARCHIVE_DIR_NAME
    try:
        if not archive_root.is_dir():
            return None
        batch_dirs = [d for d in archive_root.iterdir() if d.is_dir()]
        if not batch_dirs:
            return None
        latest = max(batch_dirs, key=lambda d: d.stat().st_mtime)
        candidate = latest / f"{factory}.json"
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _session_status_light(factory: str) -> tuple[str | None, str | None]:
    """轻量读 sessions/{factory}.json，只取 (status, updated_at)，不算 coverage。

    主路径未命中时回读 sessions/_archive/ 最新归档批次目录中的同名文件：
    归档是批次边界（方案A 新批次启动会把上一批次 session 整体移入
    _archive），预检的「已处理」记忆需要跨批次回看最近一次归档，否则
    「提过完成但未审核落库」的工厂在新批次预检中被误判为未处理。
    文件不存在/归档为空/JSON 损坏一律静默回落，返回 (None, None)。
    """
    path = SESSIONS_DIR / f"{factory}.json"
    if not path.is_file():
        path = _latest_archived_session(factory)
        if path is None:
            return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 损坏的会话文件按无会话处理
        return None, None
    return data.get("status"), data.get("updated_at")


def check_processed_factories(
    downstream_file_path: str | None = None,
    factory_names: list[str] | None = None,
) -> dict[str, Any]:
    """预检装箱单各工厂是否已处理过（W4b 重复处理确认的唯一判定口径）。

    工厂集合：给出 factory_names 时直接用之（不解析文件，调用方已知名单）；
    缺省时解析装箱单（parse_requirements），解析失败抛 ValueError（路由层
    转 422）。

    level 四档（processed = level in (audited, session_complete)）：
      - audited：review_audits 有 approved=true 记录（最强，审核已落库）；
      - session_complete：sessions/*.json 为 complete_auto/complete_manual；
      - partial：collecting/waiting_pl（提过没提完，不算已处理）；
      - none：无任何记录。
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
            status, updated_at = _session_status_light(name)
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

    # W4b：skip_processed 实时重算差集（不沿用预览结论，防 TTL 内竞态）；
    # 显式 factory_filter 优先（互斥语义）
    effective_filter = factory_filter
    skipped: list[str] = []
    if skip_processed and not factory_filter:
        precheck = check_processed_factories(str(d_path))
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
    return result


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
