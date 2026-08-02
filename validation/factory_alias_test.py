# -*- coding: utf-8 -*-
"""W5 工厂名对照求证测试（factory_match 五档匹配 / 预扫三档 / alias 落盘 /
Node2 overrides / dispatcher 两轮确认端到端）。

覆盖：
1. match_factory_folder 五档顺序与落档（override→alias→alias_ci→exact→fuzzy→none，
   override 指向不存在文件夹时落后续档）；
2. recommend_candidates 包含信号（天津市依依衛生用品 ⊃ 依依，保底 70 分）；
3. prescan_factory_aliases 三档（临时装箱单 + 临时上游目录：确定/候选/无候选）；
4. save_alias_entries 备份/原子写/覆盖警告/并发串行 + 损坏 alias 容错；
5. Node2（folder_router）带 overrides 命中、不带 overrides 行为回归（fuzzy 照旧）、
   损坏 alias_map 不打崩 Node2；
6. dispatcher 剧本端到端（DISPATCHER_MOCK=1）：无 decisions → pending_confirmation
   带 factory_scan；带 decisions confirm → state 含 factory_alias_overrides；
   save=false 不动 alias 文件，save=true 追加落盘（.bak + 原子写）。

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 全部指向
临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/factory_alias_test.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook  # noqa: E402

from app import dispatcher  # noqa: E402
from app import factory_match as fm  # noqa: E402
from app.api import service  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.dispatcher import loop  # noqa: E402
from app.nodes.folder_router import folder_router  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 夹具目录（隔离前先建好，ALIAS_MAP_PATH 随隔离指向它）----
import tempfile  # noqa: E402

FIX = Path(tempfile.mkdtemp(prefix="yamato_fa_fixture_"))
ALIAS_PATH = FIX / "alias_map.json"
ALIAS_PATH.write_text("{}\n", encoding="utf-8")

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_fa_test_",
                     extra_env={"ALIAS_MAP_PATH": str(ALIAS_PATH)})


def _make_xlsx(path: Path, rows: list[tuple[str, str]]) -> str:
    """最小下游装箱单（Node1 只需 MAKER_MEI_KJ/SHOHIN_CD，四列与冒烟一致）。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    for factory, sku in rows:
        ws.append([factory, sku, "ITEM", 10])
    wb.save(path)
    return str(path)


def _set_script(items: list[dict]) -> None:
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(items)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_match_five_tiers() -> None:
    """五档顺序与落档。"""
    folders = ["ov目录", "alias目录", "Top", "ab", "东基恒"]

    # 1) override 最高优先
    hit = fm.match_factory_folder("厂F", folders, {"厂F": "alias目录"}, 40,
                                  overrides={"厂F": "ov目录"})
    assert hit == ("ov目录", 100.0, "override"), hit
    # override 指向不存在文件夹 → 落后续档（alias）
    hit = fm.match_factory_folder("厂F", folders, {"厂F": "alias目录"}, 40,
                                  overrides={"厂F": "不存在目录"})
    assert hit == ("alias目录", 100.0, "alias"), hit
    print("  ✓ override 命中 / override 落档到 alias")

    # 2) alias 精确
    hit = fm.match_factory_folder("厂F", folders, {"厂F": "alias目录"}, 40)
    assert hit == ("alias目录", 100.0, "alias"), hit
    # 3) alias 大小写不敏感
    hit = fm.match_factory_folder("厂G", folders, {"厂G": "TOP"}, 40)
    assert hit == ("Top", 100.0, "alias_ci"), hit
    print("  ✓ alias 精确 / alias_ci 大小写兜底")

    # 4) 规范化精确（去空白小写）
    hit = fm.match_factory_folder("A B", folders, {}, 40)
    assert hit == ("ab", 100.0, "exact"), hit
    # 5) fuzzy 兜底
    hit = fm.match_factory_folder("东基恒更新", folders, {}, 40)
    assert hit[0] == "东基恒" and hit[2] == "fuzzy" and hit[1] >= 40, hit
    print("  ✓ exact 规范化 / fuzzy 兜底")

    # none：低于 cutoff / 空目录
    hit = fm.match_factory_folder("完全不相关zzz", folders, {}, 40)
    assert hit[2] == "none" and hit[0] is None, hit
    hit = fm.match_factory_folder("任意", [], {}, 40)
    assert hit == (None, 0.0, "none"), hit
    print("  ✓ 低分/空目录 → none")


def case_2_recommend_contains_signal() -> None:
    """recommend_candidates 包含信号：依依 case 保底 70 分入候选。"""
    cands = fm.recommend_candidates(
        "天津市依依衛生用品有限公司", ["依依", "无关甲厂", "无关乙厂"], cutoff=40)
    by_folder = {c["folder"]: c for c in cands}
    assert "依依" in by_folder, f"包含信号候选漏召: {cands}"
    entry = by_folder["依依"]
    assert "contains" in entry["signals"], entry
    assert entry["score"] >= 70.0, f"包含信号应保底 70 分: {entry}"
    print(f"  ✓ 依依 入候选：score={entry['score']:.0f} signals={entry['signals']}")


def case_3_prescan_three_tiers() -> None:
    """prescan 三档：确定命中 / 低置信推荐 / 无候选。"""
    xlsx = _make_xlsx(FIX / "downstream3.xlsx", [
        ("工厂A", "4900000000001"),
        ("依依衛生用品", "4900000000002"),
        ("ZZZ幽灵厂", "4900000000003"),
    ])
    upstream = FIX / "upstream3"
    (upstream / "工厂A").mkdir(parents=True)
    (upstream / "依依").mkdir(parents=True)

    scan = service.prescan_factory_aliases(xlsx, str(upstream))
    assert scan["resolved"].get("工厂A", {}).get("folder") == "工厂A", scan
    assert scan["resolved"]["工厂A"]["method"] in ("alias", "alias_ci", "exact"), scan
    assert "依依衛生用品" in scan["candidates"], scan
    cand_folders = [c["folder"] for c in scan["candidates"]["依依衛生用品"]]
    assert "依依" in cand_folders, scan["candidates"]
    assert "ZZZ幽灵厂" in scan["unmatched"], scan
    print(f"  ✓ 三档：resolved={list(scan['resolved'])} "
          f"candidates={list(scan['candidates'])} unmatched={scan['unmatched']}")

    # 上游目录不存在：warnings + 全部 unmatched，不抛异常
    scan2 = service.prescan_factory_aliases(xlsx, str(FIX / "不存在目录"))
    assert scan2["warnings"] and not scan2["resolved"], scan2
    assert set(scan2["unmatched"]) == {"工厂A", "依依衛生用品", "ZZZ幽灵厂"}, scan2
    print("  ✓ 上游目录不存在 → warnings + 全部 unmatched（不抛）")


def case_4_save_alias_entries() -> None:
    """save_alias_entries：备份/原子写/覆盖/并发 + 损坏容错。"""
    p = FIX / "alias_save.json"
    p.write_text(json.dumps({"K1": "V1"}, ensure_ascii=False), encoding="utf-8")

    r = fm.save_alias_entries({"K2": "V2"}, path=p)
    assert r["saved"] == 1 and r["overwritten"] == [], r
    assert (p.with_name(p.name + ".bak")).exists(), "缺 .bak 备份"
    assert not (p.with_name(p.name + ".tmp")).exists(), "临时文件残留（非原子写）"
    merged = json.loads(p.read_text(encoding="utf-8"))
    assert merged == {"K1": "V1", "K2": "V2"}, merged
    print("  ✓ 追加保存：.bak 备份 + 原子写（无 .tmp 残留）+ 合并正确")

    r = fm.save_alias_entries({"K1": "V9"}, path=p)
    assert r["overwritten"] == ["K1"], r
    merged = json.loads(p.read_text(encoding="utf-8"))
    assert merged["K1"] == "V9" and merged["K2"] == "V2", merged
    print("  ✓ 覆盖既有 key 记入 overwritten")

    # 并发：两线程各存一个 key，最终都在
    t1 = threading.Thread(target=fm.save_alias_entries,
                          args=({"T1": "x"},), kwargs={"path": p})
    t2 = threading.Thread(target=fm.save_alias_entries,
                          args=({"T2": "y"},), kwargs={"path": p})
    t1.start(); t2.start(); t1.join(); t2.join()
    merged = json.loads(p.read_text(encoding="utf-8"))
    assert merged.get("T1") == "x" and merged.get("T2") == "y", merged
    print("  ✓ 并发保存互不踩踏（锁串行）")

    # 损坏 JSON → 空表容错不抛
    bad = FIX / "alias_broken.json"
    bad.write_text("{broken json", encoding="utf-8")
    assert fm.load_alias_map(bad) == {}
    print("  ✓ 损坏 alias_map 按空表容错（不抛）")


def case_5_node2_overrides() -> None:
    """Node2：带 overrides 命中 / 不带行为回归 / 损坏 alias 不打崩。"""
    upstream = FIX / "upstream5"
    (upstream / "依依").mkdir(parents=True)
    (upstream / "备用").mkdir(parents=True)
    factory = "天津市依依"

    base_state = {
        "pending_factories": [factory],
        "downstream_requirements": {factory: ["4900000000001"]},
        "upstream_root": str(upstream),
    }

    # 不带 overrides：行为回归——fuzzy 照旧命中「依依」（ratio≈57 ≥ 40）
    out = folder_router(dict(base_state))
    cur = out["current_factory_data"]
    assert cur["folder_path"] == str(upstream / "依依"), cur
    print(f"  ✓ 不带 overrides：fuzzy 命中 依依（score={cur['match_score']:.0f}）")

    # 带 overrides：最高优先档命中「备用」
    out = folder_router({**base_state,
                         "factory_alias_overrides": {factory: "备用"}})
    cur = out["current_factory_data"]
    assert cur["folder_path"] == str(upstream / "备用"), cur
    assert cur["match_score"] == 100.0, cur
    print("  ✓ 带 overrides：override 档命中 备用（100 分）")

    # 损坏 alias_map：Node2 不崩，仍走 fuzzy
    os.environ["ALIAS_MAP_PATH"] = str(FIX / "alias_broken.json")
    get_settings.cache_clear()
    try:
        out = folder_router(dict(base_state))
        assert out["current_factory_data"]["folder_path"] == str(upstream / "依依"), out
    finally:
        os.environ["ALIAS_MAP_PATH"] = str(ALIAS_PATH)
        get_settings.cache_clear()
    print("  ✓ 损坏 alias_map 不打崩 Node2（按空表兜底）")


def case_6_dispatcher_two_rounds() -> None:
    """dispatcher 端到端：预扫进 pending → decisions confirm → overrides/落盘。"""
    factory = "天津市依依"
    xlsx = _make_xlsx(FIX / "downstream6.xlsx", [(factory, "4900000000001")])
    upstream = FIX / "upstream6"
    (upstream / "备用").mkdir(parents=True)

    # ---- 轮1：无 decisions → pending_confirmation 带 factory_scan ----
    tid1 = f"FA-TEST-R1-{int(time.time()*1000) % 100000}"
    sid1 = f"FA-TEST-S1-{int(time.time()*1000)}"
    _set_script([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid1,
                                  "downstream_file_path": xlsx,
                                  "upstream_root": str(upstream)}}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid1}", session_id=sid1)
    assert r["status"] == "pending_confirmation", r
    assert r.get("factory_scan") is not None, "响应缺 factory_scan"
    assert r["action"].get("factory_scan") is not None, "信封缺 factory_scan"
    scan = r["factory_scan"]
    # 天津市依依 vs 备用：无候选 → unmatched（或低分候选），总之不是确定命中
    assert factory not in (scan.get("resolved") or {}), scan
    assert factory in (scan.get("unmatched") or []) \
        or factory in (scan.get("candidates") or {}), scan
    print(f"  ✓ 轮1：pending_confirmation 带 factory_scan"
          f"（unmatched={scan.get('unmatched')}）")

    # ---- 轮2：带 decisions（save=false）confirm → overrides 进 state，不落盘 ----
    tid2 = f"FA-TEST-R2-{int(time.time()*1000) % 100000}"
    sid2 = f"FA-TEST-S2-{int(time.time()*1000)}"
    _set_script([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid2,
                                  "downstream_file_path": xlsx,
                                  "upstream_root": str(upstream),
                                  "alias_decisions": [
                                      {"factory": factory, "folder": "备用",
                                       "save": False}]}}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid2}，{factory} 用 备用 仅本次",
                                  session_id=sid2)
    assert r["status"] == "pending_confirmation", r
    preview_text = "\n".join(r["action"]["preview_lines"])
    assert "[仅本次]" in preview_text, preview_text
    r2 = dispatcher.confirm(sid2, None)
    assert r2["status"] == "applied", r2
    state = service.get_order_state(tid2)
    overrides = state["values"].get("factory_alias_overrides")
    assert overrides == {factory: "备用"}, f"overrides 未进 state: {overrides}"
    # save=false：alias 文件不动
    alias_now = fm.load_alias_map(ALIAS_PATH)
    assert factory not in alias_now, f"save=false 不应落盘: {alias_now}"
    print("  ✓ 轮2（save=false）：state 含 overrides，alias 文件未动")

    # ---- 轮3：save=true confirm → 追加落盘（.bak + 原子写）----
    tid3 = f"FA-TEST-R3-{int(time.time()*1000) % 100000}"
    sid3 = f"FA-TEST-S3-{int(time.time()*1000)}"
    _set_script([
        {"tool_calls": [{"id": "c1", "name": "create_batch",
                         "args": {"thread_id": tid3,
                                  "downstream_file_path": xlsx,
                                  "upstream_root": str(upstream),
                                  "alias_decisions": [
                                      {"factory": factory, "folder": "备用",
                                       "save": True}]}}]},
    ])
    r = dispatcher.handle_message(f"发起批次 {tid3}，{factory} 永久对照 备用",
                                  session_id=sid3)
    assert r["status"] == "pending_confirmation", r
    preview_text = "\n".join(r["action"]["preview_lines"])
    assert "[永久保存]" in preview_text, preview_text
    r3 = dispatcher.confirm(sid3, None)
    assert r3["status"] == "applied", r3
    alias_now = fm.load_alias_map(ALIAS_PATH)
    assert alias_now.get(factory) == "备用", f"save=true 应落盘: {alias_now}"
    assert (ALIAS_PATH.with_name(ALIAS_PATH.name + ".bak")).exists(), "缺 .bak"
    assert not (ALIAS_PATH.with_name(ALIAS_PATH.name + ".tmp")).exists()
    saved = (r3["result"] or {}).get("alias_saved")
    assert saved and saved["saved"] == 1, r3["result"]
    print("  ✓ 轮3（save=true）：alias 已追加落盘（.bak + 原子写）")


CASES = [
    ("1. match_factory_folder 五档顺序与落档", case_1_match_five_tiers),
    ("2. recommend_candidates 包含信号（依依）", case_2_recommend_contains_signal),
    ("3. prescan 三档", case_3_prescan_three_tiers),
    ("4. save_alias_entries 备份/原子写/覆盖/并发", case_4_save_alias_entries),
    ("5. Node2 overrides 命中 + 行为回归", case_5_node2_overrides),
    ("6. dispatcher 两轮确认端到端", case_6_dispatcher_two_rounds),
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
        print("🎉 W5 工厂名对照求证全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
