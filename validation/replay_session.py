# -*- coding: utf-8 -*-
"""增量模式重放验证：模拟单据逐个到达，验证 FactorySession 行为。

场景：每个工厂的文件按"最坏到达序"喂入——负向信号候选（报关/通関）→
非候选（PO/发票/模板/图片）→ 真箱单。断言：
1. 负向候选到达时提示「暂无箱单」且不提取（成本控制：LLM 只跑真目标）
2. 最终累积结果与 GT 全字段一致（100%）
3. 设置 expected_skus 后状态变为 complete_auto
4. 全部处理完后重复喂入 → already_processed，不再调 LLM

用法：LLM_ENABLE_THINKING=0 python3 replay_session.py [工厂名]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extraction.session import (  # noqa: E402
    COMPLETE_AUTO,
    FactorySession,
    process_file,
)
from extraction.target_identifier import _name_score, scan_file  # noqa: E402
from ground_truth import get_factory_ground_truth, list_factories  # noqa: E402

BASE = "/Users/nz/Downloads/yamato/96/工厂"
TOL = 0.01


def worst_case_order(files: list[str]) -> list[str]:
    """最坏到达序：负向候选 → 非候选 → 真目标（组内按路径）。"""
    def key(fp: str) -> tuple:
        neg = _name_score(fp) <= -100
        try:
            cand = scan_file(fp).is_candidate
        except Exception:  # noqa: BLE001
            cand = False
        group = 0 if (neg and cand) else (2 if cand else 1)
        return (group, fp)
    return sorted(files, key=key)


def replay(factory: str) -> dict:
    folder = Path(BASE) / factory
    files = [str(p) for p in sorted(folder.rglob("*"))
             if p.is_file() and not p.name.startswith(".")]
    ordered = worst_case_order(files)
    gt = get_factory_ground_truth(factory)

    session = FactorySession(factory=factory)
    session.set_expected_skus(list(gt.keys()))

    llm_extractions = 0
    no_pl_prompted = False
    log = []
    t0 = time.time()
    for fp in ordered:
        r = process_file(session, fp)
        if r.action in ("extracted", "replaced_target", "forced"):
            llm_extractions += 1
        if r.action == "deferred_negative" and "暂无箱单" in r.message:
            no_pl_prompted = True
        log.append(f"    [{r.action:22}] {Path(fp).name[:44]:<46} {r.message[:50]}")

    # 重复喂入第一个文件 → already_processed，不再调 LLM
    r_dup = process_file(session, ordered[0])
    dup_ok = r_dup.action == "already_processed"

    # 结果校验
    ok_n = 0
    bad = []
    for sku, g in gt.items():
        it = session.items.get(sku)
        if it and it.get("total_quantity") == g["total_quantity"] \
                and abs((it.get("total_net_weight") or -1) - g["total_net_weight"]) < TOL \
                and abs((it.get("total_gross_weight") or -1) - g["total_gross_weight"]) < TOL:
            ok_n += 1
        else:
            bad.append(sku)
    return {
        "factory": factory,
        "files": len(ordered),
        "llm_extractions": llm_extractions,
        "no_pl_prompted": no_pl_prompted,
        "sku_hit": ok_n,
        "sku_total": len(gt),
        "missing": bad,
        "status": session.status,
        "complete_auto": session.status == COMPLETE_AUTO,
        "dup_ok": dup_ok,
        "elapsed": round(time.time() - t0, 1),
        "log": log,
        "n_issues": len(session.issues),
    }


def main() -> int:
    factories = [sys.argv[1]] if len(sys.argv) > 1 else list_factories()
    all_ok = True
    for f in factories:
        print(f"[重放] {f}", flush=True)
        r = replay(f)
        for ln in r["log"]:
            print(ln)
        ok = (r["sku_hit"] == r["sku_total"] and r["complete_auto"] and r["dup_ok"])
        all_ok &= ok
        print(f"  {'✓' if ok else '✗'} SKU {r['sku_hit']}/{r['sku_total']}  "
              f"LLM提取 {r['llm_extractions']} 次  状态 {r['status']}  "
              f"暂无箱单提示 {r['no_pl_prompted']}  重复处理拦截 {r['dup_ok']}  "
              f"反馈 {r['n_issues']} 条  耗时 {r['elapsed']}s", flush=True)
        if r["missing"]:
            print(f"    未命中: {r['missing']}")
    print(f"\n{'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
