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
- current_slots / current_target_tool：Triage 分诊层的多轮参数收集
  槽位——操作员分多轮才把写工具参数说全时，已提取参数暂存于此，
  凑齐进 loop 生成 pending_action、或用户中止/换话题时清空；
- soft_pending：Triage 黄灯区（0.6–0.8 置信的写工具请求）的软挂起
  状态——确认式反问已发出、等操作员一轮内答复，"是"则按已存意图
  直接进 loop（不再过 triage），"算了"则全清，其他消息清挂起后
  正常走新一轮分诊。

存储为进程内 dict：重启即丢（可接受，pending 状态本就短命），需要跨重启
持久时再迁 app/db。锁只保护 dict 读写；session 本体在锁外被修改（含 LLM
调用期间），最坏情况是并发同会话丢一条历史，不影响安全——写操作的最后
防线是无状态的校验 + 人工确认。
"""
from __future__ import annotations

import threading
import time

_SESSION_TTL_SEC = 2 * 3600   # 会话闲置 2 小时过期
_SESSION_MAX = 500            # 会话总量上限（淘汰最旧）
_HISTORY_MAX_TURNS = 10       # 发给 LLM 的最大历史轮数（一轮 = user + assistant）

_TOOL_HISTORY_MAX = 50        # 工具审计流水上限（超出裁最旧）


class DispatcherSession:
    """单会话状态：对话历史 + 待确认写操作信封 + 工具调用审计流水。"""

    __slots__ = ("history", "pending_action", "tool_history", "updated_at",
                 "current_slots", "current_target_tool", "soft_pending")

    def __init__(self) -> None:
        # 发给 LLM 的精简历史：{"role": "user"/"assistant", "content": str}
        self.history: list[dict] = []
        # 服务端留存的待确认写操作 action 信封；二期 confirm 优先用它
        self.pending_action: dict | None = None
        # 工具调用记录（审计展示用）：
        # {"ts", "tool", "args_summary", "result_summary", "confirmed"}
        self.tool_history: list[dict] = []
        self.updated_at: float = time.time()
        # Triage 多轮参数收集槽位：未凑齐的工具参数 + 对应目标工具名
        self.current_slots: dict = {}
        self.current_target_tool: str | None = None
        # Triage 黄灯区软挂起（一轮有效）：
        # {"target_tool": str, "slots": dict, "question": str, "armed": True}
        self.soft_pending: dict | None = None


_SESSIONS: dict[str, DispatcherSession] = {}
_SESSIONS_LOCK = threading.Lock()


def get_session(session_id: str) -> DispatcherSession:
    """取会话（不存在则新建），惰性清理过期会话并控制总量。

    锁只保护 dict 读写；返回的 session 在锁外被修改（含 LLM 调用期间），
    最坏情况是并发同会话丢一条历史，不影响安全——写操作的最后防线是
    无状态的校验 + 人工确认。
    """
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
            sess = _SESSIONS[session_id] = DispatcherSession()
        sess.updated_at = now
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
    """把一轮对话写入历史，超出上限裁掉最旧的。"""
    session.history.append({"role": "user", "content": user_msg})
    session.history.append({"role": "assistant", "content": agent_msg})
    excess = len(session.history) - _HISTORY_MAX_TURNS * 2
    if excess > 0:
        del session.history[:excess]


def record_tool(session: DispatcherSession, tool: str, args_summary: str,
                result_summary: str, confirmed: bool | None = None) -> None:
    """追加一条工具调用记录（审计展示用），超上限裁最旧。

    confirmed 语义：None=只读工具（无需确认），True/False=写工具的
    人工确认结果（批准/拒绝）。只存摘要不存全文——完整数据由工具本身
    的日志/DB 承载，这里只做界面回放与排查用的时间线。
    """
    session.tool_history.append({
        "ts": time.time(),
        "tool": tool,
        "args_summary": args_summary,
        "result_summary": result_summary,
        "confirmed": confirmed,
    })
    excess = len(session.tool_history) - _TOOL_HISTORY_MAX
    if excess > 0:
        del session.tool_history[:excess]


def clear_pending(session: DispatcherSession) -> None:
    """清空待确认写操作信封（confirm 执行后 / reject 后 / 新写操作覆盖前调用）。"""
    session.pending_action = None


def set_slots(session: DispatcherSession, target_tool: str | None,
              slots: dict) -> None:
    """写入槽位状态（Triage 多轮参数收集用，凑齐进 loop 后由调用方清空）。"""
    session.current_target_tool = target_tool
    session.current_slots = dict(slots or {})


def clear_slots(session: DispatcherSession) -> None:
    """清空槽位状态（pending_action 创建 / confirm 终态 / qa 换话题 / 用户中止时调用）。

    软挂起 soft_pending 一并清：换话题/中止/终态时状态必须一致归零，
    否则下一轮会被入口短路误消费。
    """
    session.current_slots = {}
    session.current_target_tool = None
    session.soft_pending = None


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
