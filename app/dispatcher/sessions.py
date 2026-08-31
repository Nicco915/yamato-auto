"""调度 Agent 会话记忆（L1 进程内会话存储）。

调度 Agent（app/dispatcher）是操作员的自然语言入口：查批次、解释错误、
发起批次、改数审核、重跑、改路径。对话天然多轮——操作员先说"看一下
昨天的批次"，再说"把那个挂起的重跑一遍"——无状态单轮解析必然失败，
因此每个 session_id 持有一个 DispatcherSession。

与 agent_chat 的关系：模式复制自 app/agent_chat.py 的 _ChatSession/
_get_session/_record_turn（TTL 惰性清理 + 总量淘汰最旧），但**独立存储**
——调度会话要额外承载三件 agent_chat 没有的东西：
- pending_action：写操作（create_batch/rerun/submit_review/set_paths）的
  待确认 action 信封。铁律是 LLM 只发起、系统生成预览、人工确认后才执行；
  信封必须由服务端持有，confirm 端点优先用它而不是客户端回传的参数，
  防止 LLM/前端在确认间隙篡改内容；
- tool_history：工具调用审计流水（含确认结果），供界面回放"Agent 做过
  什么、操作员批没批"，也是排查"它为什么说已执行"类事故的依据；
- current_slots 已随 legacy（Triage 分诊层）删除：react 引擎的多轮协商由
  模型自主完成，参数暂存不再需要代码侧槽位；
- soft_pending：黄灯区（模型对写工具意图不确定）的软挂起
  状态——确认式反问已发出、等操作员一轮内答复，"是"则按已存意图
  直接出确认卡（不进 LLM），"算了"则全清，其他消息清挂起后
  正常走引擎。

存储为进程内 dict：重启即丢（可接受，pending 状态本就短命），需要跨重启
持久时再迁 app/db。锁只保护 dict 读写；session 本体在锁外被修改（含 LLM
调用期间），最坏情况是并发同会话丢一条历史，不影响安全——写操作的最后
防线是无状态的校验 + 人工确认。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from app.db.models import ChatSession as _ChatSessionOrm
from app.db.models import ChatMessage as _ChatMessageOrm
from app.db.models import ChatToolHistory as _ChatToolHistoryOrm
from app.db.session import get_session as _get_db_session

logger = logging.getLogger(__name__)

_SESSION_TTL_SEC = 2 * 3600   # 会话闲置 2 小时过期
_SESSION_MAX = 500            # 会话总量上限（淘汰最旧）
_HISTORY_MAX_TURNS = 30       # 发给 LLM 的最大历史轮数（一轮 = user + assistant）

_TOOL_HISTORY_MAX = 50        # 工具审计流水上限（超出裁最旧）


class DispatcherSession:
    """单会话状态：对话历史 + 待确认写操作信封 + 工具调用审计流水。"""

    __slots__ = ("session_id", "history", "pending_action", "tool_history", "updated_at",
                 "soft_pending",
                 "pending_file_selection")

    def __init__(self, session_id: str | None = None) -> None:
        # 持久化 session_id（有则写 DB，无则纯内存会话）
        self.session_id: str | None = session_id
        # 发给 LLM 的精简历史：{"role": "user"/"assistant", "content": str}
        self.history: list[dict] = []
        # 服务端留存的待确认写操作 action 信封；二期 confirm 优先用它
        self.pending_action: dict | None = None
        # 工具调用记录（审计展示用）：
        # {"ts", "tool", "args_summary", "result_summary", "confirmed"}
        self.tool_history: list[dict] = []
        self.updated_at: float = time.time()
        # 黄灯区软挂起（一轮有效）：
        # {"target_tool": str, "slots": dict, "question": str, "armed": True}
        self.soft_pending: dict | None = None
        # UI 文件选择挂起：request_file_selection 工具触发后等待用户选择
        # {"type": "file"|"dir", "extensions": str|None, "title": str|None,
        #  "created_at": float}
        self.pending_file_selection: dict | None = None


_SESSIONS: dict[str, DispatcherSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _ensure_session_row(session_id: str) -> None:
    """确保 chat_sessions 表有该 session_id 的行（不存在则插入）。

    新建会话时调用一次，后续 record_turn/record_tool/persist_pending 依赖此行存在。
    """
    try:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if row is None:
                db.add(_ChatSessionOrm(session_id=session_id))
                db.commit()
    except Exception as exc:  # noqa: BLE001 DB 失败不阻塞主流程
        logger.warning("_ensure_session_row 失败: %s", exc)


def _auto_title(session_id: str, msg: str) -> None:
    """首条用户消息截断 30 字自动生成标题（仅当 DB 中 title 为空时）。"""
    if not session_id or not msg or not msg.strip():
        return
    try:
        # 截断逻辑：中文按字符，英文按空格边界
        if len(msg) <= 30:
            title = msg.strip()
        elif msg[30] == " " or msg[29] == " ":
            title = msg[:30].rstrip()
        else:
            title = msg[:30].rstrip() + "…"

        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if row and not row.title:
                row.title = title
                row.title_source = "auto"
                db.commit()
    except Exception as exc:
        logger.warning("_auto_title 失败 | session=%s | %s", session_id, exc)


def _maybe_llm_title(session_id: str) -> None:
    """累计消息数达标且标题为 auto 时，调 LLM 生成精炼标题。

    触发条件：DB 中 title_source == "auto"（手动改的跳过）。
    失败静默（辅助功能，不阻塞主流程）。
    """
    if not session_id:
        return
    try:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if not row or row.title_source != "auto":
                return
            # 取最近 6 条消息（3 轮对话）拼成摘要
            msgs = (db.query(_ChatMessageOrm)
                    .filter(_ChatMessageOrm.session_id == session_id)
                    .order_by(_ChatMessageOrm.ts.desc())
                    .limit(6)
                    .all())
            msgs = list(reversed(msgs))  # 按 ts 升序

        if len(msgs) < 4:  # 至少 2 轮对话才值得生成标题
            return

        # 拼装 prompt
        snippet_parts = []
        for m in msgs:
            content = (m.content or "")[:80]
            role_cn = "用户" if m.role == "user" else "助手"
            snippet_parts.append(f"[{role_cn}] {content}")
        snippet = "\n".join(snippet_parts)

        from app.extraction import llm_client
        prompt = (
            "用 10 字以内概括以下对话的主题（中文），只输出标题文本，不要引号，不要标点。\n\n"
            + snippet
        )
        summary = llm_client.chat_completion(
            [{"role": "user", "content": prompt}],
            source_file="session_title",
            max_tokens=50,
            json_mode=False,
        ).strip()

        if not summary or len(summary) > 20:
            return  # LLM 返回异常，跳过

        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if row and row.title_source == "auto":  # 双重检查，防止并发手动改
                row.title = summary
                row.title_source = "llm"
                db.commit()
    except Exception as exc:
        logger.warning("_maybe_llm_title 失败 | session=%s | %s", session_id, exc)


def _hydrate_from_db(session_id: str) -> DispatcherSession | None:
    """从 DB 加载会话（内存 miss 时兜底）。

    查到 ChatSession 行 → 重建 DispatcherSession（history/tool_history/pending_action）；
    查不到 → 返回 None。pending_action 若超 TTL 则直接清 DB 并置 None。
    """
    try:
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session_id)
            if row is None:
                return None
            sess = DispatcherSession(session_id=session_id)
            # 历史消息：按 ts 升序
            msgs = (db.query(_ChatMessageOrm)
                    .filter(_ChatMessageOrm.session_id == session_id)
                    .order_by(_ChatMessageOrm.ts.asc())
                    .all())
            sess.history = [{"role": m.role, "content": m.content} for m in msgs]
            # 工具审计流水：按 ts 升序
            tools = (db.query(_ChatToolHistoryOrm)
                     .filter(_ChatToolHistoryOrm.session_id == session_id)
                     .order_by(_ChatToolHistoryOrm.ts.asc())
                     .all())
            sess.tool_history = [
                {"ts": m.ts, "tool": m.tool, "args_summary": m.args_summary,
                 "result_summary": m.result_summary, "confirmed": m.confirmed}
                for m in tools
            ]
            # pending_action：陈旧检查
            if row.pending_action_json:
                try:
                    action = json.loads(row.pending_action_json)
                    # 延迟 import 避免循环依赖
                    from app.dispatcher.executor import ACTION_TTL_SEC
                    created_at = float(action.get("created_at", 0))
                    if time.time() - created_at > ACTION_TTL_SEC:
                        # 陈旧：清 DB
                        row.pending_action_json = None
                        row.updated_at = datetime.now(timezone.utc)
                        db.commit()
                        logger.info("hydrate 发现陈旧 pending_action，已清 DB | session=%s",
                                    session_id)
                    else:
                        sess.pending_action = action
                except (json.JSONDecodeError, ValueError, TypeError) as exc:
                    logger.warning("pending_action_json 解析失败 | session=%s | 原因=%s",
                                   session_id, exc)
            # updated_at：从 DB 读（用于 TTL 计算）
            if row.updated_at:
                sess.updated_at = row.updated_at.timestamp()
            return sess
    except Exception as exc:  # noqa: BLE001 DB 失败不阻塞主流程
        logger.warning("_hydrate_from_db 失败 | session=%s | 原因=%s", session_id, exc)
        return None


def get_session(session_id: str) -> DispatcherSession:
    """取会话（不存在则新建），惰性清理过期会话并控制总量。

    锁只保护 dict 读写；返回的 session 在锁外被修改（含 LLM 调用期间），
    最坏情况是并发同会话丢一条历史，不影响安全——写操作的最后防线是
    无状态的校验 + 人工确认。

    内存 miss 时从 DB hydrate（重启后恢复）。
    """
    needs_db_create = False
    with _SESSIONS_LOCK:
        now = time.time()
        expired = [k for k, s in _SESSIONS.items()
                   if now - s.updated_at > _SESSION_TTL_SEC]
        for k in expired:
            del _SESSIONS[k]
        if len(_SESSIONS) >= _SESSION_MAX and session_id not in _SESSIONS:
            oldest = min(_SESSIONS, key=lambda k: _SESSIONS[k].updated_at)
            del _SESSIONS[oldest]
        sess = _SESSIONS.get(session_id)
        if sess is None:
            # 内存 miss：先尝试从 DB hydrate
            sess = _hydrate_from_db(session_id)
            if sess is None:
                # 全新会话：标记需要在锁外创建 DB 行
                sess = DispatcherSession(session_id=session_id)
                needs_db_create = True
            _SESSIONS[session_id] = sess
        sess.updated_at = now
    # 锁外做 DB 操作（避免持锁期间访问 DB）
    if needs_db_create:
        _ensure_session_row(session_id)
    return sess


def peek_session(session_id: str) -> DispatcherSession | None:
    """只读查看会话：不创建、不刷新 updated_at（不续 TTL），不存在返回 None。

    供 GET /api/v1/dispatcher/history 用（W3）——切页刷新是高频只读动作，
    绝不能像 get_session 那样刷出空会话（刷爆 _SESSION_MAX 淘汰无辜会话）
    或给濒死会话续命。锁内顺手清理 TTL 过期会话（含被查询的这个）。
    """
    with _SESSIONS_LOCK:
        now = time.time()
        expired = [k for k, s in _SESSIONS.items()
                   if now - s.updated_at > _SESSION_TTL_SEC]
        for k in expired:
            del _SESSIONS[k]
        return _SESSIONS.get(session_id)


def record_turn(session: DispatcherSession, user_msg: str, agent_msg: str) -> None:
    """把一轮对话写入历史，超出上限裁掉最旧的。

    末尾写穿 DB（INSERT chat_messages × 2 + UPDATE chat_sessions.updated_at）。
    """
    session.history.append({"role": "user", "content": user_msg})
    session.history.append({"role": "assistant", "content": agent_msg})
    excess = len(session.history) - _HISTORY_MAX_TURNS * 2
    if excess > 0:
        del session.history[:excess]
    # 写穿 DB
    if session.session_id:
        try:
            now = time.time()
            with _get_db_session() as db:
                db.add(_ChatMessageOrm(
                    session_id=session.session_id, role="user",
                    content=user_msg, ts=now))
                db.add(_ChatMessageOrm(
                    session_id=session.session_id, role="assistant",
                    content=agent_msg, ts=now))
                row = db.get(_ChatSessionOrm, session.session_id)
                if row is not None:
                    row.updated_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as exc:  # noqa: BLE001 DB 失败不阻塞主流程
            logger.warning("record_turn DB 写穿失败: %s", exc)
    # 自动标题：首条用户消息截断生成（异步，不阻塞主流程已无法——
    # record_turn 本身是同步的，_auto_title 内部 try/except 兜底）
    if session.session_id and user_msg:
        _auto_title(session.session_id, user_msg)
    # LLM 摘要标题：累计 4 条消息（2 轮对话）后尝试升级 auto → llm
    if session.session_id:
        _maybe_llm_title(session.session_id)


def record_tool(session: DispatcherSession, tool: str, args_summary: str,
                result_summary: str, confirmed: bool | None = None) -> None:
    """追加一条工具调用记录（审计展示用），超上限裁最旧。

    confirmed 语义：None=只读工具（无需确认），True/False=写工具的
    人工确认结果（批准/拒绝）。只存摘要不存全文——完整数据由工具本身
    的日志/DB 承载，这里只做界面回放与排查用的时间线。

    末尾写穿 DB（INSERT chat_tool_history）。
    """
    ts = time.time()
    session.tool_history.append({
        "ts": ts,
        "tool": tool,
        "args_summary": args_summary,
        "result_summary": result_summary,
        "confirmed": confirmed,
    })
    excess = len(session.tool_history) - _TOOL_HISTORY_MAX
    if excess > 0:
        del session.tool_history[:excess]
    # 写穿 DB
    if session.session_id:
        try:
            with _get_db_session() as db:
                db.add(_ChatToolHistoryOrm(
                    session_id=session.session_id, tool=tool,
                    args_summary=args_summary, result_summary=result_summary,
                    confirmed=confirmed, ts=ts))
                db.commit()
        except Exception as exc:  # noqa: BLE001 DB 失败不阻塞主流程
            logger.warning("record_tool DB 写穿失败: %s", exc)


def persist_pending(session: DispatcherSession) -> None:
    """将 session.pending_action 写穿到 chat_sessions.pending_action_json。

    pending_action 为 None 时写 NULL（confirm 后清 DB）。
    """
    if not session.session_id:
        return
    try:
        payload = (json.dumps(session.pending_action, ensure_ascii=False)
                   if session.pending_action else None)
        with _get_db_session() as db:
            row = db.get(_ChatSessionOrm, session.session_id)
            if row is not None:
                row.pending_action_json = payload
                row.updated_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as exc:  # noqa: BLE001 DB 失败不阻塞主流程
        logger.warning("persist_pending 失败: %s", exc)


def clear_pending(session: DispatcherSession) -> None:
    """清空待确认写操作信封（confirm 执行后 / reject 后 / 新写操作覆盖前调用）。

    内存清完后写穿 DB（pending_action_json = NULL）。
    """
    session.pending_action = None
    persist_pending(session)


def set_soft_pending(session: DispatcherSession, tool: str, slots: dict,
                     question: str) -> None:
    """写入黄灯区软挂起（确认式反问已发出，等操作员一轮内答复）。"""
    session.soft_pending = {
        "target_tool": tool,
        "slots": dict(slots or {}),
        "question": question,
        "armed": True,
    }


def clear_soft_pending(session: DispatcherSession) -> None:
    """清空黄灯区软挂起（软确认提升 / 软否定 / 换话题时调用）。"""
    session.soft_pending = None


def set_file_selection_request(session: DispatcherSession, *,
                                file_type: str, extensions: str | None = None,
                                title: str | None = None) -> None:
    """写入 UI 文件选择挂起（request_file_selection 工具触发后）。"""
    session.pending_file_selection = {
        "type": file_type,
        "extensions": extensions,
        "title": title,
        "created_at": time.time(),
    }


def clear_file_selection_request(session: DispatcherSession) -> None:
    """清空 UI 文件选择挂起（用户已选择或取消后调用）。"""
    session.pending_file_selection = None


__all__ = [
    "DispatcherSession",
    "get_session",
    "peek_session",
    "record_turn",
    "record_tool",
    "persist_pending",
    "clear_pending",
    "set_soft_pending",
    "clear_soft_pending",
    "set_file_selection_request",
    "clear_file_selection_request",
]
