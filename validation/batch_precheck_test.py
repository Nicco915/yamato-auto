# -*- coding: utf-8 -*-
"""W4b 重复处理预检测试（check_processed_factories 四档 + precheck 端点 +
skip_processed 差集 / skipped_all 拦截）。

覆盖：
1. 四档判定：audited（review_audits approved）/ session_complete
   （sessions/*.json complete_auto）/ partial（collecting）/ none（无记录），
   损坏 session json → none；
2. POST /api/v1/batches/precheck 全链路（factory_names 直给 200；
   装箱单路径不存在且未给 factory_names → 422）；
3. skip_processed=true 差集正确：已处理工厂被跳过，只跑未处理工厂，
   响应带 skipped_processed；
4. 全部已处理 → skipped_all，checkpoints 无新 thread（不建图）。

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
AUDITED_TID = f"PRECHECK-AUDIT-{int(time.time()*1000) % 100000}"


def _make_xlsx(path: Path, factories: list[str]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    for i, f in enumerate(factories, 1):
        ws.append([f, f"490000000000{i}", "ITEM", 10])
    wb.save(path)
    return str(path)


def _write_session(factory: str, status: str) -> None:
    (SESSIONS / f"{factory}.json").write_text(json.dumps({
        "factory": factory, "status": status,
        "updated_at": "2026-08-01T10:00:00",
        "expected_skus": [], "items": {}, "issues": [], "history": [],
    }, ensure_ascii=False), encoding="utf-8")


def _seed() -> None:
    """造判定素材：audit 落库行 + 三份 session json（含一份损坏）。"""
    with get_db_session() as db:
        db.add(ReviewAudit(
            thread_id=AUDITED_TID, factory_name="厂A", approved=True,
            edited_count=0, changes_json="[]", new_skus_json="[]",
            result_status="success"))
        db.commit()
    _write_session("厂B", "complete_auto")
    _write_session("厂C", "collecting")
    (SESSIONS / "厂D.json").write_text("{broken json", encoding="utf-8")


def case_1_four_levels() -> None:
    """四档判定 + 损坏 session → none。"""
    _seed()
    r = service.check_processed_factories(
        factory_names=["厂A", "厂B", "厂C", "厂D", "厂E"])
    by_name = {f["factory"]: f for f in r["factories"]}

    a = by_name["厂A"]
    assert a["level"] == "audited" and a["processed"] is True, a
    assert a["last_audit"] and a["last_audit"]["thread_id"] == AUDITED_TID, a
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
    """POST /api/v1/batches/precheck 全链路 + 422。"""
    r = client.post("/api/v1/batches/precheck",
                    json={"factory_names": ["厂A", "厂B", "厂E"]})
    assert r.status_code == 200, r.text
    body = r.json()
    by_name = {f["factory"]: f for f in body["factories"]}
    assert by_name["厂A"]["level"] == "audited", body
    assert by_name["厂B"]["level"] == "session_complete", body
    assert by_name["厂E"]["level"] == "none", body
    assert body["processed_count"] == 2 and body["total_count"] == 3, body
    print("  ✓ precheck 端点 200，四档判定与 service 层一致")

    r = client.post("/api/v1/batches/precheck",
                    json={"downstream_file_path": str(TMP / "不存在.xlsx")})
    assert r.status_code == 422, f"装箱单解析失败应 422: {r.status_code} {r.text}"
    assert "装箱单解析失败" in r.json()["detail"], r.json()
    print("  ✓ 装箱单不存在 → 422")


def case_3_skip_processed_diff() -> None:
    """skip_processed=true：差集正确（厂B 跳过，只跑 厂F），响应带 skipped。"""
    xlsx = _make_xlsx(TMP / "downstream_skip.xlsx", ["厂B", "厂F"])
    upstream = TMP / "upstream_skip"
    upstream.mkdir(exist_ok=True)
    tid = f"PRECHECK-SKIP-{int(time.time()*1000) % 100000}"

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
    """全部已处理 → skipped_all，checkpoints 无新 thread。"""
    xlsx = _make_xlsx(TMP / "downstream_all.xlsx", ["厂A", "厂B"])
    upstream = TMP / "upstream_all"
    upstream.mkdir(exist_ok=True)
    tid = f"PRECHECK-ALL-{int(time.time()*1000) % 100000}"

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
