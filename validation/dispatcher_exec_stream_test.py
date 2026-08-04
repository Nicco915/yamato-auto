# -*- coding: utf-8 -*-
"""W4a 执行级节点进度测试（exec_progress SSE + on_progress 钩子）。

覆盖：
1. 流式 confirm：POST /api/v1/dispatcher/chat/stream（confirm=true）事件序含
   exec_progress（带 node/factory/done/total/message），末事件为 applied
   （事件名兼容），批次已建；
2. run_until_interrupt(on_progress) collector 收到 node1/node2 事件
   （装箱单解析完成 / 开始处理 X(i/N)）；
3. on_progress 抛异常跑图不受影响（进度是辅助设施，绝不阻塞主流程）。

隔离（血泪红线）：checkpoint/master db、output、sessions 全部指向临时目录
（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_exec_stream_test.py
  DISPATCHER_ENGINE=react EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/dispatcher_exec_stream_test.py

双引擎可跑：case 1 经 SSE 端点（handle_message 按 DISPATCHER_ENGINE
分流），剧本经 _dual_engine.set_scripts 同注两条 mock 通道；
case 2/3 直调 service.run_until_interrupt，与引擎无关。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import dispatcher  # noqa: E402
from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402

from _dual_engine import set_scripts  # noqa: E402
from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_stream_test_", alias_map_copy=True)

client = TestClient(app)

DOWNSTREAM = os.environ.get(
    "YAMATO_TEST_DOWNSTREAM",
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
)
EMPTY_UPSTREAM = TMP / "empty_upstream"
EMPTY_UPSTREAM.mkdir(exist_ok=True)


def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def case_1_stream_confirm_events() -> None:
    """流式 confirm：exec_progress 事件序 + 末事件 applied。"""
    tid = f"W4A-STREAM-{int(time.time()*1000) % 100000}"
    sid = f"W4A-SID-{int(time.time()*1000)}"
    set_scripts([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid,
                                  "downstream_file_path": DOWNSTREAM,
                                  "upstream_root": str(EMPTY_UPSTREAM),
                                  "factory_filter": ["山東中地"]}}]},
    ])
    # 轮1：发起 → pending_confirmation（done 事件收尾）
    r = client.post("/api/v1/dispatcher/chat/stream",
                    json={"message": f"发起批次 {tid}", "session_id": sid})
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    assert events, f"应至少有 done 事件: {r.text[:500]}"
    assert events[-1]["type"] == "done", events[-1]
    assert events[-1]["status"] == "pending_confirmation", events[-1]

    # 轮2：confirm → exec_progress … → applied 收尾
    r = client.post("/api/v1/dispatcher/chat/stream",
                    json={"confirm": True, "session_id": sid})
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    types = [e.get("type") for e in events]
    assert "exec_progress" in types, f"缺 exec_progress 事件: {types}"
    prog = [e for e in events if e["type"] == "exec_progress"]
    for e in prog:
        assert e.get("node") and e.get("message"), e
        assert e.get("tool") == "create_batch" and e.get("thread_id") == tid, e
        assert "done" in e and "total" in e, e
    nodes = [e["node"] for e in prog]
    assert "node1_parse_downstream" in nodes, nodes
    assert "node2_folder_router" in nodes, nodes
    # 末事件 applied（事件名兼容），批次已建
    assert events[-1]["type"] == "applied", events[-1]
    assert events[-1]["status"] == "applied", events[-1]
    state = service.get_order_state(tid)
    assert state["exists"] is True, f"批次应已创建: {state}"
    print(f"  ✓ 流式 confirm：{len(prog)} 条 exec_progress"
          f"（{' → '.join(nodes)}），末事件 applied，批次已建")


def case_2_on_progress_collector() -> None:
    """run_until_interrupt(on_progress) collector 收到 node1/node2 事件。"""
    tid = f"W4A-COLL-{int(time.time()*1000) % 100000}"
    collected: list[dict] = []
    r = service.run_until_interrupt(
        tid,
        downstream_file_path=DOWNSTREAM,
        upstream_root=str(EMPTY_UPSTREAM),
        factory_filter=["山東中地"],
        on_progress=collected.append,
    )
    assert r["status"] == "pending_human_review", r
    nodes = [e["node"] for e in collected]
    assert "node1_parse_downstream" in nodes, collected
    assert "node2_folder_router" in nodes, collected
    n1 = next(e for e in collected if e["node"] == "node1_parse_downstream")
    assert n1["total"] == 1 and "装箱单解析完成" in n1["message"], n1
    n2 = next(e for e in collected if e["node"] == "node2_folder_router")
    assert n2["factory"] == "山東中地" and "开始处理" in n2["message"], n2
    assert "1/1" in n2["message"], n2
    print(f"  ✓ collector 收到 {' → '.join(nodes)}："
          f"「{n1['message']}」/「{n2['message']}」")


def case_3_on_progress_exception_safe() -> None:
    """on_progress 抛异常跑图不受影响。"""
    tid = f"W4A-BOOM-{int(time.time()*1000) % 100000}"

    def boom(event: dict) -> None:
        raise RuntimeError("进度回调故意爆炸")

    r = service.run_until_interrupt(
        tid,
        downstream_file_path=DOWNSTREAM,
        upstream_root=str(EMPTY_UPSTREAM),
        factory_filter=["山東中地"],
        on_progress=boom,
    )
    assert r["status"] == "pending_human_review", \
        f"on_progress 异常不应影响跑图: {r}"
    state = service.get_order_state(tid)
    assert state["exists"] is True, state
    print("  ✓ on_progress 每事件都抛异常，跑图照常挂起")


CASES = [
    ("1. 流式 confirm 事件序（exec_progress → applied）", case_1_stream_confirm_events),
    ("2. on_progress collector 收到 node1/node2", case_2_on_progress_collector),
    ("3. on_progress 抛异常跑图不受影响", case_3_on_progress_exception_safe),
]


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name, fn in CASES:
        print(f"===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results.append((name, False, f"{type(e).__name__}: {e}"))
        else:
            print(f"[PASS] {name}")
            results.append((name, True, ""))
        print()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    if passed == len(results):
        print("🎉 W4a 执行级节点进度全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
