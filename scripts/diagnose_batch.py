#!/usr/bin/env python3
"""批次状态诊断：打印 checkpoint 快照的关键字段（只读，不改任何数据）。

用于定位「状态栏与工厂卡片不一致」「批次卡住」类问题——直接展示
LangGraph 快照的 next/tasks/interrupts 与业务字段，与批次详情页
（_summarize_snapshot / get_batch_detail）的推导口径一一对照。

用法（在 app/ 目录下）：
    python3 scripts/diagnose_batch.py <thread_id> [<thread_id>...]
"""
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文工厂名直接 print 会乱码/报错；强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 老 Python/重定向场景防御
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.service import _config  # noqa: E402
from app.graph import get_graph  # noqa: E402


def diagnose(thread_id: str) -> None:
    graph = get_graph()
    snap = graph.get_state(_config(thread_id))
    print(f"===== {thread_id} =====")
    if not snap.values:
        print("批次不存在（checkpoint 无该 thread_id）")
        return
    values = snap.values

    interrupts = []
    for t in snap.tasks:
        for iv in (t.interrupts or []):
            v = iv.value or {}
            interrupts.append(v.get("factory_name") or "(无工厂名 payload)")
    print("next          :", list(snap.next) or "(空)")
    print("interrupts    :", interrupts or "(无)")
    print("derived status:", (
        "pending_review" if interrupts else
        "running" if snap.next else "completed"))

    req = values.get("downstream_requirements") or {}
    print("requirements  :", sorted(req.keys()))
    print("factory_filter:", values.get("factory_filter"))
    print("pending       :", values.get("pending_factories"))
    print("deferred      :", [e.get("factory_name") if isinstance(e, dict) else e
                              for e in (values.get("deferred_factories") or [])])
    cur = values.get("current_factory_data") or {}
    print("current       :", cur.get("factory_name"))
    print("validation    :", values.get("validation_status"))
    print("factory_outputs:", sorted((values.get("factory_outputs") or {}).keys()))
    print("final_output  :", values.get("final_output_path"))
    print()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python3 scripts/diagnose_batch.py <thread_id> [...]")
        return 1
    for tid in sys.argv[1:]:
        diagnose(tid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
