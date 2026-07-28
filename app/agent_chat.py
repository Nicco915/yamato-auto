"""提取 Agent 对话式路径配置（生产人工指令通道）。

2026-07-28 用户授权：生产环境下操作员可与提取 Agent 对话，允许它直接修改
路径配置。流程（LLM 只解析不做决策，决策永远属于人工）：

1. parse_instruction：LLM 把自然语言解析成结构化路径参数（白名单 3 个 key）；
2. validate_paths：纯 Python 校验——绝对路径 + 目录/文件必须存在，零容错；
3. preview_changes：生成「旧值 → 新值」预览，等人工确认；
4. apply_paths（confirm 后）：写回 .env（持久，先备份 .env.bak）+ 刷新运行时
   配置 + 当前批次带新路径从 Node1 重跑（scope 见用户答复：持久+当前批次）。

授权范围仅限路径三旋钮，其他任何配置修改请求一律拒绝：
- upstream_root        上游工厂文件夹根目录（目录）
- downstream_file_path 下游装箱表 xlsx（文件）
- gt_source            GT 基准文件（文件，validation 用）
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from dotenv import dotenv_values

from app.config import PROJECT_ROOT, get_settings

# 白名单：内部 key → (.env 变量名, 期望类型, 中文名)
ALLOWED_PATHS: dict[str, tuple[str, str, str]] = {
    "upstream_root": ("UPSTREAM_ROOT", "dir", "上游工厂文件夹根目录"),
    "downstream_file_path": ("DOWNSTREAM_FILE_PATH", "file", "下游装箱表"),
    "gt_source": ("GT_SOURCE", "file", "GT 基准文件"),
}

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_PARSE_PROMPT = """你是路径配置指令解析器，服务于供应链单证提取 Agent。
操作员会用自然语言要求修改路径配置。你只识别以下三类路径（其他一律不提取）：

- upstream_root：上游工厂文件夹根目录。操作员可能说：工厂文件夹/工厂目录/上游目录/工厂文件在…
- downstream_file_path：下游装箱表（xlsx 文件）。操作员可能说：下游表/装箱表/ContentsOfTheContainer/要填的表
- gt_source：GT 基准文件（ground truth 对照表）。操作员可能说：GT/gt文件/基准表/对照表

铁律：
1. 只提取操作员明确给出的绝对路径（以 / 开头的完整路径）；相对路径、猜的路径禁止输出；
2. 同一句话出现多个路径时，按语义各自归类，拿不准归类的一律不提取；
3. 与修改路径无关的内容（闲聊、提问、其他配置如 API key/模型/数据库）→ action=chat；
4. 想改路径但没给出明确绝对路径 → action=unknown，reply 里说明缺什么。

只输出 JSON：
{"action": "set_paths" | "unknown" | "chat",
 "paths": {"upstream_root": "...", "downstream_file_path": "...", "gt_source": "..."},
 "reply": "一句话复述你的理解（中文）"}
paths 里没有提取到的 key 不要出现。"""


# ---------------------------------------------------------------------------
# 第一步：LLM 解析（只解析，不做决策）
# ---------------------------------------------------------------------------

def parse_instruction(message: str) -> dict:
    """LLM 解析自然语言指令 → {action, paths, reply}。输出受白名单强约束。"""
    from app.extraction import llm_client  # 延迟 import：无 API key 时其余功能仍可用

    raw = llm_client.chat_completion(
        [
            {"role": "system", "content": _PARSE_PROMPT},
            {"role": "user", "content": message},
        ],
        source_file="agent_chat",
        max_tokens=1024,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "unknown", "paths": {},
                "reply": f"解析器输出异常，未执行任何操作（原文：{raw[:100]}）"}

    action = parsed.get("action") if parsed.get("action") in ("set_paths", "unknown", "chat") else "unknown"
    # 白名单强过滤：LLM 输出的非授权 key 一律丢弃（决策权在代码，不在模型）
    paths = {
        k: str(v).strip()
        for k, v in (parsed.get("paths") or {}).items()
        if k in ALLOWED_PATHS and isinstance(v, str) and v.strip()
    }
    if action == "set_paths" and not paths:
        action = "unknown"
    return {"action": action, "paths": paths,
            "reply": str(parsed.get("reply") or "")}


# ---------------------------------------------------------------------------
# 第二步：纯 Python 校验（零容错）
# ---------------------------------------------------------------------------

def validate_paths(paths: dict) -> list[str]:
    """校验路径合法性，返回错误列表（空=通过）。"""
    errors: list[str] = []
    for key, value in paths.items():
        if key not in ALLOWED_PATHS:
            errors.append(f"未授权的配置项：{key}（仅支持 {sorted(ALLOWED_PATHS)}）")
            continue
        _, kind, label = ALLOWED_PATHS[key]
        p = Path(value)
        if not p.is_absolute():
            errors.append(f"{label}：必须是绝对路径，收到 {value!r}")
        elif kind == "dir" and not p.is_dir():
            errors.append(f"{label}：目录不存在 {value}")
        elif kind == "file" and not p.is_file():
            errors.append(f"{label}：文件不存在 {value}")
    return errors


# ---------------------------------------------------------------------------
# 第三步：预览（旧值 → 新值）
# ---------------------------------------------------------------------------

def preview_changes(paths: dict, env_path: Path | None = None) -> list[str]:
    """对比 .env 当前值，生成人读的变更预览。"""
    env_path = env_path or DEFAULT_ENV_PATH
    current = dotenv_values(env_path) if env_path.exists() else {}
    lines = []
    for key, new in paths.items():
        env_key, _, label = ALLOWED_PATHS[key]
        old = current.get(env_key) or "（未设置）"
        marker = "不变" if old == new else "修改"
        lines.append(f"[{marker}] {label} {env_key}:\n    {old}\n -> {new}")
    return lines


# ---------------------------------------------------------------------------
# 第四步：应用（confirm 后）——写 .env + 刷运行时 + 当前批次重跑
# ---------------------------------------------------------------------------

def _upsert_env(env_path: Path, updates: dict[str, str]) -> None:
    """行级 upsert .env：已存在的 key 原地替换，不存在的追加到文件尾。

    其他行（注释/API key/空行）一字不动；写入前备份 .env.bak。
    """
    if env_path.exists():
        shutil.copy2(env_path, env_path.parent / f"{env_path.name}.bak")
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        lines.append("")
        lines.append("# ===== 业务路径（agent 对话修改）=====")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_paths(paths: dict, thread_id: str | None = None,
                env_path: Path | None = None) -> dict:
    """应用路径变更：.env 持久化 + 运行时刷新 +（可选）当前批次重跑。

    调用前必须已过 validate_paths；此处再校验一次（confirm 通道防线）。
    """
    errors = validate_paths(paths)
    if errors:
        raise ValueError("路径校验未通过：" + "；".join(errors))

    env_path = env_path or DEFAULT_ENV_PATH
    updates = {ALLOWED_PATHS[k][0]: v for k, v in paths.items()}
    _upsert_env(env_path, updates)

    # 运行时立即生效：环境变量优先于 .env（pydantic-settings 优先级），
    # 清 settings 缓存后新值即刻可用；.env 保证重启后仍生效
    for env_key, value in updates.items():
        os.environ[env_key] = value
    get_settings.cache_clear()

    result: dict = {"applied": updates, "env_file": str(env_path)}

    if thread_id:
        from app.api import service  # 延迟 import 避免环
        result["rerun"] = service.rerun_with_paths(
            thread_id,
            upstream_root=paths.get("upstream_root"),
            downstream_file_path=paths.get("downstream_file_path"),
        )
    return result


def handle_message(message: str, env_path: Path | None = None) -> dict:
    """对话主入口（确认前）：解析 → 校验 → 预览。"""
    parsed = parse_instruction(message)
    if parsed["action"] != "set_paths":
        return {"status": "rejected", "action": parsed["action"],
                "message": parsed["reply"] or "未识别为路径修改指令，未执行任何操作。"
                "支持的指令：修改 工厂文件夹 / 下游表 / GT 文件 的路径（需绝对路径）。"}

    errors = validate_paths(parsed["paths"])
    if errors:
        return {"status": "rejected", "action": "set_paths",
                "message": "路径校验未通过，未执行任何操作", "errors": errors,
                "parsed": parsed}

    return {
        "status": "pending_confirmation",
        "action": parsed,
        "preview": preview_changes(parsed["paths"], env_path),
        "message": "以上变更确认后，我将写入 .env 持久生效"
        "（携带 thread_id 时当前批次立即用新路径重跑）。",
    }


__all__ = ["parse_instruction", "validate_paths", "preview_changes",
           "apply_paths", "handle_message", "ALLOWED_PATHS"]
