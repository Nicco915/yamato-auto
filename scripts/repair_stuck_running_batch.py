#!/usr/bin/env python3
"""修复「卡在运行中」的批次（reopen 快照回写事故，2026-08-26）。

背景：旧版 apply_reopen_payload 回写 factory_outputs 快照时
update_state(as_node=NODE6)，update_state 会按 as_node 的出边重算
next——Node6 的条件边重路由出 next=(NODE7,)，但没有任何代码继续
stream(None)，批次从此永远显示「进行中」。新版已改锚 NODE7（终点
节点，出边只有 →END，不产生 next）。

本脚本修复已被卡住的批次：把锚点改挂到 NODE7 重放一次空更新，
next 即归空，批次回到「已完成」。

用法（在 app/ 目录下）：
  python3 scripts/repair_stuck_running_batch.py <thread_id> [<thread_id>...]

安全检查（任一不满足则跳过该批次，不做任何修改）：
- 批次存在且 next 非空、无挂起 interrupt（确属「卡住」形态）；
- pending_factories 与 deferred_factories 均为空（确属已跑完，而非
  真的还有工厂待处理——那种情况请用「补充工厂」）；
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.service import _config  # noqa: E402
from app.graph import NODE7, get_graph  # noqa: E402


def repair(thread_id: str) -> bool:
    graph = get_graph()
    cfg = _config(thread_id)
    snap = graph.get_state(cfg)
    if not snap.values:
        print(f"[跳过] {thread_id}: 批次不存在")
        return False
    if not snap.next:
        print(f"[跳过] {thread_id}: next 已为空（未卡住），无需修复")
        return False
    if any(t.interrupts for t in snap.tasks):
        print(f"[跳过] {thread_id}: 正挂起审核中（非卡住形态），请勿修复")
        return False
    values = snap.values
    if (values.get("pending_factories") or values.get("deferred_factories")):
        print(f"[跳过] {thread_id}: 仍有待处理工厂"
              f"（pending={values.get('pending_factories')}, "
              f"deferred={values.get('deferred_factories')}），"
              f"请走「补充工厂」而非本脚本")
        return False

    graph.update_state(cfg, {}, as_node=NODE7)
    after = graph.get_state(cfg)
    if after.next:
        print(f"[失败] {thread_id}: 修复后 next 仍非空: {after.next}")
        return False
    print(f"[成功] {thread_id}: next 已清空，批次恢复「已完成」")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ok = all(repair(tid) for tid in sys.argv[1:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
