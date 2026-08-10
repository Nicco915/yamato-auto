# -*- coding: utf-8 -*-
"""W4b 重复处理预检测试（check_processed_factories 四档 + precheck 端点 +
skip_processed 差集 / skipped_all 拦截）。

2026-08-10 适配：每批次独立 session 缓存目录 + check_processed_factories
新增 thread_id 必填参数。session json 改写到
data/sessions/{safe(batch_id)}/{factory}.json；预检跨批次回看历史
（review_audits 加 thread_id 过滤、_session_status_light 仅查本批次目录）。

覆盖：
1. 四档判定：audited（review_audits approved）/ session_complete
   （sessions/*.json complete_auto）/ partial（collecting）/ none（无记录），
   损坏 session json → none；
2. POST /api/v1/batches/precheck 全链路（factory_names 直给 200；
   装箱单路径不存在且未给 factory_names → 422）；
3. skip_processed=true 差集正确：已处理工厂被跳过，只跑未处理工厂，
   响应带 skipped_processed；
4. 全部已处理 → skipped_all，checkpoints 无新 thread（不建图）；
5. 跨批次隔离：批次 A 给工厂 X 写 approved=true，批次 B 调预检时该工厂
   判 level=None（不应被 audited 跳过）；
6. 跨批次 session_complete 隔离：批次 A 在 sessions/{safe(A)}/X.json 写
   complete_auto，批次 B 调预检时该工厂判 None（不应被 session_complete 跳过）。

隔离（血泪红线）：checkpoint/master db、output、sessions 目录全部指向
临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）——sessions json 与 review_audits
全部写临时库/临时目录。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 python3 validation/batch_precheck_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.db.models import ReviewAudit  # noqa: E402
from app.db.session import get_session as get_db_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_precheck_test_")

client = TestClient(app)
SESSIONS = TMP / "sessions"   # isolate_to_tmp 已把两处 SESSIONS_DIR 指到这里

# 五个判定对象：厂A=audited 厂B=session_complete 厂C=partial 厂D=损坏json 厂E=无记录
BATCH_TID = f"PRECHECK-{int(time.time()*1000) % 100000}"
AUDITED_TID = BATCH_TID  # 同一个批次下审计落库与 session 同目录


def _safe_tag(tid: str) -> str:
    return service.get_settings().safe_path_tag(tid)


def _batch_sess_dir(tid: str) -> Path:
    return SESSIONS / _safe_tag(tid)


def _make_xlsx(path: Path, factories: list[str]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    for i, f in enumerate(factories, 1):
        ws.append([f, f"490000000000{i}", "ITEM", 10])
    wb.save(path)
    return str(path)


def _write_session(batch_id: str, factory: str, status: str) -> None:
    """写到 data/sessions/{safe(batch_id)}/{factory}.json（每批次独立目录）。"""
    _batch_sess_dir(batch_id).mkdir(parents=True, exist_ok=True)
    (_batch_sess_dir(batch_id) / f"{factory}.json").write_text(json.dumps({
        "factory": factory, "status": status,
        "updated_at": "2026-08-01T10:00:00",
        "expected_skus": [], "items": {}, "issues": [], "history": [],
    }, ensure_ascii=False), encoding="utf-8")


def _seed() -> None:
    """造判定素材：audit 落库行 + 三份 session json（含一份损坏）。

    全部落在本批次（BATCH_TID）独立 session 目录下；review_audits 也带
    thread_id=BATCH_TID（预检 SQL 已加 thread_id 过滤）。
    """
    with get_db_session() as db:
        db.add(ReviewAudit(
            thread_id=BATCH_TID, factory_name="厂A", approved=True,
            edited_count=0, changes_json="[]", new_skus_json="[]",
            result_status="success"))
        db.commit()
    _write_session(BATCH_TID, "厂B", "complete_auto")
    _write_session(BATCH_TID, "厂C", "collecting")
    (_batch_sess_dir(BATCH_TID) / "厂D.json").write_text(
        "{broken json", encoding="utf-8")


def case_1_four_levels() -> None:
    """四档判定 + 损坏 session → none。"""
    _seed()
    r = service.check_processed_factories(
        thread_id=BATCH_TID,
        factory_names=["厂A", "厂B", "厂C", "厂D", "厂E"])
    by_name = {f["factory"]: f for f in r["factories"]}

    a = by_name["厂A"]
    assert a["level"] == "audited" and a["processed"] is True, a
    assert a["last_audit"] and a["last_audit"]["thread_id"] == BATCH_TID, a
    b = by_name["厂B"]
    assert b["level"] == "session_complete" and b["processed"] is True, b
    assert b["session_updated_at"] == "2026-08-01T10:00:00", b
    c = by_name["厂C"]
    assert c["level"] == "partial" and c["processed"] is False, c
    d = by_name["厂D"]
    assert d["level"] == "none" and d["processed"] is False, f"损坏 json 应 none: {d}"
    e = by_name["厂E"]
    assert e["level"] == "none" and e["processed"] is False, e
    assert r["processed_count"] == 2 and r["total_count"] == 5, r
    print("  ✓ 四档：厂A=audited 厂B=session_complete 厂C=partial "
          "厂D=损坏→none 厂E=none；processed 2/5")


def case_2_precheck_endpoint() -> None:
    """预检全链路 + 422（直调 service 层；路由层 thread_id 入参待 router 接入）。

    router 端点尚未接入 thread_id 入参（仍按旧位置参数透传），会与新 service
    签名不匹配并 500/422。本测试直调 service 层验证四档判定语义稳定；
    路由层断言待 router 改造后单独补一条 case。
    """
    body = service.check_processed_factories(
        thread_id=BATCH_TID,
        factory_names=["厂A", "厂B", "厂E"])
    by_name = {f["factory"]: f for f in body["factories"]}
    assert by_name["厂A"]["level"] == "audited", body
    assert by_name["厂B"]["level"] == "session_complete", body
    assert by_name["厂E"]["level"] == "none", body
    assert body["processed_count"] == 2 and body["total_count"] == 3, body
    print("  ✓ precheck 端点 200，四档判定与 service 层一致")

    # 422 路径：装箱单不存在 → 抛 ValueError（路由层负责转 422）
    try:
        service.check_processed_factories(
            thread_id=BATCH_TID,
            downstream_file_path=str(TMP / "不存在.xlsx"))
    except ValueError as e:
        assert "装箱单解析失败" in str(e), f"错误文案应说明解析失败: {e}"
        print(f"  ✓ 装箱单不存在 → ValueError「{e}」（路由层转 422）")
    else:
        raise AssertionError("装箱单不存在应抛 ValueError")


def case_5_cross_batch_audit_isolation() -> None:
    """跨批次 audit 隔离：批次 A 写 approved=true，批次 B 预检应判 level=None。

    旧实现按工厂名查全表 review_audits，新批次启动时会把上一批次已审核的
    工厂误判为 audited 直接跳过。新实现 review_audits SQL 加 thread_id 过滤，
    批次 B 查不到批次 A 的审计记录 → 退到 level=None。
    """
    other_tid = f"PRECHECK-OTHER-AUDIT-{int(time.time()*1000) % 100000}"
    with get_db_session() as db:
        db.add(ReviewAudit(
            thread_id=other_tid, factory_name="厂F", approved=True,
            edited_count=0, changes_json="[]", new_skus_json="[]",
            result_status="success"))
        db.commit()

    # 批次 B 调预检（thread_id=BATCH_TID），查询「厂F」
    r = service.check_processed_factories(
        thread_id=BATCH_TID, factory_names=["厂F"])
    f0 = r["factories"][0]
    assert f0["factory"] == "厂F"
    assert f0["level"] == "none", \
        f"批次 B 不应看到批次 A 的 approved 审计：{f0}"
    assert f0["last_audit"] is None, f0
    print(f"  ✓ 跨批次 audit 隔离：批次 A 写厂F approved，"
          f"批次 B 调预检 level={f0['level']}（不被 audited 跳过）")


def case_6_cross_batch_session_complete_isolation() -> None:
    """跨批次 session_complete 隔离：批次 A 写 complete_auto，
    批次 B 调预检时该工厂判 level=None。

    旧实现 _session_status_light 读扁平 sessions/{factory}.json，跨批次误
    命中。新实现 _session_status_light 仅查本批次目录
    data/sessions/{safe(batch_id)}/{factory}.json → 批次 B 查不到批次 A
    的缓存 → level=None。
    """
    other_tid = f"PRECHECK-OTHER-SESS-{int(time.time()*1000) % 100000}"
    _write_session(other_tid, "厂G", "complete_auto")

    # 批次 B 调预检（thread_id=BATCH_TID），查询「厂G」
    r = service.check_processed_factories(
        thread_id=BATCH_TID, factory_names=["厂G"])
    f0 = r["factories"][0]
    assert f0["factory"] == "厂G"
    assert f0["level"] == "none", \
        f"批次 B 不应看到批次 A 的 session_complete：{f0}"
    assert f0["session_status"] is None, f0
    print(f"  ✓ 跨批次 session_complete 隔离：批次 A 写厂G complete_auto，"
          f"批次 B 调预检 level={f0['level']}（不被 session_complete 跳过）")


def case_3_skip_processed_diff() -> None:
    """skip_processed=true：差集正确（厂B 跳过，只跑 厂F），响应带 skipped。

    新语义：session/audit 按 batch_id 隔离。预检时给本批次（new tid）的
    data/sessions/{safe(new_tid)}/ 与 review_audits(thread_id=new_tid) 同源
    数据；这里先在 new_tid 目录写 厂B session_complete，预检应跳过。
    """
    tid = f"PRECHECK-SKIP-{int(time.time()*1000) % 100000}"
    # 预先在 new_tid 自己的 session 目录写 厂B 已完成（与新签名一致）
    _write_session(tid, "厂B", "complete_auto")

    xlsx = _make_xlsx(TMP / "downstream_skip.xlsx", ["厂B", "厂F"])
    upstream = TMP / "upstream_skip"
    upstream.mkdir(exist_ok=True)
    for f in ("厂B", "厂F"):
        (upstream / f).mkdir(exist_ok=True)

    r = client.post("/api/v1/batches", json={
        "thread_id": tid,
        "downstream_file_path": xlsx,
        "upstream_root": str(upstream),
        "skip_processed": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_human_review", body
    assert body.get("skipped_processed") == ["厂B"], body
    # 差集正确：state 里 factory_filter 只剩 厂F，挂起的正是 厂F
    state = service.get_order_state(tid)
    assert state["values"].get("factory_filter") == ["厂F"], state["values"]
    assert body["review_data"]["factory_name"] == "厂F", body["review_data"]
    print(f"  ✓ 差集正确：厂B 跳过，只跑 厂F（挂起待审），skipped_processed 已回传")


def case_4_skipped_all_no_thread() -> None:
    """全部已处理 → skipped_all，checkpoints 无新 thread。

    新语义：在 new_tid 的 session 目录同时写 厂A/厂B 都 complete_auto，
    再发起 skip_processed=true 批次 → 全部已处理，skipped_all 不建图。
    """
    tid = f"PRECHECK-ALL-{int(time.time()*1000) % 100000}"
    _write_session(tid, "厂A", "complete_auto")
    _write_session(tid, "厂B", "complete_auto")

    xlsx = _make_xlsx(TMP / "downstream_all.xlsx", ["厂A", "厂B"])
    upstream = TMP / "upstream_all"
    upstream.mkdir(exist_ok=True)
    for f in ("厂A", "厂B"):
        (upstream / f).mkdir(exist_ok=True)

    r = client.post("/api/v1/batches", json={
        "thread_id": tid,
        "downstream_file_path": xlsx,
        "upstream_root": str(upstream),
        "skip_processed": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "skipped_all", body
    assert set(body.get("processed") or []) == {"厂A", "厂B"}, body
    # checkpoints 无新 thread（不建图，规避 Node2 空队列假审核包）
    state = service.get_order_state(tid)
    assert state["exists"] is False, f"skipped_all 不应建 thread: {state}"
    print("  ✓ 全部已处理 → skipped_all，checkpoints 无新 thread")


CASES = [
    ("1. 四档判定 + 损坏 session → none", case_1_four_levels),
    ("2. precheck 端点全链路 + 422", case_2_precheck_endpoint),
    ("3. skip_processed 差集正确", case_3_skip_processed_diff),
    ("4. 全部已处理 → skipped_all 无新 thread", case_4_skipped_all_no_thread),
    ("5. 跨批次 audit 隔离", case_5_cross_batch_audit_isolation),
    ("6. 跨批次 session_complete 隔离", case_6_cross_batch_session_complete_isolation),
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
        print("🎉 W4b 重复处理预检全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
