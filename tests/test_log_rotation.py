# -*- coding: utf-8 -*-
"""日志按日轮转测试（app/logging_config.py + app/dispatcher/debug_log.py）。

对应《2026-10-14-批次终止与日志轮转-实施计划.md》阶段三（3.4）：
1. setup_logging() 后 root 的两个文件 handler 均为 TimedRotatingFileHandler，
   when=="MIDNIGHT"、backupCount==30、utc is False、encoding 含 utf-8；
   控制台 StreamHandler 仍在；重复调用幂等（handler 数量不变）；
2. debug_log._get_logger() 的 handler 同参数且 propagate is False；
   log_event 写一条不抛；
3. 轮转模拟用例（计划 3.4-3，可选）跳过：TimedRotatingFileHandler 的
   rollover 判定依赖系统时钟，monkeypatch 环境敏感；computeRollover
   语义信任标准库（与计划 3.5-2 人工自测口径一致），故不做、不阻塞验收。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_log_rotation.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_log_rot_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app import logging_config  # noqa: E402
from app.dispatcher import debug_log  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_log_rot_test_")


def _assert_timed_handler(h: logging.Handler, *, name: str) -> None:
    """统一断言按日轮转参数（计划 3.6 验收口径）。"""
    assert isinstance(h, TimedRotatingFileHandler), f"{name} 不是 TimedRotatingFileHandler"
    assert h.when.upper() == "MIDNIGHT", f"{name} when={h.when!r}"
    # 注意：when="midnight" 时标准库把 interval 换算成秒存储（1 天=86400），
    # 构造参数 interval=1 不在实例属性上保留原值，故此处按换算后口径断言
    assert h.interval == 86400, f"{name} interval={h.interval!r}"
    assert h.backupCount == 30, f"{name} backupCount={h.backupCount!r}"
    assert h.utc is False, f"{name} utc={h.utc!r}（应本地时区）"
    enc = (h.encoding or "").lower().replace("_", "-")
    assert "utf-8" in enc or "utf8" in enc, f"{name} encoding={h.encoding!r}"


def test_setup_logging_timed_rotation_handlers():
    """root 三通道：控制台 + app.log + error.log，文件通道全部按日轮转。"""
    logging_config.setup_logging()
    root = logging.getLogger()

    file_handlers = [h for h in root.handlers
                     if isinstance(h, TimedRotatingFileHandler)]
    by_name = {Path(h.baseFilename).name: h for h in file_handlers}
    assert "app.log" in by_name, f"缺 app.log handler：{sorted(by_name)}"
    assert "error.log" in by_name, f"缺 error.log handler：{sorted(by_name)}"
    _assert_timed_handler(by_name["app.log"], name="app.log")
    _assert_timed_handler(by_name["error.log"], name="error.log")

    # 三通道分层结构不变：app.log DEBUG 起、error.log WARNING 起
    assert by_name["app.log"].level == logging.DEBUG
    assert by_name["error.log"].level == logging.WARNING

    # 控制台 StreamHandler 仍在（纯 StreamHandler，非文件 handler 子类）
    assert any(type(h) is logging.StreamHandler for h in root.handlers), \
        "控制台 StreamHandler 缺失"


def test_setup_logging_idempotent():
    """重复调用 setup_logging() 不重复添加 handler（数量不变）。"""
    logging_config.setup_logging()
    root = logging.getLogger()
    before = len(root.handlers)
    logging_config.setup_logging()
    logging_config.setup_logging()
    assert len(root.handlers) == before, \
        f"重复调用后 handler 数 {before} -> {len(root.handlers)}"


def test_debug_log_timed_rotation_and_propagate():
    """dispatcher.log 同为按日轮转，propagate=False 防线不变。"""
    lg = debug_log._get_logger()
    assert lg.propagate is False, "dispatcher_debug 不得上抛 root"
    assert lg.handlers, "dispatcher_debug 无 handler"
    _assert_timed_handler(lg.handlers[0], name="dispatcher.log")
    assert Path(lg.handlers[0].baseFilename).name == "dispatcher.log"


def test_debug_log_log_event_no_raise():
    """log_event 写一条不抛（日志绝不反过来弄挂调度循环）。"""
    debug_log.log_event("log_rotation_test", marker="阶段三", n=1)
    assert debug_log._LOG_FILE.exists(), "dispatcher.log 未创建"
