# -*- coding: utf-8 -*-
"""生产前端（T1-T5）全链路 API/UI 测试：TestClient 走通批次管理 + 审核 + 审计落库。

隔离原则（绝不碰 app/data/ 下的生产 db）：
- checkpoint / master db 全部指向临时目录（env 前置，import app 之前生效）；
- 下游装箱单复制到临时目录后按批次传入（Node6 只写 output 副本，绝不覆盖原件，
  这里连 output 目录也指向临时目录，全链路零生产副作用）；
- EXTRACTION_MOCK=1：提取走 mock，不调 LLM，秒级跑完。

断言流：defaults → 发起批次（首工厂挂起）→ 重名 409 → 坏路径 422 → 批次列表
→ 批次详情 → 审核 payload → resume（改总净重 +50 + 新 SKU 补录）→ 审计落库
→ usage → 4 个页面路由 → 详情 404 → 删除挂起批次 → 列表确认消失
→ 审计留痕（既有 resume 行保留 + batch_deleted 留痕行）→ 重复删除/详情 404。

（删除步骤放最后：第 9 步审计断言依赖批次存在，第 11 步页面路由为静态页不受影响。
 running 409 分支在 mock/TestClient 下难以稳定构造——需要批次恰好处于
 "next 非空且无 interrupt" 的瞬态，单线程串行请求抓不到，此处注释说明不强制断言。）

用法（在 app/ 目录下）：
  python3 validation/ui_api_test.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# ---- env 前置（EXTRACTION_MOCK 需在 import app 之前；db 路径在 import 后隔离）----
os.environ["EXTRACTION_MOCK"] = "1"                      # 提取走 mock，不调 LLM

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.graph import get_graph  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 关键坑：app.extraction.llm_client 在 import 时执行 load_dotenv(override=True)，
# 会把 .env 里的 MASTER_DB_PATH/CHECKPOINT_DB_PATH 盖回 os.environ——因此
# db 隔离必须放在 import 完成之后：重设 env + 清 lru_cache + 真实库断言守卫
# （graph/engine 都是惰性单例，首个请求才创建，此刻清缓存即指向临时目录）。
TMP = isolate_to_tmp("yamato_ui_test_", alias_map_copy=True)

THREAD_ID = "UI-TEST-1"
client = TestClient(app)


def main() -> int:
    settings = get_settings()

    # 隔离兜底：db 必须指向临时目录，否则直接中止（宁可 FAIL 也不碰生产 db）
    for name, p in (("master_db", settings.master_db_abs),
                    ("checkpoint_db", settings.checkpoint_db_abs),
                    ("output_dir", settings.output_dir_abs)):
        assert str(p).startswith(str(TMP)), f"{name} 未隔离到临时目录: {p}"

    # 下游装箱单：复制真实文件到临时目录（只读源文件，测试全链路不碰原件）
    src_downstream = Path(settings.downstream_file_path)
    assert src_downstream.is_file(), f"默认下游装箱单不存在: {src_downstream}"
    tmp_downstream = TMP / src_downstream.name
    shutil.copy2(src_downstream, tmp_downstream)

    # 预热：全新部署（checkpoints.db 不存在）时 SqliteSaver 尚未建表，
    # service._open_checkpoint_ro 以 mode=ro 打开会失败——先触发建库建表。
    # （注意：这也意味着生产全新部署首发批次前必须有任意一次图调用，
    #  属 service 层已知边界，此处测试侧预热规避）
    get_graph().get_state({"configurable": {"thread_id": "__warmup__"}})

    # ---- 1. 配置默认值 ----
    print("===== 1. GET /api/v1/config/defaults =====")
    r = client.get("/api/v1/config/defaults")
    assert r.status_code == 200, r.text
    defaults = r.json()
    for f in ("upstream_root", "downstream_file_path", "weight_diff_warn_ratio"):
        assert defaults.get(f) not in (None, ""), f"defaults 缺字段 {f}: {defaults}"
    print(f"  ✓ 三字段非空: {defaults}")

    # ---- 2. 发起批次：mock 模式跑全工厂，首个工厂挂起即返回 ----
    print("\n===== 2. POST /api/v1/batches（发起批次）=====")
    r = client.post("/api/v1/batches", json={
        "thread_id": THREAD_ID,
        "downstream_file_path": str(tmp_downstream),
    })
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["status"] == "pending_human_review", created
    first_factory = created["review_data"]["factory_name"]
    print(f"  ✓ 首个工厂「{first_factory}」挂起待审")

    # ---- 3. 同 thread_id 重发 → 409 ----
    print("\n===== 3. 重名 thread_id → 409 =====")
    r = client.post("/api/v1/batches", json={
        "thread_id": THREAD_ID,
        "downstream_file_path": str(tmp_downstream),
    })
    assert r.status_code == 409, f"期望 409，实际 {r.status_code}: {r.text}"
    print(f"  ✓ 409: {r.json()['detail']}")

    # ---- 4. 不存在的上游路径 → 422 ----
    print("\n===== 4. 不存在的 upstream_root → 422 =====")
    r = client.post("/api/v1/batches", json={
        "thread_id": "UI-TEST-GHOST",
        "upstream_root": str(TMP / "不存在路径ghost"),
    })
    assert r.status_code == 422, f"期望 422，实际 {r.status_code}: {r.text}"
    assert "不存在" in r.json()["detail"], r.json()
    print(f"  ✓ 422: {r.json()['detail']}")

    # ---- 5. 批次列表 ----
    print("\n===== 5. GET /api/v1/batches =====")
    r = client.get("/api/v1/batches")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "batches" in body and isinstance(body["batches"], list), body
    mine = next((b for b in body["batches"] if b["thread_id"] == THREAD_ID), None)
    assert mine, f"列表中找不到 {THREAD_ID}: {body}"
    assert mine["status"] == "pending_review", mine
    assert mine["progress"]["total"] >= 1, mine
    assert mine["created_at"], f"created_at 为空: {mine}"
    total_factories = mine["progress"]["total"]
    print(f"  ✓ {THREAD_ID} status=pending_review，进度 "
          f"{mine['progress']['done']}/{total_factories}，created_at={mine['created_at']}")

    # ---- 6. 批次详情 ----
    print("\n===== 6. GET /api/v1/batches/{thread_id} =====")
    r = client.get(f"/api/v1/batches/{THREAD_ID}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["factories"], "factories 为空"
    assert len(detail["factories"]) == total_factories, \
        f"factories 数 {len(detail['factories'])} != progress.total {total_factories}"
    currents = [f for f in detail["factories"] if f["role"] == "current"]
    assert len(currents) == 1, f"应恰有 1 个 current 工厂: {currents}"
    assert currents[0]["factory"] == first_factory
    assert "session" in detail["factories"][0], "factories[] 缺 session 键"
    assert detail["audit"] == [], f"resume 前 audit 应为空: {detail['audit']}"
    assert detail["usage"]["scope"] == "process_lifetime", detail["usage"]
    print(f"  ✓ factories {len(detail['factories'])} 个（current=「{first_factory}」），"
          f"audit 空，usage.scope=process_lifetime")

    # ---- 7. 审核 payload ----
    print("\n===== 7. GET /api/v1/review/{thread_id}/payload =====")
    r = client.get(f"/api/v1/review/{THREAD_ID}/payload")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["items"], "payload items 为空"
    assert payload.get("weight_diff_warn_ratio") == 0.05, \
        f"weight_diff_warn_ratio 异常: {payload.get('weight_diff_warn_ratio')}"
    n_new = sum(1 for i in payload["items"] if i.get("is_new_sku"))
    # 三字段结构断言：新 SKU 带 fields_to_fill + 顶层商检默认值；
    # 老 SKU 三字段提升到顶层（值为主库值，第 17 步播种后断言具体值）
    for i in payload["items"]:
        if i.get("is_new_sku"):
            assert i.get("fields_to_fill") == ["name_cn", "hs_code", "inspection_required"], i
            assert "inspection_required" in i, f"新 SKU 缺顶层商检默认值: {i}"
        else:
            for f in ("name_cn", "hs_code", "inspection_required"):
                assert f in i, f"老 SKU payload 缺顶层字段 {f}: {i}"
    print(f"  ✓ items {len(payload['items'])} 条（新 SKU {n_new}），"
          f"weight_diff_warn_ratio=0.05，三字段结构断言通过")

    # ---- 8. resume：改第一项总净重 +50，新 SKU 补录合规字段 ----
    print("\n===== 8. POST /api/v1/orders/{thread_id}/resume =====")
    edited_sku = payload["items"][0]["sku"]
    orig_net = payload["items"][0]["extracted_data"]["total_net_weight"]
    new_net = orig_net + 50
    items = []
    for i, item in enumerate(payload["items"]):
        h_item = {"sku": item["sku"], "extracted_data": dict(item["extracted_data"])}
        if i == 0:
            h_item["extracted_data"]["total_net_weight"] = new_net
        if item.get("is_new_sku"):
            h_item["name_cn"] = f"测试中文品名-{i + 1}"
            h_item["hs_code"] = "9404909000"
            h_item["inspection_required"] = False
        items.append(h_item)
    r = client.post(f"/api/v1/orders/{THREAD_ID}/resume",
                    json={"approved": True, "items": items})
    assert r.status_code == 200, r.text
    resumed = r.json()
    assert resumed["status"] in ("pending_human_review", "success"), resumed
    print(f"  ✓ SKU {edited_sku} 总净重 {orig_net} -> {new_net}，"
          f"resume 结果 status={resumed['status']}")

    # ---- 9. 审计落库：1 行，changes 扁平结构 ----
    print("\n===== 9. 审计落库（GET 批次详情 audit）=====")
    r = client.get(f"/api/v1/batches/{THREAD_ID}")
    assert r.status_code == 200, r.text
    audit = r.json()["audit"]
    assert len(audit) == 1, f"应有 1 行审计: {audit}"
    row = audit[0]
    assert row["factory_name"] == first_factory, row
    assert row["approved"] is True, row
    assert row["edited_count"] >= 1, row
    change = next((c for c in row["changes"]
                   if c["sku"] == edited_sku and c["field"] == "total_net_weight"), None)
    assert change, f"changes 缺 total_net_weight 扁平记录: {row['changes']}"
    assert change["old"] == orig_net and change["new"] == new_net, change
    if n_new:
        assert row["new_skus"], f"payload 有新 SKU 但审计 new_skus 为空: {row}"
    print(f"  ✓ 审计 1 行：edited_count={row['edited_count']}，"
          f"change={change}，new_skus {len(row['new_skus'])} 条")

    # ---- 10. 全局用量 ----
    print("\n===== 10. GET /api/v1/usage =====")
    r = client.get("/api/v1/usage")
    assert r.status_code == 200, r.text
    assert "scope" in r.json(), r.json()
    print(f"  ✓ scope={r.json()['scope']}")

    # ---- 11. 页面路由 ----
    print("\n===== 11. 页面路由 200 + 特征字符串 =====")
    for path, marker in (("/", "发起批次"),
                         ("/chat", "调度 Agent"),  # 6.7 起 /chat 为调度 Agent 页
                         (f"/batch/{THREAD_ID}", "批次详情"),
                         ("/ui/static/ui.css", "--primary")):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert marker in r.text, f"{path} 缺特征字符串 {marker!r}"
        print(f"  ✓ {path} 200，含 {marker!r}")

    # ---- 12. 详情 404 ----
    print("\n===== 12. 不存在的批次 → 404 =====")
    r = client.get("/api/v1/batches/不存在的批次")
    assert r.status_code == 404, f"期望 404，实际 {r.status_code}: {r.text}"
    print(f"  ✓ 404: {r.json()['detail']}")

    # ---- 13. 删除挂起批次：DELETE → 200，checkpoints_removed>0 ----
    # （第 8 步 resume 后 UI-TEST-1 处于"下一工厂挂起"的 pending_review 状态，
    #   删除它是合法分支；running 409 分支见文件头注释，不强制断言）
    print("\n===== 13. DELETE /api/v1/batches/UI-TEST-1（挂起批次）=====")
    r = client.delete(f"/api/v1/batches/{THREAD_ID}")
    assert r.status_code == 200, f"期望 200，实际 {r.status_code}: {r.text}"
    deleted = r.json()
    assert deleted["deleted"] == THREAD_ID, deleted
    assert deleted["checkpoints_removed"] > 0, \
        f"checkpoints_removed 应 >0: {deleted}"
    assert deleted["writes_removed"] >= 0, deleted
    print(f"  ✓ 200: checkpoints_removed={deleted['checkpoints_removed']}，"
          f"writes_removed={deleted['writes_removed']}")

    # ---- 14. 批次列表：UI-TEST-1 已消失 ----
    print("\n===== 14. GET /api/v1/batches（删除后）=====")
    r = client.get("/api/v1/batches")
    assert r.status_code == 200, r.text
    remaining = [b["thread_id"] for b in r.json()["batches"]]
    assert THREAD_ID not in remaining, f"删除后列表仍有 {THREAD_ID}: {remaining}"
    print(f"  ✓ {THREAD_ID} 已从列表消失（现存 {len(remaining)} 个批次）")

    # ---- 15. 审计留痕：既有 resume 行保留 + 新增 batch_deleted 行 ----
    print("\n===== 15. review_audits 留痕（直查临时 master.db）=====")
    master_db = str(settings.master_db_abs)
    assert master_db.startswith(str(TMP)), f"master.db 未隔离: {master_db}"
    conn = sqlite3.connect(master_db)
    try:
        rows = conn.execute(
            "SELECT result_status FROM review_audits WHERE thread_id = ? "
            "ORDER BY audit_id", (THREAD_ID,)).fetchall()
    finally:
        conn.close()
    statuses = [r[0] for r in rows]
    assert len(rows) == 2, f"应恰有 2 行审计（resume + 留痕）: {statuses}"
    assert statuses[0] != "batch_deleted", f"既有 resume 审计行被改动: {statuses}"
    assert statuses[-1] == "batch_deleted", f"缺 batch_deleted 留痕行: {statuses}"
    print(f"  ✓ resume 行保留（result_status={statuses[0]!r}）"
          f" + 新增留痕行（result_status='batch_deleted'）")

    # ---- 16. 重复删除 → 404；详情 → 404 ----
    print("\n===== 16. 重复删除/详情 → 404 =====")
    r = client.delete(f"/api/v1/batches/{THREAD_ID}")
    assert r.status_code == 404, f"重复删除期望 404，实际 {r.status_code}: {r.text}"
    print(f"  ✓ 重复删除 404: {r.json()['detail']}")
    r = client.get(f"/api/v1/batches/{THREAD_ID}")
    assert r.status_code == 404, f"删除后详情期望 404，实际 {r.status_code}: {r.text}"
    print(f"  ✓ 详情 404: {r.json()['detail']}")

    # ---- 17. 老 SKU 三字段提升：播种主库后新批次 payload 顶层带主库值 ----
    print("\n===== 17. 老 SKU payload 顶层三字段（播种主库 + UI-TEST-2）=====")
    # 首批次 resume(approved) 时 Node6 已把首工厂全部 mock SKU 落主库，
    # 这里对前 2 个 SKU upsert 覆盖为可识别的播种值（mock 提取确定性，
    # 新批次同工厂同 SKU）
    seed = [(i["sku"], n) for n, i in enumerate(payload["items"][:2], start=1)]
    conn = sqlite3.connect(master_db)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO factories (factory_name, created_at) "
            "VALUES (?, datetime('now'))", (first_factory,))
        fid = cur.execute(
            "SELECT factory_id FROM factories WHERE factory_name = ?",
            (first_factory,)).fetchone()[0]
        for sku, n in seed:
            cur.execute(
                "INSERT INTO factory_skus (factory_id, sku_code, name_cn, hs_code, "
                "inspection_required, unit_net_weight, unit_gross_weight, updated_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(factory_id, sku_code) DO UPDATE SET "
                "name_cn=excluded.name_cn, hs_code=excluded.hs_code, "
                "inspection_required=excluded.inspection_required",
                (fid, sku, f"主库品名-{n}", f"940490900{n}", n % 2, 5.0, 5.3))
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/v1/batches", json={
        "thread_id": "UI-TEST-2",
        "downstream_file_path": str(tmp_downstream),
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_human_review", r.json()
    r = client.get("/api/v1/review/UI-TEST-2/payload")
    assert r.status_code == 200, r.text
    p2 = r.json()
    assert p2["factory_name"] == first_factory, \
        f"第二批次首工厂应与首批次一致: {p2['factory_name']} != {first_factory}"
    # 首工厂 SKU 首批次已全部落主库 → 第二批次应全部命中为老 SKU，
    # 且三字段提升到顶层（未播种的为首批次 resume 补录值）
    assert all(not i.get("is_new_sku") for i in p2["items"]), \
        f"第二批次仍有新 SKU: {[i['sku'] for i in p2['items'] if i.get('is_new_sku')]}"
    for i in p2["items"]:
        for f in ("name_cn", "hs_code", "inspection_required"):
            assert f in i, f"老 SKU payload 缺顶层字段 {f}: {i}"
        assert "fields_to_fill" not in i, f"老 SKU 不应有 fields_to_fill: {i}"
    for sku, n in seed:
        it = next(i for i in p2["items"] if i["sku"] == sku)
        assert it.get("name_cn") == f"主库品名-{n}", it
        assert it.get("hs_code") == f"940490900{n}", it
        assert it.get("inspection_required") == bool(n % 2), it
    print(f"  ✓ 全部 {len(p2['items'])} 个老 SKU 顶层带三字段，"
          f"其中 {len(seed)} 个播种值精确命中")

    # 清理：删除 UI-TEST-2，不留挂起批次
    r = client.delete("/api/v1/batches/UI-TEST-2")
    assert r.status_code == 200, f"清理 UI-TEST-2 失败: {r.status_code} {r.text}"
    print("  ✓ UI-TEST-2 已删除清理")

    print("\nui_api_test: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nui_api_test: FAIL — {e}")
        sys.exit(1)
