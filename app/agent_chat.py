"""提取 Agent 对话式路径配置（生产人工指令通道）。

2026-07-28 用户授权：生产环境下操作员可与提取 Agent 对话，允许它直接修改
路径配置。流程（LLM 只解析不做决策，决策永远属于人工）：

1. parse_instruction：LLM 把自然语言解析成结构化路径参数（白名单 3 个 key）；
2. validate_paths：纯 Python 校验——绝对路径 + 同平台路径必须存在，零容错；
   异平台路径（如 macOS 网关上配 Windows 生产机路径）本机无法验证存在性，
   不硬拒，由 cross_platform_warnings 产出警告，靠人工确认环节兜底；
3. preview_changes：生成「旧值 → 新值」预览，等人工确认；
4. apply_paths（confirm 后）：写回 .env（持久，先备份 .env.bak）+ 刷新运行时
   配置 + 当前批次带新路径从 Node1 重跑（scope 见用户答复：持久+当前批次）。

授权范围仅限路径三旋钮，其他任何配置修改请求一律拒绝：
- upstream_root        上游工厂文件夹根目录（目录）
- downstream_file_path 下游装箱表 xlsx（文件）
- gt_source            GT 基准文件（文件，validation 用）

L1 会话记忆（2026-07-28）：操作员会分多轮补充信息（先给路径、后说明类别，
或反过来），无状态单轮解析必然失败。两层设计：
- 路线 A：session.history 随请求发给解析器，LLM 可跨轮合并上下文；
- 路线 B：unclassified 槽位由代码持有（唯一事实来源），LLM 没合并时
  代码用 category_hint + 待归类路径兜底。合并结果仍须过 validate_paths
  + 人工确认——记忆只负责填充槽位，绝不绕过任何一道防线。
存储为进程内 dict：重启即丢（可接受，确认前状态本就短命），需要跨重启
持久时再迁 app/db。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
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

_PARSE_PROMPT = r"""你是路径配置指令解析器，服务于供应链单证提取 Agent。
操作员会用自然语言要求修改路径配置。你会看到之前的对话历史（如有），
操作员可能分多轮补充信息（先给路径、后说明类别，或反过来）。
你只识别以下三类路径（其他一律不提取）：

- upstream_root：上游工厂文件夹根目录。操作员可能说：工厂文件夹/工厂目录/上游目录/工厂文件在…
- downstream_file_path：下游装箱表（xlsx 文件）。操作员可能说：下游表/装箱表/ContentsOfTheContainer/要填的表
- gt_source：GT 基准文件（ground truth 对照表）。操作员可能说：GT/gt文件/基准表/对照表

铁律：
1. 只提取操作员明确给出的绝对路径，以下两种风格都算绝对路径：
   - Unix 风格：以 / 开头（如 /Users/nas/工厂）；
   - Windows 风格：盘符开头（如 D:\factory\工厂 或 D:/factory/工厂），或 UNC 路径（\\NAS\share\…）；
   相对路径、猜的路径禁止输出；路径原文照抄不得改写，
   Windows 反斜杠在 JSON 里必须按 JSON 规则转义（一个 \ 写成 \\）；
2. 同一句话出现多个路径时，按语义各自归类，拿不准归类的一律不提取；
3. 与修改路径无关的内容（闲聊、提问、其他配置如 API key/模型/数据库）→ action=chat；
4. 想改路径但没给出明确绝对路径 → action=unknown，reply 里说明缺什么；
5. 提取到路径时，reply 里必须注明识别到的路径风格（Unix / Windows 盘符 / UNC），
   方便操作员在确认预览时发现「本意 Unix 但手滑写成反斜杠」或「UNC 被误判成盘符」两类事故；
6. 多轮合并：历史中有未归类的绝对路径，本轮操作员说明了类别（如"这是下游装箱表"
   或"刚才那个是工厂目录"），把历史中的路径原文填入 paths 对应 key，action=set_paths；
   反向同理：历史已说明类别，本轮补了绝对路径，也合并填入；
7. 只给出类别、本轮和历史都没有可用绝对路径时 → action=unknown，
   category_hint 填对应 key，reply 说明缺绝对路径；
8. 本轮提到的绝对路径若无法归类（规则 2/6 都不适用）→ 原文放入 unclassified
   数组，action=unknown，reply 里询问它属于哪一类。

只输出 JSON：
{"action": "set_paths" | "unknown" | "chat",
 "paths": {"upstream_root": "...", "downstream_file_path": "...", "gt_source": "..."},
 "category_hint": "upstream_root" | "downstream_file_path" | "gt_source",
 "unclassified": ["..."],
 "reply": "一句话复述你的理解（中文）"}
paths / category_hint / unclassified 没有内容时不要出现对应字段。"""


# ---------------------------------------------------------------------------
# L1 会话记忆：进程内会话存储
# ---------------------------------------------------------------------------

_SESSION_TTL_SEC = 2 * 3600   # 会话闲置 2 小时过期
_SESSION_MAX = 500            # 会话总量上限（淘汰最旧）
_HISTORY_MAX_TURNS = 10       # 发给 LLM 的最大历史轮数（一轮 = user + assistant）


class _ChatSession:
    """单会话状态：history 供路线 A（LLM 跨轮合并），unclassified 供路线 B（代码兜底）。"""

    __slots__ = ("history", "unclassified", "updated_at")

    def __init__(self) -> None:
        self.history: list[dict] = []        # {"role": "user"/"assistant", "content": str}
        self.unclassified: list[str] = []    # 操作员给过但尚未归类的绝对路径
        self.updated_at: float = time.time()


_SESSIONS: dict[str, _ChatSession] = {}
_SESSIONS_LOCK = threading.Lock()


def _get_session(session_id: str) -> _ChatSession:
    """取会话（不存在则新建），惰性清理过期会话并控制总量。

    锁只保护 dict 读写；返回的 session 在锁外被修改（含 LLM 调用期间），
    最坏情况是并发同会话丢一条历史，不影响安全——校验与确认是无状态的。
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
            sess = _SESSIONS[session_id] = _ChatSession()
        sess.updated_at = now
        return sess


def _record_turn(session: _ChatSession, user_msg: str, agent_msg: str) -> None:
    """把一轮对话写入历史，超出上限裁掉最旧的。"""
    session.history.append({"role": "user", "content": user_msg})
    session.history.append({"role": "assistant", "content": agent_msg})
    excess = len(session.history) - _HISTORY_MAX_TURNS * 2
    if excess > 0:
        del session.history[:excess]


# ---------------------------------------------------------------------------
# 第一步：LLM 解析（只解析，不做决策）
# ---------------------------------------------------------------------------

def parse_instruction(message: str, history: list[dict] | None = None) -> dict:
    """LLM 解析自然语言指令 → {action, paths, category_hint, unclassified, reply}。

    输出受白名单强约束；history 为最近若干轮对话（路线 A），供 LLM 跨轮合并
    「第 1 轮路径 + 第 2 轮类别」这类分轮补充的指令。
    """
    from app.extraction import llm_client  # 延迟 import：无 API key 时其余功能仍可用

    messages = [{"role": "system", "content": _PARSE_PROMPT}]
    messages.extend((history or [])[-_HISTORY_MAX_TURNS * 2:])
    messages.append({"role": "user", "content": message})
    raw = llm_client.chat_completion(
        messages,
        source_file="agent_chat",
        max_tokens=1024,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "unknown", "paths": {}, "category_hint": None,
                "unclassified": [],
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
    hint = parsed.get("category_hint")
    category_hint = hint if hint in ALLOWED_PATHS else None
    # 待归类路径同样只收绝对路径（防线与 paths 一致）
    unclassified = [
        p for p in (str(v).strip() for v in (parsed.get("unclassified") or []))
        if p and _is_absolute_path(p)
    ]
    return {"action": action, "paths": paths, "category_hint": category_hint,
            "unclassified": unclassified,
            "reply": str(parsed.get("reply") or "")}


def _merge_with_session(parsed: dict, session: _ChatSession) -> dict:
    """路线 B：代码侧槽位合并（LLM 没按历史合并时的兜底）。

    - category_hint + 唯一一条待归类路径 → 合并为 set_paths；
    - 多条待归类路径时不猜，保持 unknown 让操作员指明；
    - 本轮新提到的未归类路径入槽；已归类的路径移出槽位。
    返回 {"action", "paths", "reply"}。
    """
    action = parsed["action"]
    paths = dict(parsed["paths"])
    reply = parsed["reply"]

    hint = parsed.get("category_hint")
    # 白名单防线（与 parse 层双重保险）：非授权 key 绝不触发合并
    if hint in ALLOWED_PATHS and hint not in paths and len(session.unclassified) == 1:
        paths[hint] = session.unclassified[0]
        label = ALLOWED_PATHS[hint][2]
        reply = f"已将上文路径 {paths[hint]} 归类为{label}。" + (reply or "")
        if action == "unknown":
            action = "set_paths"

    for p in parsed.get("unclassified", []):
        # 只收绝对路径（与 parse 层双重保险）
        if p and _is_absolute_path(p) \
                and p not in paths.values() and p not in session.unclassified:
            session.unclassified.append(p)
    session.unclassified = [p for p in session.unclassified
                            if p not in paths.values()]
    return {"action": action, "paths": paths, "reply": reply}


# ---------------------------------------------------------------------------
# 第二步：纯 Python 校验（零容错）
# ---------------------------------------------------------------------------

def _is_absolute_path(value: str) -> bool:
    """跨平台绝对路径判定（与运行平台解耦）。

    同时认可三类（macOS 上配置 Windows 生产机路径的场景必需）：
    - Unix 风格：/ 开头（含 // 开头）；
    - Windows 盘符：X:\\ 或 X:/（盘符大小写不限）；
    - Windows UNC：\\\\server\\share 开头。
    注意：本函数只判格式；存在性校验（is_dir/is_file）只能在本机执行，
    异平台路径由 _is_cross_platform 识别后跳过存在性校验（见 validate_paths）。
    """
    if value.startswith("/"):
        return True
    if value.startswith("\\\\"):
        return True
    return (len(value) >= 3 and value[0].isalpha()
            and value[1] == ":" and value[2] in "\\/")


def _path_style(value: str) -> str:
    """路径风格分类：windows_drive / unc / unix / relative。"""
    if value.startswith("\\\\"):
        return "unc"
    if (len(value) >= 3 and value[0].isalpha()
            and value[1] == ":" and value[2] in "\\/"):
        return "windows_drive"
    if value.startswith("/"):
        return "unix"
    return "relative"


# 路径风格 → 中文名（用于 warnings 文案）
_STYLE_LABELS = {"windows_drive": "Windows 盘符", "unc": "UNC", "unix": "Unix"}


def _is_cross_platform(style: str) -> bool:
    """路径风格与运行平台不一致时视为异平台（本机无法验证存在性）。

    - Windows 盘符 / UNC 路径运行在非 Windows（macOS/Linux 网关）上；
    - Unix 路径运行在 Windows 上。
    """
    if style in ("windows_drive", "unc"):
        return sys.platform != "win32"
    if style == "unix":
        return sys.platform == "win32"
    return False


def validate_paths(paths: dict) -> list[str]:
    """校验路径合法性，返回硬错误列表（空=通过）。

    错误语义分三类：
    - 非绝对路径 → 硬错误「必须是绝对路径」；
    - 同平台绝对路径但本机不存在 → 硬错误「目录/文件不存在」；
    - 异平台绝对路径 → 本机无法验证存在性，跳过 is_dir/is_file，不算硬错误
      （由 cross_platform_warnings 产出警告，靠 pending_confirmation 人工兜底）。
    """
    errors: list[str] = []
    for key, value in paths.items():
        if key not in ALLOWED_PATHS:
            errors.append(f"未授权的配置项：{key}（仅支持 {sorted(ALLOWED_PATHS)}）")
            continue
        _, kind, label = ALLOWED_PATHS[key]
        p = Path(value)
        if not _is_absolute_path(value):
            errors.append(f"{label}：必须是绝对路径，收到 {value!r}")
        elif _is_cross_platform(_path_style(value)):
            continue  # 异平台路径：本机无法验证存在性，不硬拒
        elif kind == "dir" and not p.is_dir():
            errors.append(f"{label}：目录不存在 {value}")
        elif kind == "file" and not p.is_file():
            errors.append(f"{label}：文件不存在 {value}")
    return errors


def cross_platform_warnings(paths: dict) -> list[str]:
    """对每个异平台路径产出中文警告（本机无法验证存在性，提示人工核实目标机）。"""
    local = "Windows" if sys.platform == "win32" else "macOS/Linux"
    warnings: list[str] = []
    for key, value in paths.items():
        if key not in ALLOWED_PATHS:
            continue
        style = _path_style(value)
        if not _is_cross_platform(style):
            continue
        label = ALLOWED_PATHS[key][2]
        warnings.append(
            f"{label}为 {_STYLE_LABELS[style]}路径，本机（{local}）无法验证存在性，"
            f"请确认该路径在目标机上存在后再确认：{value}"
        )
    return warnings


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

    # 审核页白名单立即跟随新 upstream_root（W1：不等下一请求的懒刷新；
    # 延迟 import 避免环——review.router 的批次兜底会反向 import service）
    if "upstream_root" in paths:
        from app.review import router as review_router
        review_router.refresh_review_roots()

    result: dict = {"applied": updates, "env_file": str(env_path)}
    # 异平台路径天然通过校验，但须把警告带给操作员（提示核实目标机）
    warnings = cross_platform_warnings(paths)
    if warnings:
        result["warnings"] = warnings

    if thread_id:
        from app.api import service  # 延迟 import 避免环
        result["rerun"] = service.rerun_with_paths(
            thread_id,
            upstream_root=paths.get("upstream_root"),
            downstream_file_path=paths.get("downstream_file_path"),
        )
    return result


def handle_message(message: str, env_path: Path | None = None,
                   session_id: str | None = None) -> dict:
    """对话主入口（确认前）：解析（带历史）→ 槽位合并 → 校验 → 预览。

    session_id 提供时启用 L1 会话记忆；缺省为临时会话（行为与旧无状态版一致）。
    """
    session = _get_session(session_id) if session_id else _ChatSession()
    parsed = parse_instruction(message, history=session.history)
    merged = _merge_with_session(parsed, session)
    action, paths, reply = merged["action"], merged["paths"], merged["reply"]

    if action != "set_paths":
        text = reply or ("未识别为路径修改指令，未执行任何操作。"
                         "支持的指令：修改 工厂文件夹 / 下游表 / GT 文件 的路径（需绝对路径）。")
        # 有待归类路径时主动提醒，引导操作员一句话补类别
        if session.unclassified:
            text += (f"\n（我记得你给过路径：{'、'.join(session.unclassified)}"
                     f"，告诉我它属于哪一类即可——工厂文件夹 / 下游装箱表 / GT 基准文件）")
        _record_turn(session, message, text)
        result = {"status": "rejected", "action": action, "message": text}
        if session_id:
            result["session_id"] = session_id
        return result

    errors = validate_paths(paths)
    if errors:
        _record_turn(session, message, "路径校验未通过：" + "；".join(errors))
        result = {"status": "rejected", "action": "set_paths",
                  "message": "路径校验未通过，未执行任何操作", "errors": errors,
                  "parsed": {"action": action, "paths": paths, "reply": reply}}
        if session_id:
            result["session_id"] = session_id
        return result

    warnings = cross_platform_warnings(paths)
    text = ("以上变更确认后，我将写入 .env 持久生效"
            "（携带 thread_id 时当前批次立即用新路径重跑）。")
    if warnings:
        text += "注意：存在异平台路径，本机无法验证其存在性，请核实目标机上路径有效后再确认。"
    _record_turn(session, message, reply or text)
    result = {
        "status": "pending_confirmation",
        "action": {"action": "set_paths", "paths": paths, "reply": reply},
        "preview": preview_changes(paths, env_path),
        "message": text,
    }
    if warnings:
        result["warnings"] = warnings
    if session_id:
        result["session_id"] = session_id
    return result


def record_apply(session_id: str | None, paths: dict) -> None:
    """confirm 执行后调用：确认结果记入历史，已应用的路径移出待归类槽位。"""
    if not session_id:
        return
    session = _get_session(session_id)
    applied = "、".join(f"{ALLOWED_PATHS[k][2]}={v}"
                        for k, v in paths.items() if k in ALLOWED_PATHS)
    _record_turn(session, "[确认执行]", f"已确认并应用：{applied}")
    session.unclassified = [p for p in session.unclassified
                            if p not in paths.values()]


__all__ = ["parse_instruction", "validate_paths", "cross_platform_warnings",
           "preview_changes", "apply_paths", "handle_message", "record_apply",
           "ALLOWED_PATHS"]
