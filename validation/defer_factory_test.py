# -*- coding: utf-8 -*-
"""失败工厂暂缓队列正式测试（W6c，对应 5367c9b W6a + f0987ce W6b）。

覆盖（方案《修改方案_暂缓队列与运行中对照_20260804.md》W6c 六条）：
1. 暂缓主路径：3 厂批次（f1 有文件夹、f2 无文件夹、f3 有文件夹）→
   挂起顺序 [f1, f3, f2]：f2 一遍时被暂缓（不进 Node5/Node6），f3 批准后
   二遍重试 f2 仍失败 → 最终挂起，payload 带 final_attempt=True +
   failure_reason="no_folder_matched"；最终批准前 master.db 无 f2 记录
   （占位数据绝不写库）；
2. 全部失败：2 厂皆无文件夹 → create 一路不挂起跑到二遍 → 逐个最终挂起
   （两次挂起 payload 均带 final_attempt）；
3. 末尾单厂不空转：1 厂批次无文件夹 → 直接挂起，无暂缓、payload 不带
   final_attempt 标记，deferred_factories 为空；
4. 对照注入：接用例 1 的 f2 最终挂起现场，tmp 补建文件夹放假单据 →
   retry_factory_extraction(tid, folder=...) 重新挂起且 mock 提取成功
   （payload 不带 final_attempt）；负例 folder="不存在"/".." → ValueError
   且挂起现场不被破坏；
5. save 持久化：接用例 3 的挂起现场，retry(folder=..., save=True) →
   返回带 alias_saved 且 tmp env 的 alias_map.json 可被 load_alias_map()
   读到该对照；负例 save=True 无 folder → ValueError；
6. 进度事件：用例 1 同型场景挂接 on_progress，断言流出过含「已暂缓」
   （Node4B）与「开始重试」（Node2 二遍弹队）文案的事件。

工厂命名约定：无文件夹的工厂与其余工厂名零字符重叠（fuzzy cutoff 40 下
绝不会误配到别人的文件夹），保证「无文件夹」场景确定性。

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 全部
指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。alias_map_copy=True：匹配行为与生产
一致，save 落盘写的是临时副本，绝不碰真文件。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 python3 validation/defer_factory_test.py
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
from sqlalchemy import select  # noqa: E402

from app.api import service  # noqa: E402
from app.db.models import Factory  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.factory_match import load_alias_map  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_defer_factory_test_", alias_map_copy=True)

# 用例 1/4：f2 与 f1/f3 零字符重叠（防 fuzzy 误配）
F1, F2, F3 = "阿尔法电子", "昆盛金属", "绿洲塑料"
# 用例 2：两厂皆无文件夹
G1, G2 = "北辰陶瓷", "南方纸业"
# 用例 3/5：单厂无文件夹
H1 = "东阳皮革"
# 用例 6：i2 与 i1/i3 零字符重叠
I1, I2, I3 = "西海玻璃", "中原纺织", "东山家具"


def _tid(tag: str) -> str:
    return f"DEFER-{tag}-{int(time.time()*1000) % 1000000}"


def _sku(case_no: int, i: int) -> str:
    """13 位条码（Node1 目标判定按 13 位条码列），按用例号分段防跨批次串号。"""
    return f"490{case_no}{i:09d}"


def _make_xlsx(path: Path, factories: list[str], case_no: int) -> str:
    """最小装箱单：Node1 只需工厂名列 + SKU 列即可解析出各厂需求（一厂一行）。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    for i, f in enumerate(factories, 1):
        ws.append([f, _sku(case_no, i), "ITEM", 10])
    wb.save(path)
    return str(path)


def _make_batch(tid: str, factories: list[str], folders: set[str],
                case_no: int, on_progress=None) -> tuple[dict, Path]:
    """造批次并跑到首个挂起：tmp 装箱单 + 上游目录（只给 folders 里的厂建文件夹）。

    返回 (run_until_interrupt 结果, 上游目录 Path)。断言首个结果必须是挂起。
    """
    xlsx = _make_xlsx(TMP / f"downstream_{tid}.xlsx", factories, case_no)
    upstream = TMP / f"upstream_{tid}"
    upstream.mkdir(exist_ok=True)
    for f in folders:
        (upstream / f).mkdir(exist_ok=True)
    r = service.run_until_interrupt(
        tid, downstream_file_path=xlsx, upstream_root=str(upstream),
        on_progress=on_progress)
    assert r["status"] == "pending_human_review", r
    return r, upstream


def _approve(tid: str, payload: dict, on_progress=None) -> dict:
    """按 payload 批准并推进（新 SKU 补录合规字段；占位数据模拟人工补录数值）。

    占位条目（提取失败）三项数值为 None，直接提交会算不出单重——这里模拟
    审核页人工补录：None 字段填上确定性数值（净 5.0 / 毛 5.3 KG 每件）。
    """
    items = []
    for i, item in enumerate(payload["items"]):
        ed = dict(item["extracted_data"] or {})
        if ed.get("total_quantity") is None:
            ed["total_quantity"] = 100
        if ed.get("total_net_weight") is None:
            ed["total_net_weight"] = 500.0
        if ed.get("total_gross_weight") is None:
            ed["total_gross_weight"] = 530.0
        h_item = {"sku": item["sku"], "extracted_data": ed}
        if item.get("is_new_sku"):
            h_item["name_cn"] = f"测试中文品名-{i + 1}"
            h_item["hs_code"] = "9404909000"
            h_item["inspection_required"] = False
        items.append(h_item)
    return service.resume_order(tid, {"approved": True, "items": items},
                                on_progress=on_progress)


def _db_factory_names() -> set[str]:
    """读（临时）master.db 的 factories 表全集——Node6 落库的唯一入口。"""
    with get_session() as s:
        return {f.factory_name for f in s.scalars(select(Factory)).all()}


def case_1_defer_main_path() -> None:
    """暂缓主路径：挂起顺序 [f1, f3, f2最终]；f2 暂缓期不挂起、不写库。"""
    tid = _tid("MAIN")
    r, upstream = _make_batch(tid, [F1, F2, F3], folders={F1, F3}, case_no=1)

    # 首挂 f1（mock 成功），非最终挂起不带标记
    p1 = r["review_data"]
    assert p1["factory_name"] == F1, p1.get("factory_name")
    assert "final_attempt" not in p1, f"首挂不应带 final_attempt: {p1.keys()}"

    # 批准 f1 → f2 应被暂缓（不挂起），流直接走到 f3 挂起
    r2 = _approve(tid, p1)
    assert r2["status"] == "pending_human_review", r2
    p3 = r2["review_data"]
    assert p3["factory_name"] == F3, \
        f"f2 应被暂缓跳过、直接挂起 f3，实际: {p3.get('factory_name')}"

    # 此刻 f2 已在暂缓队列（带失败原因），主队列已空
    values = service.get_order_state(tid)["values"]
    deferred = values.get("deferred_factories") or []
    assert [d["factory_name"] for d in deferred] == [F2], deferred
    assert deferred[0]["failure_reason"] == "no_folder_matched", deferred

    # 批准 f3 → 主队列空 → Node2 弹暂缓条目二遍重试 f2 仍失败 → 最终挂起
    r3 = _approve(tid, p3)
    assert r3["status"] == "pending_human_review", r3
    p2f = r3["review_data"]
    assert p2f["factory_name"] == F2, p2f.get("factory_name")
    assert p2f.get("final_attempt") is True, \
        f"二遍仍失败的最终挂起应带 final_attempt=True: {p2f.keys()}"
    assert p2f.get("failure_reason") == "no_folder_matched", p2f

    # 挂起顺序汇总断言：[f1, f3, f2]
    order = [p1["factory_name"], p3["factory_name"], p2f["factory_name"]]
    assert order == [F1, F3, F2], order
    print(f"  ✓ 挂起顺序 {order}：f2 一遍暂缓、二遍最终挂起"
          f"（final_attempt=True, failure_reason=no_folder_matched）")

    # 占位不写库：f1/f3 已落库，f2 在最终批准前 master.db 无其记录
    names = _db_factory_names()
    assert F1 in names and F3 in names, f"f1/f3 应已落库: {names}"
    assert F2 not in names, f"f2 最终批准前不应落库（占位不写库）: {names}"
    print(f"  ✓ 占位不写库：master.db 已有 {sorted(names)}，无「{F2}」")

    # f2 最终挂起现场留给用例 4（对照注入）接力
    case_1_defer_main_path.ctx = {"tid": tid, "upstream": upstream,
                                  "payload": p2f}


def case_2_all_failed() -> None:
    """全部失败：2 厂皆无文件夹 → 一路不挂起跑到二遍 → 逐个最终挂起。"""
    tid = _tid("ALLFAIL")
    r, _ = _make_batch(tid, [G1, G2], folders=set(), case_no=2)

    # 关键断言：create 一路不挂起（两厂一遍都进了暂缓队列），
    # 首次挂起即二遍重试的最终挂起
    p_first = r["review_data"]
    assert p_first["factory_name"] == G1, p_first.get("factory_name")
    assert p_first.get("final_attempt") is True, \
        f"全部失败时首次挂起即应带 final_attempt: {p_first.keys()}"
    assert p_first.get("failure_reason") == "no_folder_matched", p_first
    # g1 被弹出二遍时，g2 还躺在暂缓队列里
    deferred = service.get_order_state(tid)["values"].get("deferred_factories") or []
    assert [d["factory_name"] for d in deferred] == [G2], deferred
    print(f"  ✓ 两厂一遍皆暂缓、一路不挂起；首挂即 {G1} 二遍最终挂起"
          f"（暂缓队列余 {[d['factory_name'] for d in deferred]}）")

    # 人工补录批准 g1 → 逐个最终挂起：g2 二遍仍失败 → 最终挂起
    r2 = _approve(tid, p_first)
    assert r2["status"] == "pending_human_review", r2
    p_second = r2["review_data"]
    assert p_second["factory_name"] == G2, p_second.get("factory_name")
    assert p_second.get("final_attempt") is True, \
        f"第二次挂起也应带 final_attempt: {p_second.keys()}"
    assert p_second.get("failure_reason") == "no_folder_matched", p_second
    print(f"  ✓ 补录批准 {G1} 后 {G2} 最终挂起（两次挂起均 final_attempt）")


def case_3_single_factory_no_spin() -> None:
    """末尾单厂不空转：1 厂批次无文件夹 → 直接挂起，无暂缓、无 final_attempt。"""
    tid = _tid("SINGLE")
    r, upstream = _make_batch(tid, [H1], folders=set(), case_no=3)

    p = r["review_data"]
    assert p["factory_name"] == H1, p.get("factory_name")
    assert "final_attempt" not in p, \
        f"最后一个工厂首次失败应直接挂起（不空转），不带 final_attempt: {p.keys()}"
    values = service.get_order_state(tid)["values"]
    assert not (values.get("deferred_factories") or []), \
        f"单厂不应产生暂缓队列: {values.get('deferred_factories')}"
    print(f"  ✓ 单厂无文件夹直接挂起：无 final_attempt 标记，暂缓队列空")

    # 挂起现场留给用例 5（save 持久化）接力
    case_3_single_factory_no_spin.ctx = {"tid": tid, "upstream": upstream,
                                         "payload": p}


def case_4_folder_injection() -> None:
    """对照注入：f2 最终挂起现场补建文件夹 → retry(folder=) 重提成功重新挂起。"""
    ctx = case_1_defer_main_path.ctx
    tid, upstream = ctx["tid"], ctx["upstream"]

    # 负例：不存在的文件夹 / 路径穿越 → ValueError，且挂起现场不被破坏
    for bad in ("不存在的目录", ".."):
        try:
            service.retry_factory_extraction(tid, folder=bad)
        except ValueError as e:
            print(f"  ✓ 负例 folder={bad!r} 被拒：ValueError「{e}」")
        else:
            raise AssertionError(f"folder={bad!r} 应抛 ValueError")
    p_now = service.get_review_payload(tid)
    assert p_now and p_now["factory_name"] == F2, \
        f"负例不应破坏挂起现场: {p_now}"
    assert p_now.get("final_attempt") is True, p_now.keys()

    # tmp 里补建 f2 的文件夹并放一份假单据（mock 模式不读内容，有目录即可）
    folder_name = "昆盛金属补建"
    fdir = upstream / folder_name
    fdir.mkdir()
    (fdir / "装箱单.xlsx").write_bytes(b"fake-doc")

    ret = service.retry_factory_extraction(tid, folder=folder_name)
    assert ret["status"] == "pending_human_review", ret
    assert ret["factory"] == F2, ret
    rd = ret["review_data"]
    assert rd["factory_name"] == F2, rd.get("factory_name")
    assert "final_attempt" not in rd, \
        f"对照注入后提取成功，payload 不应再带 final_attempt: {rd.keys()}"
    assert (rd.get("folder_path") or "").endswith(folder_name), rd.get("folder_path")
    # mock 提取成功：数值齐全（首个 SKU mock qty = 50）
    ed = rd["items"][0]["extracted_data"]
    assert ed.get("total_quantity") == 50, ed
    print(f"  ✓ 对照注入 {F2} -> {folder_name}：重新挂起，mock 提取成功"
          f"（qty={ed['total_quantity']}，无 final_attempt 标记）")

    # 批准走完整批，f2 落库（闭环：对照注入后人工审核通过）
    r_done = _approve(tid, rd)
    assert r_done["status"] == "success", r_done
    assert F2 in _db_factory_names(), f"批准后 f2 应落库: {_db_factory_names()}"
    print(f"  ✓ 批准走完整批：「{F2}」已落库")


def case_5_save_persistence() -> None:
    """save 持久化：retry(folder=..., save=True) 对照落 alias_map.json 可读回。"""
    ctx = case_3_single_factory_no_spin.ctx
    tid, upstream = ctx["tid"], ctx["upstream"]

    # 负例：save=True 无 folder → ValueError（无对照可保存，防误用）
    try:
        service.retry_factory_extraction(tid, save=True)
    except ValueError as e:
        assert "folder" in str(e), f"错误文案应指向 folder: {e}"
        print(f"  ✓ 负例 save=True 无 folder 被拒：ValueError「{e}」")
    else:
        raise AssertionError("save=True 无 folder 应抛 ValueError")

    # 补建文件夹（名 ≠ 工厂名，正是对照场景）+ 假单据
    folder_name = "东阳皮革仓"
    fdir = upstream / folder_name
    fdir.mkdir()
    (fdir / "送货单.pdf").write_bytes(b"fake-doc")

    ret = service.retry_factory_extraction(tid, folder=folder_name, save=True)
    assert ret["status"] == "pending_human_review", ret
    assert (ret.get("alias_saved") or {}).get("saved") == 1, ret
    # tmp env 的 alias_map.json 可被 load_alias_map() 读到该对照
    amap = load_alias_map()
    assert amap.get(H1) == folder_name, \
        f"alias_map 应含 {H1} -> {folder_name}: 实际 {amap.get(H1)!r}"
    print(f"  ✓ save=True：对照 {H1} -> {folder_name} 已永久落盘并可读回"
          f"（alias_saved={ret['alias_saved']}）")

    # 收尾：批准走完整批
    r_done = _approve(tid, ret["review_data"])
    assert r_done["status"] == "success", r_done


def case_6_progress_events() -> None:
    """进度事件：on_progress 收集到「已暂缓」（Node4B）与「开始重试」（Node2 二遍）。"""
    events: list[dict] = []

    def _collect(ev: dict) -> None:
        events.append(ev)

    tid = _tid("PROGRESS")
    r, _ = _make_batch(tid, [I1, I2, I3], folders={I1, I3}, case_no=6,
                       on_progress=_collect)
    p1 = r["review_data"]
    assert p1["factory_name"] == I1, p1.get("factory_name")

    # 批准 i1：i2 在此段被 Node4B 暂缓（「已暂缓」事件应在此流出）
    r2 = _approve(tid, p1, on_progress=_collect)
    p3 = r2["review_data"]
    assert p3["factory_name"] == I3, p3.get("factory_name")

    # 批准 i3：Node2 弹暂缓队列二遍重试 i2（「开始重试」事件应在此流出）
    r3 = _approve(tid, p3, on_progress=_collect)
    p2f = r3["review_data"]
    assert p2f["factory_name"] == I2, p2f.get("factory_name")
    assert p2f.get("final_attempt") is True, p2f.keys()

    msgs = [str(e.get("message") or "") for e in events]
    defer_msgs = [m for m in msgs if "已暂缓" in m and I2 in m]
    retry_msgs = [m for m in msgs if "开始重试" in m and I2 in m]
    assert defer_msgs, f"应出现过含「已暂缓」的 {I2} 事件，全部事件: {msgs}"
    assert retry_msgs, f"应出现过含「开始重试」的 {I2} 事件，全部事件: {msgs}"
    print(f"  ✓ 暂缓事件：「{defer_msgs[0]}」")
    print(f"  ✓ 重试事件：「{retry_msgs[0]}」")


CASES = [
    ("1. 暂缓主路径：挂起顺序 [f1, f3, f2最终]，f2 暂缓期不挂起不写库",
     case_1_defer_main_path),
    ("2. 全部失败：2 厂皆无文件夹，一路不挂起跑到二遍逐个最终挂起",
     case_2_all_failed),
    ("3. 末尾单厂不空转：1 厂无文件夹直接挂起（无暂缓无标记）",
     case_3_single_factory_no_spin),
    ("4. 对照注入：补建文件夹 retry(folder=) 重提成功 + 负例 ValueError",
     case_4_folder_injection),
    ("5. save 持久化：retry(save=True) 对照落 alias_map 可读回 + 负例",
     case_5_save_persistence),
    ("6. 进度事件：「已暂缓」与「开始重试」文案均流出",
     case_6_progress_events),
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
        print("🎉 失败工厂暂缓队列全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
