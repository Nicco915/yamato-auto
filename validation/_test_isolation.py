# -*- coding: utf-8 -*-
"""validation 测试隔离公共助手（血泪红线，2026-08-02）。

【红线背景】app/extraction/llm_client.py 在 import 时执行
load_dotenv(override=True)，会把测试脚本在 import 前预设的
CHECKPOINT_DB_PATH / MASTER_DB_PATH 等环境变量**覆盖回 .env 真实路径**
——已因此截断过真实 checkpoints.db。

【铁律用法】先 import 全部 app 模块，**然后**调用 isolate_to_tmp：
    from _test_isolation import isolate_to_tmp
    TMP = isolate_to_tmp("yamato_xxx_test_")
isolate_to_tmp 会：设置临时 env → get_settings.cache_clear() →
断言 get_settings() 确实指向临时目录且不等于真实库路径（守卫），
最后才把 TMP 交给用例。graph/engine 都是惰性单例，首次调用才建连接，
此刻清缓存重建 Settings 即可让 db 全部指向临时目录。

绝不触碰的真实文件（守卫断言目标）：
- app/data/checkpoints.db
- app/data/master.db
- data/sessions/（SESSIONS_DIR 同步 patch 到临时目录）
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
REAL_CHECKPOINT_DB = (APP_ROOT / "app" / "data" / "checkpoints.db").resolve()
REAL_MASTER_DB = (APP_ROOT / "app" / "data" / "master.db").resolve()
REAL_SESSIONS_DIR = (APP_ROOT / "data" / "sessions").resolve()
REAL_ALIAS_MAP = APP_ROOT / "app" / "alias_map.json"


def patch_sessions_dir(tmp: Path) -> Path:
    """把提取会话目录（写 sessions/{工厂}.json）指向临时目录。

    SESSIONS_DIR 是 app.extraction.session 的模块级常量（不读 settings），
    service 又 from-import 了一份引用，两处都要 patch。模块未 import 时
    跳过（该测试根本不走提取线）。
    """
    sess_dir = tmp / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    try:
        from app.extraction import session as _es
        _es.SESSIONS_DIR = sess_dir
    except ImportError:
        pass
    try:
        from app.api import service as _svc
        _svc.SESSIONS_DIR = sess_dir
    except ImportError:
        pass
    return sess_dir


def isolate_to_tmp(
    prefix: str,
    *,
    alias_map_copy: bool = False,
    patch_sessions: bool = True,
    extra_env: dict[str, str] | None = None,
) -> Path:
    """把 checkpoint/master/output（可选 alias/sessions）隔离到临时目录。

    必须在 import 全部 app 模块之后调用（llm_client 的 load_dotenv
    override 已在 import 时执行完毕，此刻重设 env 才有效）。

    - alias_map_copy=True：把真实 alias_map.json 复制到临时目录并指向之
      （匹配行为与生产一致，读写都不碰真文件）；
    - patch_sessions=True：SESSIONS_DIR 两处引用 patch 到临时目录；
    - extra_env：追加/覆盖其他环境变量（如 UPSTREAM_ROOT）。

    返回临时目录 Path。守卫断言失败直接 AssertionError（宁可 FAIL
    也不碰生产库）。
    """
    from app.config import get_settings

    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    env = {
        "CHECKPOINT_DB_PATH": str(tmp / "checkpoints.db"),
        "MASTER_DB_PATH": str(tmp / "master.db"),
        "OUTPUT_DIR": str(tmp / "output"),
    }
    if alias_map_copy:
        dst = tmp / "alias_map.json"
        if REAL_ALIAS_MAP.exists():
            shutil.copy2(REAL_ALIAS_MAP, dst)
        else:
            dst.write_text("{}\n", encoding="utf-8")
        env["ALIAS_MAP_PATH"] = str(dst)
    env.update(extra_env or {})
    for k, v in env.items():
        os.environ[k] = v

    get_settings.cache_clear()
    s = get_settings()

    # ---- 真实库路径断言守卫（血泪红线）----
    ckpt = s.checkpoint_db_abs.resolve()
    master = s.master_db_abs.resolve()
    assert ckpt != REAL_CHECKPOINT_DB, \
        f"checkpoint_db 仍指向真实库: {ckpt}（隔离失败，中止）"
    assert master != REAL_MASTER_DB, \
        f"master_db 仍指向真实库: {master}（隔离失败，中止）"
    for name, p in (("checkpoint_db", ckpt), ("master_db", master),
                    ("output_dir", s.output_dir_abs.resolve())):
        assert str(p).startswith(str(tmp.resolve())), \
            f"{name} 未隔离到临时目录: {p}"
    if patch_sessions:
        sess_dir = patch_sessions_dir(tmp)
        assert sess_dir.resolve() != REAL_SESSIONS_DIR
    return tmp
