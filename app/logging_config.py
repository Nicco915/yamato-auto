"""中央日志配置：控制台 + 滚动文件双通道，供 API 服务与 CLI 入口统一接入。

用途：
- 控制台 StreamHandler，级别取环境变量 LOG_LEVEL（默认 INFO）；
- RotatingFileHandler 全量日志 app/data/logs/app.log（DEBUG 起）；
- RotatingFileHandler 错误日志 app/data/logs/error.log（WARNING 起）；
- 接管 uvicorn 三个 logger，统一走 root 的 handler/格式。

L2 批次关联（contextvars）：
- 业务入口（service 层批次函数）用 logging_context(thread_id=.../factory=...)
  上下文管理器绑定关联字段，退出时按 token 恢复前值（嵌套安全）；
  图内节点入口用 bind_factory_from_state(state) 各自绑定当前工厂名；
- ContextFilter（挂在各 handler 上）把 contextvars 当前值写入
  record.thread_id / record.factory，ContextFormatter 据此渲染
  "[thread_id factory]"；未绑定时不设属性、占位自动省略；
- grep 批次号即可回放该批次全链路日志。
"""
import contextlib
import contextvars
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 用 Path(__file__) 推导日志目录（与 llm_client.py 的 .env 推导方式一致），
# 避免 import app.config 造成循环依赖
_LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"

# root logger 上的幂等标记：重复调用 setup_logging() 不重复添加 handler
_SETUP_FLAG = "_yamato_logging_setup"

# 文件滚动参数：单文件 10MB，保留 5 个备份
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


# 哨兵：区分"record 上本来就没有 context 属性"与"旧值恰好是某个对象"
_CONTEXT_MISSING = object()


# ---------------------------------------------------------------------------
# L2：批次关联字段（contextvars）
# ---------------------------------------------------------------------------
# ContextVar 默认 None：未绑定时 ContextFilter 不设 record 属性，
# ContextFormatter 自动省略 "[...]" 占位。
#
# 并发安全依据（实测验证）：
# - asyncio.to_thread 会把调用处的 context 拷贝进 worker 线程，
#   worker 内 set 只影响该拷贝，并发请求互不串扰；
# - LangGraph 同步图每个节点也在拷贝的 context 中执行——节点能读到
#   service 层绑定的 thread_id，但节点内 set 不外泄给后续节点
#   （folder_router 绑 factory 因此只覆盖自身日志，离开即自动失效，
#   无需显式清理；后续节点如需工厂名须各自从 state 绑定）。
log_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_thread_id", default=None
)
log_factory: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "log_factory", default=None
)


def bind_context(
    thread_id: str | None = None, factory: str | None = None
) -> list[tuple[contextvars.ContextVar, contextvars.Token]]:
    """绑定当前上下文的关联字段；传 None 的字段保持现状（不清除）。

    返回 (ContextVar, token) 列表——ContextVar.set() 的 token 可供
    var.reset(token) 恢复进入前的值（logging_context 的嵌套安全
    正是基于此）；不关心恢复的调用方直接忽略返回值即可。
    """
    tokens: list[tuple[contextvars.ContextVar, contextvars.Token]] = []
    if thread_id is not None:
        tokens.append((log_thread_id, log_thread_id.set(thread_id)))
    if factory is not None:
        tokens.append((log_factory, log_factory.set(factory)))
    return tokens


@contextlib.contextmanager
def logging_context(thread_id: str | None = None, factory: str | None = None):
    """关联字段绑定的上下文管理器：进入时绑定，退出时恢复前值。

    嵌套安全（L2 审查修复点 2）：与 "try/finally clear_context()" 不同，
    __exit__ 用 var.reset(token) 恢复进入前的值——嵌套调用（如
    dispatcher 工具同步调 service.resume_order）退出内层后，外层已绑的
    thread_id 原样恢复，不会被抹成 None。
    """
    tokens = bind_context(thread_id=thread_id, factory=factory)
    try:
        yield
    finally:
        # 逆序 reset：与 set 顺序对称，逐层恢复前值
        for var, token in reversed(tokens):
            var.reset(token)


def clear_context() -> None:
    """清空当前上下文的全部关联字段（无条件置 None）。

    仅用于"确知无外层绑定"的场景（如一次性 CLI 入口的最外层）；
    可能被嵌套调用的入口必须改用 logging_context()——否则内层退出时
    会把外层已绑的 thread_id 抹掉。
    """
    log_thread_id.set(None)
    log_factory.set(None)


def bind_factory_from_state(state) -> None:
    """节点入口绑定当前工厂名：从 state["current_factory_data"]["factory_name"] 取。

    多工厂批次 resume 链上，Node3 提取 / Node4 核算 / Node5 审核 / Node6 写回
    处理的是工厂 F_{k+1} 的数据，但节点 context 在 submit 时拷贝自调用处
    （service 层残留的是上一工厂 F_k），节点内 set 又不外泄——因此每个节点
    入口须各自从 state 重绑（与 folder_router 的既有模式一致）。
    节点独立 context 保证绑定离开即失效，无需清理。
    取不到工厂名时不绑定、不抛异常（缺省容错）。
    """
    factory = ((state or {}).get("current_factory_data") or {}).get("factory_name")
    if factory:
        bind_context(factory=factory)


class ContextFilter(logging.Filter):
    """把 contextvars 当前值写入 record.thread_id / record.factory。

    值为 None 时不设属性（ContextFormatter 对缺失属性自动省略占位，
    也避免把 None 渲染进日志）。始终返回 True，不做级别过滤。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        thread_id = log_thread_id.get()
        factory = log_factory.get()
        if thread_id is not None:
            record.thread_id = thread_id
        if factory is not None:
            record.factory = factory
        return True


class ContextFormatter(logging.Formatter):
    """带关联字段占位的 Formatter。

    record 上若存在 thread_id / factory 属性（由 ContextFilter 从 contextvars
    注入），则在 logger 名后输出 "[thread_id factory]"；缺失时自动省略，
    不影响普通 record 的格式化。
    """

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        thread_id = getattr(record, "thread_id", None)
        factory = getattr(record, "factory", None)
        if thread_id:
            parts.append(str(thread_id))
        if factory:
            parts.append(str(factory))
        # 临时塞入 record，让格式串里的 %(context)s 统一渲染；渲染后恢复/清理
        old_context = getattr(record, "context", _CONTEXT_MISSING)
        record.context = f"[{' '.join(parts)}] " if parts else ""
        try:
            return super().format(record)
        finally:
            # 避免污染 record（同一 record 可能被多个 handler 格式化）：
            # 用户经 extra={"context": ...} 自带的同名属性恢复原值，
            # 原本没有该属性的才删除
            if old_context is _CONTEXT_MISSING:
                del record.context
            else:
                record.context = old_context


def _build_formatter() -> ContextFormatter:
    """统一格式：时间(毫秒) | 级别 | logger名 | [关联占位] | 消息。"""
    # 不传 datefmt：logging 默认时间格式自带 ",毫秒" 后缀
    return ContextFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(context)s%(message)s",
    )


def _takeover_uvicorn() -> None:
    """接管 uvicorn：清空其自带 handler 并上抛，统一走 root 的格式与文件。

    注意时机：以 uvicorn.run(app 对象) 方式启动时，uvicorn 会在
    setup_logging() 之后用自带 dictConfig 重配 uvicorn 系列 logger，
    把这里的接管结果覆盖掉（handler 被重置、propagate 被改回 False）。
    因此除 setup_logging() 内调用外，FastAPI startup 钩子需再调用一次。
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


def setup_logging() -> None:
    """初始化全局日志配置（幂等，可重复调用）。"""
    root = logging.getLogger()
    if getattr(root, _SETUP_FLAG, False):
        return  # 已初始化过，直接返回，不重复添加 handler

    # 日志目录自动创建
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = _build_formatter()

    # L2 关联字段注入：ContextFilter 必须挂在 handler 上，而不是 root logger 上——
    # logger 级 filter 只作用于该 logger 自己产生的记录，对子 logger
    # propagate 上来的记录不生效；handler.filter 才是每条日志的必经之处。
    context_filter = ContextFilter()

    # a) 控制台：级别取 LOG_LEVEL 环境变量，默认 INFO
    console_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level, logging.INFO))
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    # b) 全量文件：DEBUG 起，滚动保留
    app_handler = RotatingFileHandler(
        _LOG_DIR / "app.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(formatter)
    app_handler.addFilter(context_filter)

    # c) 错误文件：WARNING 起，滚动保留
    error_handler = RotatingFileHandler(
        _LOG_DIR / "error.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    error_handler.addFilter(context_filter)

    root.setLevel(logging.DEBUG)  # 总开关放到最低，由 handler 各自过滤
    root.addHandler(console_handler)
    root.addHandler(app_handler)
    root.addHandler(error_handler)

    # 接管 uvicorn（时机问题见 _takeover_uvicorn docstring）
    _takeover_uvicorn()

    # 打幂等标记，后续调用直接短路
    setattr(root, _SETUP_FLAG, True)
