"""调度 Agent L2 操作记忆（会话间，跨重启持久化）。

L1 记忆（sessions.py）是进程内 dict，重启即丢——适合 pending_action / 短命对话历史。
但调度 Agent 还需要"跨会话"的长期记忆：上次在哪个工厂目录操作、最近改过哪些路径、
上一轮创建的批次 thread_id 是什么。这些信息在操作员关掉浏览器再打开时需要延续，
因此落到 SQLite（master.db 的 dispatcher_memory 表）。

设计要点：
- 按 session_id 分区：一个浏览器终端一份记忆，互不干扰；
- 写操作成功后自动更新：create_batch/rerun/submit_review 更新 last_thread_id，
  set_paths 更新 recent_paths，所有写操作追加 operation_summary；
- 对话结束自动摘要：由上层（agent_chat 回调）调用 record_operation 完成；
- 所有方法 try/except 包死，失败只打印警告——记忆是辅助设施，不能搞挂主流程。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from app.db.models import Base, DispatcherMemory
from app.db.session import get_session

logger = logging.getLogger(__name__)


def _ensure_table() -> None:
    """确保 dispatcher_memory 表存在。

    正常流程中 get_engine() 的 create_all 已覆盖全部 Base 子类模型，
    这里是兜底——万一表被手动 DROP 或旧库升级，不至于直接报错。
    """
    try:
        from app.db.session import get_engine
        Base.metadata.create_all(get_engine(), tables=[DispatcherMemory.__table__])
    except Exception as exc:
        logger.warning("dispatcher_memory 建表兜底失败: %s", exc)


def _fmt_ago(ts: float) -> str:
    """把 Unix 时间戳格式化为中文相对时间（'刚刚' / '3分钟前' / '2小时前'）。"""
    diff = time.time() - ts
    if diff < 60:
        return "刚刚"
    minutes = int(diff // 60)
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}小时前"
    days = int(hours // 24)
    return f"{days}天前"


class OperationMemory:
    """调度 Agent L2 操作记忆（会话间，跨重启持久化）。

    按 session_id 分区——每个浏览器终端独立一份记忆。写操作成功后自动更新，
    对话结束时由上层调用 record_operation 追加摘要。所有方法失败只记警告，
    不抛异常——记忆是辅助设施，不能影响主流程。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id

    def load(self) -> dict:
        """从 SQLite 加载记忆。不存在时返回空 dict。

        返回结构::

            {
                "last_thread_id": str | None,
                "last_factory": str | None,
                "recent_paths": [{path, category, updated_at}],      # 最近 3 条
                "operation_summary": [{tool, args_summary, result_summary, ts}],  # 最近 10 次
                "updated_at": float | None,
            }
        """
        try:
            _ensure_table()
            with get_session() as sess:
                row = sess.get(DispatcherMemory, self.session_id)
                if row is None:
                    return {
                        "last_thread_id": None,
                        "last_factory": None,
                        "recent_paths": [],
                        "operation_summary": [],
                        "updated_at": None,
                    }
                return {
                    "last_thread_id": row.last_thread_id,
                    "last_factory": row.last_factory,
                    "recent_paths": json.loads(row.recent_paths_json or "[]"),
                    "operation_summary": json.loads(row.operation_summary_json or "[]"),
                    "updated_at": row.updated_at.timestamp() if row.updated_at else None,
                }
        except Exception as exc:
            logger.warning("加载 session_id=%s 记忆失败: %s", self.session_id, exc)
            return {
                "last_thread_id": None,
                "last_factory": None,
                "recent_paths": [],
                "operation_summary": [],
                "updated_at": None,
            }

    def update(self, **kwargs) -> None:
        """更新记忆字段。

        kwargs 可包含：last_thread_id, last_factory, recent_paths, operation_summary。
        不存在则 INSERT，存在则 UPDATE。list 类型字段自动 JSON 序列化。
        """
        try:
            _ensure_table()
            field_map = {
                "last_thread_id", "last_factory", "recent_paths", "operation_summary",
            }
            payload = {k: v for k, v in kwargs.items() if k in field_map}
            if "recent_paths" in payload:
                payload["recent_paths_json"] = json.dumps(
                    payload.pop("recent_paths"), ensure_ascii=False,
                )
            if "operation_summary" in payload:
                payload["operation_summary_json"] = json.dumps(
                    payload.pop("operation_summary"), ensure_ascii=False,
                )
            payload["updated_at"] = datetime.now(timezone.utc)

            with get_session() as sess:
                row = sess.get(DispatcherMemory, self.session_id)
                if row is None:
                    row = DispatcherMemory(session_id=self.session_id, **payload)
                    sess.add(row)
                else:
                    for k, v in payload.items():
                        setattr(row, k, v)
                sess.commit()
        except Exception as exc:
            logger.warning("更新 session_id=%s 记忆失败: %s", self.session_id, exc)

    def record_operation(self, tool: str, args_summary: str, result_summary: str) -> None:
        """记录一次操作摘要（追加到 operation_summary，保留最近 10 次）。

        自动更新 updated_at。由上层在每次写操作完成 / 对话结束时调用。
        """
        try:
            mem = self.load()
            ops = mem["operation_summary"]
            ops.append({
                "tool": tool,
                "args_summary": args_summary,
                "result_summary": result_summary,
                "ts": time.time(),
            })
            # 保留最近 10 条，裁掉更旧的
            if len(ops) > 10:
                ops = ops[-10:]
            self.update(operation_summary=ops)
        except Exception as exc:
            logger.warning("记录操作摘要失败: %s", exc)

    def auto_update_after_write(self, tool: str, args: dict, result: dict) -> None:
        """写操作成功后自动更新记忆。

        规则：
        - create_batch / rerun / submit_review → 更新 last_thread_id（从 args 或 result 提取）
        - set_paths → 更新 recent_paths（追加到头部，保留最近 3 条）
        - 所有写操作 → record_operation（同时更新 updated_at）
        """
        try:
            mem = self.load()
            updates: dict = {}

            # ---- 更新 last_thread_id ----
            if tool in ("create_batch", "rerun", "submit_review"):
                thread_id = (
                    result.get("thread_id")
                    or args.get("thread_id")
                )
                if thread_id:
                    updates["last_thread_id"] = thread_id
                    mem["last_thread_id"] = thread_id

            # ---- 更新 last_factory（通用：有 factory 参数就记） ----
            factory = args.get("factory") or args.get("factory_name")
            if factory:
                updates["last_factory"] = factory
                mem["last_factory"] = factory

            # ---- 更新 recent_paths ----
            if tool == "set_paths":
                paths_dict = args.get("paths") or {}
                # paths_dict 格式：{"upstream_root": "/xxx", "downstream_file_path": "/yyy"}
                # 转换为 list of {path, category}
                new_paths = [
                    {"path": path_str, "category": category}
                    for category, path_str in paths_dict.items()
                    if isinstance(path_str, str)
                ]
                now_ts = time.time()
                existing = mem["recent_paths"]
                # 追加到头部，带去重（同 path 只留最新的）
                seen: set[str] = set()
                merged: list[dict] = []
                for p in new_paths:
                    path_str = p.get("path", "")
                    if path_str in seen:
                        continue
                    seen.add(path_str)
                    merged.append({
                        "path": path_str,
                        "category": p.get("category", ""),
                        "updated_at": now_ts,
                    })
                for p in existing:
                    if p.get("path") in seen:
                        continue
                    merged.append(p)
                updates["recent_paths"] = merged[:3]

            if updates:
                self.update(**updates)

            # ---- 所有写操作都记录 operation_summary ----
            args_summary = ", ".join(f"{k}={v}" for k, v in args.items())[:200]
            result_summary = str(result)[:200]
            self.record_operation(tool, args_summary, result_summary)
        except Exception as exc:
            logger.warning("写操作后自动更新记忆失败: %s", exc)

    def get_context_for_prompt(self) -> str:
        """生成注入 system prompt 的上下文字符串。

        例::

            "最近操作：3分钟前创建批次 ETD0725，工厂：中地。上次改路径：工厂文件夹=/xxx（5分钟前）。"

        无记忆时返回空字符串。
        """
        try:
            mem = self.load()
            lines: list[str] = []

            # ---- 最近操作 ----
            ops = mem["operation_summary"]
            if ops:
                latest = ops[-1]
                ago = _fmt_ago(latest["ts"])
                lines.append(f"最近操作：{ago}{latest['tool']}（{latest['args_summary']}）")

            # ---- 上次改路径 ----
            paths = mem["recent_paths"]
            if paths:
                latest_path = paths[0]
                ago = _fmt_ago(latest_path["updated_at"])
                factory_hint = f"，工厂：{mem['last_factory']}" if mem["last_factory"] else ""
                lines.append(
                    f"上次改路径：{latest_path['category']}={latest_path['path']}"
                    f"（{ago}{factory_hint}）"
                )

            # ---- 上次批次 / 审核 ----
            if mem["last_thread_id"]:
                lines.append(f"上次线程 ID：{mem['last_thread_id']}")

            return "。".join(lines) + "。" if lines else ""
        except Exception as exc:
            logger.warning("生成记忆上下文失败: %s", exc)
            return ""
