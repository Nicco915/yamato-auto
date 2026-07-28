# -*- coding: utf-8 -*-
"""对话式路径配置端到端实测（生产人工指令通道）。

场景（提取 mock、指令解析走真实 LLM）：
1. 故意用空目录作 upstream_root 启动 → Node2 未匹配 → 占位挂起；
2. 操作员对话「工厂文件在 /Users/nz/Downloads/yamato/96/工厂」→ LLM 解析+校验+预览；
3. 确认 → 写临时 .env（不动真 .env）+ 当前批次重跑 → 新 payload 命中中地文件夹；
4. 负例：不存在的路径拒绝；无关闲聊拒绝；非绝对路径拒绝。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 LLM_ENABLE_THINKING=0 python3 validation/chat_paths_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")  # 提取走 mock，只测对话改路径机制

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from app import agent_chat  # noqa: E402
from app.api import service  # noqa: E402

THREAD_ID = "INTEG-CHAT-PATHS"
EMPTY_DIR = Path(tempfile.mkdtemp(prefix="yamato_empty_"))
REAL_ROOT = "/Users/nz/Downloads/yamato/96/工厂"


def main() -> int:
    # 临时 .env：复制真实 .env，apply 写它而不动真文件
    tmp_env = Path(tempfile.mkdtemp(prefix="yamato_env_")) / ".env"
    shutil.copy2(APP_ROOT / ".env", tmp_env)
    before = tmp_env.read_text(encoding="utf-8")

    # ---- 1. 错路径启动 → 占位挂起 ----
    print("===== 1. 用空目录启动（模拟工厂文件夹位置不对）=====")
    r = service.run_until_interrupt(
        THREAD_ID, upstream_root=str(EMPTY_DIR), factory_filter=["山東中地"],
    )
    assert r["status"] == "pending_human_review", r
    first = r["review_data"]
    assert first["folder_path"] is None, "空目录下不应匹配到文件夹"
    assert all(i["status"] == "Error" for i in first["items"]), "占位条目应全 Error"
    print(f"  ✓ 如期挂起：folder_path=None，{len(first['items'])} 条占位（Error）")

    # ---- 2. 对话：LLM 解析 + 校验 + 预览 ----
    print("\n===== 2. 操作员对话：告知正确工厂目录 =====")
    resp = agent_chat.handle_message(
        f"搞错了，这一批的工厂文件都在 {REAL_ROOT} 下面", env_path=tmp_env,
    )
    assert resp["status"] == "pending_confirmation", resp
    paths = resp["action"]["paths"]
    assert paths.get("upstream_root") == REAL_ROOT, f"LLM 解析结果异常: {paths}"
    print(f"  ✓ 解析: {paths}")
    for line in resp["preview"]:
        print(f"  预览: {line}")

    # ---- 3. 确认 → 写 .env + 当前批次重跑 ----
    print("\n===== 3. 人工确认 → 应用 + 当前批次重跑 =====")
    applied = agent_chat.apply_paths(paths, thread_id=THREAD_ID, env_path=tmp_env)
    after = tmp_env.read_text(encoding="utf-8")
    assert f"UPSTREAM_ROOT={REAL_ROOT}" in after, ".env 未写入新路径"
    # 其他行（API key 等）一字不动
    for line in before.splitlines():
        if line.strip().startswith("UPSTREAM_ROOT="):
            continue
        assert line in after.splitlines(), f".env 原有行被改动: {line[:50]}"
    assert (tmp_env.parent / f"{tmp_env.name}.bak").exists(), "缺 .env.bak 备份"
    print("  ✓ .env 已更新（其余行原样保留，.bak 已备份）")

    rerun = applied["rerun"]
    assert rerun["status"] == "pending_human_review", rerun
    rd = rerun["review_data"]
    assert rd["folder_path"] == f"{REAL_ROOT}/中地", f"重跑后文件夹仍不对: {rd['folder_path']}"
    assert len(rd["items"]) == 14, f"重跑后应为 14 条 mock 数据: {len(rd['items'])}"
    assert not any(i["status"] == "Error" for i in rd["items"]), "重跑后不应再有占位 Error"
    print(f"  ✓ 当前批次已用新路径重跑：folder={rd['folder_path']}，{len(rd['items'])} 条 SKU")

    # ---- 4. 负例 ----
    print("\n===== 4. 负例 =====")
    bad = agent_chat.handle_message("把工厂目录改到 /nonexistent/ghost_dir_12345", env_path=tmp_env)
    # LLM 可能判 set_paths（路径不存在被校验拒）或 unknown，两种都不得执行
    assert bad["status"] == "rejected", f"不存在路径未被拒绝: {bad}"
    print(f"  ✓ 不存在的路径被拒绝: {bad['message'][:60]}")

    chat = agent_chat.handle_message("今天天气怎么样", env_path=tmp_env)
    assert chat["status"] == "rejected" and chat["action"] in ("chat", "unknown"), chat
    print(f"  ✓ 闲聊被拒绝: {chat['message'][:60]}")

    rel = agent_chat.validate_paths({"upstream_root": "96/工厂"})
    assert rel and "绝对路径" in rel[0]
    print(f"  ✓ 相对路径被纯 Python 校验拦截: {rel[0]}")

    evil = agent_chat.validate_paths({"api_key": "/etc/passwd"})
    assert evil and "未授权" in evil[0]
    print(f"  ✓ 白名单外配置项被拦截: {evil[0]}")

    print("\n🎉 对话式路径配置端到端全部通过！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
