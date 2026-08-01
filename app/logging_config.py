"""中央日志配置：控制台 + 滚动文件双通道，供 API 服务与 CLI 入口统一接入。

用途（L1 阶段）：
- 控制台 StreamHandler，级别取环境变量 LOG_LEVEL（默认 INFO）；
- RotatingFileHandler 全量日志 app/data/logs/app.log（DEBUG 起）；
- RotatingFileHandler 错误日志 app/data/logs/error.log（WARNING 起）；
- 接管 uvicorn 三个 logger，统一走 root 的 handler/格式。

后续 L2 步骤将在此之上加 contextvars 关联字段（thread_id/factory），
Formatter 已预留占位：record 上有对应属性时输出 "[thread_id factory]"，
没有则省略（属性缺失也能正常格式化）。
"""
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


class ContextFormatter(logging.Formatter):
    """带关联字段占位的 Formatter。

    record 上若存在 thread_id / factory 属性（后续由 Filter 从 contextvars
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

    # a) 控制台：级别取 LOG_LEVEL 环境变量，默认 INFO
    console_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, console_level, logging.INFO))
    console_handler.setFormatter(formatter)

    # b) 全量文件：DEBUG 起，滚动保留
    app_handler = RotatingFileHandler(
        _LOG_DIR / "app.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    app_handler.setLevel(logging.DEBUG)
    app_handler.setFormatter(formatter)

    # c) 错误文件：WARNING 起，滚动保留
    error_handler = RotatingFileHandler(
        _LOG_DIR / "error.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    root.setLevel(logging.DEBUG)  # 总开关放到最低，由 handler 各自过滤
    root.addHandler(console_handler)
    root.addHandler(app_handler)
    root.addHandler(error_handler)

    # 接管 uvicorn（时机问题见 _takeover_uvicorn docstring）
    _takeover_uvicorn()

    # 打幂等标记，后续调用直接短路
    setattr(root, _SETUP_FLAG, True)
