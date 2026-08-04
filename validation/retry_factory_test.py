# -*- coding: utf-8 -*-
"""单厂重试 retry_factory 正式测试（问题A-3）。

覆盖：
1. 主路径：2 厂批次 → 批准厂甲 → 挂起厂乙 → service.retry_factory_extraction
   返回 pending_human_review、factory=厂乙、pending_factories 长度不变、
   force_reextract 已被 Node3 自清回 False；
2. 负例-已完成批次：批准厂乙走完整批 → 再 retry → ValueError（未挂起）；
3. 负例-不存在批次：retry → ValueError；
4. rerun 预览警告：tools._preview_rerun 对存在批次，warnings 中有一条同时
   含「作废」与「retry_factory」；批次不存在时退化为「批次不存在」警告；
5. retry_factory 工具：挂起批次 _preview_retry_factory（summary 含工厂名、
   lines 非空）→ _exec_retry_factory 返回 pending_human_review；
   不存在批次 _preview_retry_factory warnings 非空；
6. summarize 模板：summarize_applied("retry_factory", ...) 成功/error 两分支
   不崩溃、message 为中文且含工厂名。

隔离（血泪红线）：checkpoint/master db、output、sessions 全部指向临时目录
（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 python3 validation/retry_factory_test.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.dispatcher import summarize, tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_retry_factory_test_")

FACTORIES = ["测试厂甲", "测试厂乙"]
MISSING_TID = "RETRY-不存在的批次-000"


def _tid(tag: str) -> str:
    return f"RETRY-{tag}-{int(time.time()*1000) % 1000000}"


def _make_xlsx(path: Path) -> str:
    """最小装箱单：Node1 只需工厂名列 + SKU 列即可解析出两厂需求。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    for i, f in enumerate(FACTORIES, 1):
        ws.append([f, f"490000000000{i}", "ITEM", 10])
    wb.save(path)
    return str(path)


def _make_batch(tid: str) -> dict:
    """造 2 厂假批次并跑到首个 interrupt（挂起厂甲）。"""
    xlsx = _make_xlsx(TMP / f"downstream_{tid}.xlsx")
    upstream = TMP / f"upstream_{tid}"
    upstream.mkdir(exist_ok=True)
    for f in FACTORIES:
        (upstream / f).mkdir(exist_ok=True)
    r = service.run_until_interrupt(
        tid, downstream_file_path=xlsx, upstream_root=str(upstream))
    assert r["status"] == "pending_human_review", r
    return r


def _approve(tid: str, payload: dict) -> dict:
    """按 payload 原样批准（新 SKU 补录合规字段），推进到下一挂起/完成。"""
    items = []
    for i, item in enumerate(payload["items"]):
        h_item = {"sku": item["sku"],
                  "extracted_data": dict(item["extracted_data"])}
        if item.get("is_new_sku"):
            h_item["name_cn"] = f"测试中文品名-{i + 1}"
            h_item["hs_code"] = "9404909000"
            h_item["inspection_required"] = False
        items.append(h_item)
    return service.resume_order(tid, {"approved": True, "items": items})


def case_1_main_path() -> None:
    """主路径：挂起厂乙 retry → 重新挂起，队列不变，force_reextract 自清。"""
    tid = _tid("MAIN")
    r = _make_batch(tid)
    p1 = r["review_data"]
    assert p1["factory_name"] == FACTORIES[0], p1.get("factory_name")

    r2 = _approve(tid, p1)
    assert r2["status"] == "pending_human_review", r2
    assert r2["review_data"]["factory_name"] == FACTORIES[1], r2["review_data"]
    # 挂起厂乙时的待处理队列长度（厂乙已出队进入 current_factory_data）
    n_pending = len(service.get_order_state(tid)["values"]["pending_factories"])

    ret = service.retry_factory_extraction(tid)
    assert ret["status"] == "pending_human_review", ret
    assert ret["factory"] == FACTORIES[1], ret
    assert ret["review_data"]["factory_name"] == FACTORIES[1], ret["review_data"]

    state = service.get_order_state(tid)
    values = state["values"]
    assert len(values["pending_factories"]) == n_pending, \
        f"pending_factories 长度应不变: {values['pending_factories']}"
    assert values.get("force_reextract") is False, \
        f"force_reextract 应已自清: {values.get('force_reextract')!r}"
    print(f"  ✓ 厂乙重提后重新挂起：pending_factories 仍 {n_pending} 个，"
          f"force_reextract 已清回 False")

    # 供后续用例接力：返回批次与最新挂起 payload
    case_1_main_path.ctx = {"tid": tid, "payload": ret["review_data"]}


def case_2_completed_batch() -> None:
    """负例-已完成批次：批准厂乙走完整批 → 再 retry → ValueError。"""
    ctx = case_1_main_path.ctx
    tid = ctx["tid"]
    r = _approve(tid, ctx["payload"])
    assert r["status"] == "success", f"批次应走完整批: {r}"
    state = service.get_order_state(tid)
    assert state["next_nodes"] == [], f"批次应已无后续节点: {state['next_nodes']}"

    try:
        service.retry_factory_extraction(tid)
    except ValueError as e:
        assert "未挂起" in str(e), f"错误文案应说明未挂起: {e}"
        print(f"  ✓ 已完成批次 retry 被拒：ValueError「{e}」")
    else:
        raise AssertionError("已完成批次 retry 应抛 ValueError")


def case_3_missing_batch() -> None:
    """负例-不存在批次：retry → ValueError。"""
    try:
        service.retry_factory_extraction(MISSING_TID)
    except ValueError as e:
        assert "不存在" in str(e), f"错误文案应说明批次不存在: {e}"
        print(f"  ✓ 不存在批次 retry 被拒：ValueError「{e}」")
    else:
        raise AssertionError("不存在批次 retry 应抛 ValueError")


def case_4_rerun_preview_warnings() -> None:
    """rerun 预览：存在批次 warnings 含「作废」+「retry_factory」；不存在退化。"""
    tid = case_1_main_path.ctx["tid"]
    prev = tools._preview_rerun({"thread_id": tid})
    assert prev["warnings"], f"存在批次应有整批重跑警告: {prev}"
    hit = [w for w in prev["warnings"] if "作废" in w and "retry_factory" in w]
    assert hit, f"warnings 应有一条同时含「作废」与「retry_factory」: {prev['warnings']}"
    print(f"  ✓ 存在批次 rerun 警告：「{hit[0][:40]}…」（共 {len(prev['warnings'])} 条）")

    prev_ng = tools._preview_rerun({"thread_id": MISSING_TID})
    assert prev_ng["warnings"], f"不存在批次也应有退化警告: {prev_ng}"
    assert any("不存在" in w for w in prev_ng["warnings"]), prev_ng["warnings"]
    print(f"  ✓ 不存在批次退化警告：「{prev_ng['warnings'][0]}」")


def case_5_retry_factory_tool() -> None:
    """retry_factory 工具：预览含工厂名 → 执行重新挂起；不存在批次预览告警。"""
    tid = _tid("TOOL")
    r = _make_batch(tid)
    factory = r["review_data"]["factory_name"]

    prev = tools._preview_retry_factory({"thread_id": tid})
    assert factory in prev["summary"], f"summary 应含工厂名: {prev['summary']}"
    assert prev["lines"], f"lines 应非空: {prev}"
    assert not prev["warnings"], f"挂起批次预览不应有警告: {prev['warnings']}"
    print(f"  ✓ 预览：「{prev['summary'][:50]}…」，lines {len(prev['lines'])} 条")

    ret = tools._exec_retry_factory({"thread_id": tid})
    assert ret.get("status") == "pending_human_review", ret
    assert ret.get("factory") == factory, ret
    state = service.get_order_state(tid)
    assert state["values"].get("force_reextract") is False, state["values"]
    print(f"  ✓ 执行：工厂「{factory}」重新挂起待审核，force_reextract 已自清")

    prev_ng = tools._preview_retry_factory({"thread_id": MISSING_TID})
    assert prev_ng["warnings"], f"不存在批次预览应有警告: {prev_ng}"
    print(f"  ✓ 不存在批次预览警告：「{prev_ng['warnings'][0][:40]}…」")


def case_6_summarize_template() -> None:
    """summarize 模板：成功/error 两分支不崩溃、message 中文且含工厂名。"""
    ok_result = {
        "status": "pending_human_review",
        "thread_id": "T-SUM-1",
        "factory": FACTORIES[0],
        "review_data": {"factory_name": FACTORIES[0],
                        "items": [{"sku": "4900000000001"}]},
    }
    s = summarize.summarize_applied(
        "retry_factory", {"thread_id": "T-SUM-1"}, ok_result)
    assert isinstance(s["message"], str) and s["message"], s
    assert FACTORIES[0] in s["message"], f"成功 message 应含工厂名: {s['message']}"
    assert "重新提取" in s["message"], f"成功 message 应为中文模板: {s['message']}"
    print(f"  ✓ 成功分支：「{s['message']}」")

    err_result = {"error": f"批次 T-SUM-1 工厂「{FACTORIES[1]}」当前未挂起待审核，"
                           "无法单厂重试"}
    s2 = summarize.summarize_applied(
        "retry_factory", {"thread_id": "T-SUM-1"}, err_result)
    assert isinstance(s2["message"], str) and s2["message"], s2
    assert s2["message"].startswith("执行失败"), f"error 分支应走失败文案: {s2}"
    assert FACTORIES[1] in s2["message"], f"error message 应含工厂名: {s2['message']}"
    print(f"  ✓ error 分支：「{s2['message'][:50]}…」")


CASES = [
    ("1. 主路径：挂起厂乙 retry → 重新挂起、队列不变、标志自清", case_1_main_path),
    ("2. 负例：已完成批次 retry → ValueError", case_2_completed_batch),
    ("3. 负例：不存在批次 retry → ValueError", case_3_missing_batch),
    ("4. rerun 预览整批重跑作废警告（存在/不存在两档）", case_4_rerun_preview_warnings),
    ("5. retry_factory 工具预览/执行 + 不存在批次告警", case_5_retry_factory_tool),
    ("6. summarize 模板成功/error 两分支", case_6_summarize_template),
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
        print("🎉 单厂重试 retry_factory 全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
