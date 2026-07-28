# -*- coding: utf-8 -*-
"""对话式路径配置端到端实测（生产人工指令通道）。

场景（提取 mock、指令解析走真实 LLM）：
1. 故意用空目录作 upstream_root 启动 → Node2 未匹配 → 占位挂起；
2. 操作员对话「工厂文件在 /Users/nz/Downloads/yamato/96/工厂」→ LLM 解析+校验+预览；
3. 确认 → 写临时 .env（不动真 .env）+ 当前批次重跑 → 新 payload 命中中地文件夹；
4. 负例：不存在的路径拒绝；无关闲聊拒绝；非绝对路径拒绝；
5. Windows 路径正例：盘符/UNC 在 macOS 上属异平台路径——validate 不硬拒，
   cross_platform_warnings 产出警告，handle_message 进 pending_confirmation 并带 warnings。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 LLM_ENABLE_THINKING=0 python3 validation/chat_paths_test.py
  # 换机器跑时：YAMATO_TEST_REAL_ROOT="D:\factory\工厂" python3 validation/chat_paths_test.py
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
# 真实工厂目录：可用环境变量覆盖（Windows 机上跑时改成盘符路径），缺省保持 macOS 现状
REAL_ROOT = os.environ.get("YAMATO_TEST_REAL_ROOT", "/Users/nz/Downloads/yamato/96/工厂")


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

    # ---- 5. Windows 路径正例：异平台路径不硬拒，由 warnings + 人工确认兜底 ----
    print("\n===== 5. Windows 路径正例（盘符 / UNC / LLM 端到端）=====")
    win = agent_chat.validate_paths({"upstream_root": r"D:\factory\工厂"})
    assert win == [], f"异平台盘符路径不应产生硬错误: {win}"
    win_warn = agent_chat.cross_platform_warnings({"upstream_root": r"D:\factory\工厂"})
    assert len(win_warn) == 1 and "Windows 盘符" in win_warn[0], win_warn
    print(f"  ✓ 盘符路径无硬错误，警告: {win_warn[0]}")

    unc = agent_chat.validate_paths({"upstream_root": r"\\NAS\share\工厂"})
    assert unc == [], f"异平台 UNC 路径不应产生硬错误: {unc}"
    unc_warn = agent_chat.cross_platform_warnings({"upstream_root": r"\\NAS\share\工厂"})
    assert len(unc_warn) == 1 and "UNC" in unc_warn[0], unc_warn
    print(f"  ✓ UNC 路径无硬错误，警告: {unc_warn[0]}")

    # macOS 现状回归：本机真实路径通过校验且无 warnings
    local = agent_chat.validate_paths({"upstream_root": REAL_ROOT})
    assert local == [], f"本机真实路径应通过校验: {local}"
    assert agent_chat.cross_platform_warnings({"upstream_root": REAL_ROOT}) == []
    print(f"  ✓ 本机真实路径 {REAL_ROOT} 校验通过且无 warnings")

    # LLM 端到端：新 prompt 应正常提取 Windows 路径；异平台不再硬拒 →
    # pending_confirmation 且带 warnings 字段（依赖 LLM 解析，实跑时需真实 API key）
    win_msg = agent_chat.handle_message(r"这一批工厂文件都在 D:\factory\工厂 下面", env_path=tmp_env)
    assert win_msg["status"] == "pending_confirmation", win_msg
    win_path = win_msg["action"]["paths"].get("upstream_root", "")
    assert win_path.upper().startswith("D:") and "factory" in win_path, \
        f"LLM 未正确提取 Windows 路径: {win_msg['action']['paths']}"
    assert win_msg.get("warnings") and any("Windows 盘符" in w for w in win_msg["warnings"]), win_msg
    assert "异平台" in win_msg["message"], win_msg["message"]
    print(f"  ✓ LLM 正确提取 Windows 路径 {win_path!r}，进入待确认并带 warnings")

    # ---- 6. L1 会话记忆：多轮合并（先给路径、后补类别）----
    print("\n===== 6. L1 会话记忆：多轮合并 =====")
    # 6a. 纯 Python 槽位合并（路线 B，不依赖 LLM）：
    # category_hint + 唯一待归类路径 → set_paths，且路径移出待归类槽位
    sess = agent_chat._get_session("UT-MERGE")
    sess.unclassified.append(REAL_ROOT)
    merged = agent_chat._merge_with_session(
        {"action": "unknown", "paths": {}, "reply": "",
         "category_hint": "upstream_root", "unclassified": []},
        sess,
    )
    assert merged["action"] == "set_paths", merged
    assert merged["paths"]["upstream_root"] == REAL_ROOT, merged
    assert sess.unclassified == [], "已归类的路径应移出待归类槽位"
    print(f"  ✓ 代码侧槽位合并（路线 B）: {merged['paths']}")

    # 6b. 多条待归类路径时不猜，保持 unknown
    sess2 = agent_chat._get_session("UT-MERGE-MULTI")
    sess2.unclassified.extend(["/a", "/b"])
    merged2 = agent_chat._merge_with_session(
        {"action": "unknown", "paths": {}, "reply": "",
         "category_hint": "gt_source", "unclassified": []},
        sess2,
    )
    assert merged2["action"] == "unknown" and not merged2["paths"], merged2
    print("  ✓ 多条待归类路径时保持 unknown，不瞎猜")

    # 6c. 白名单防线：category_hint / unclassified 非法值被过滤
    sess3 = agent_chat._get_session("UT-MERGE-EVIL")
    sess3.unclassified.append("/x")
    merged3 = agent_chat._merge_with_session(
        {"action": "unknown", "paths": {}, "reply": "",
         "category_hint": "api_key", "unclassified": ["相对路径", "/ok"]},
        sess3,
    )
    assert merged3["action"] == "unknown" and not merged3["paths"], merged3
    assert sess3.unclassified == ["/x", "/ok"], sess3.unclassified
    print("  ✓ 非法 category_hint 不触发合并，非绝对路径不入槽（由 parse 层过滤同理）")

    # 6d. LLM 端到端多轮：先给无类别线索的裸路径（临时目录，路径名不含
    # 工厂/装箱等词），再补类别，应合并为 set_paths（路线 A 或 B 达成均可）
    sid = "UT-SESSION-MEMORY"
    r1 = agent_chat.handle_message(str(EMPTY_DIR), env_path=tmp_env, session_id=sid)
    assert r1["status"] == "rejected", f"裸路径不应直接进预览: {r1}"
    assert r1.get("session_id") == sid, r1
    print(f"  ✓ 第 1 轮（裸路径）: {r1['message'][:60]}")

    r2 = agent_chat.handle_message("刚才那个是工厂文件夹", env_path=tmp_env, session_id=sid)
    assert r2["status"] == "pending_confirmation", f"第 2 轮应合并出 set_paths: {r2}"
    assert r2["action"]["paths"].get("upstream_root") == str(EMPTY_DIR), r2["action"]["paths"]
    print(f"  ✓ 第 2 轮（补类别）合并成功: {r2['action']['paths']}")

    hist = agent_chat._get_session(sid).history
    assert len(hist) >= 4, f"会话历史应含两轮对话: {len(hist)}"
    print(f"  ✓ 会话历史已累积 {len(hist)} 条")

    # 6e. 无 session_id 时行为与旧无状态版一致（临时会话不保留）
    r3 = agent_chat.handle_message(str(EMPTY_DIR), env_path=tmp_env)
    assert r3["status"] == "rejected" and "session_id" not in r3, r3
    r4 = agent_chat.handle_message("刚才那个是工厂文件夹", env_path=tmp_env)
    assert r4["status"] == "rejected", f"无 session 不应能合并: {r4}"
    print("  ✓ 缺省 session_id 时保持无状态行为（向后兼容）")

    print("\n🎉 对话式路径配置端到端全部通过！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
