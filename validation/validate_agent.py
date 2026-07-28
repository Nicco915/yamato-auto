# -*- coding: utf-8 -*-
"""提取 Agent 端到端验证：extract_factory 跑 10 个工厂，对照 ground truth。

用法：LLM_ENABLE_THINKING=0 python3 validate_agent.py [工厂名]
结果 JSON 写入 validation/results/agent_<时间戳>.json。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extraction.agent import extract_factory, format_report  # noqa: E402
from ground_truth import FACTORY_FOLDER, get_factory_ground_truth, list_factories  # noqa: E402

TOL = 0.01


def check_factory(factory: str) -> dict:
    folder = f"{FACTORY_FOLDER}/{factory}"
    gt = get_factory_ground_truth(factory)
    t0 = time.time()
    report = extract_factory(folder)
    elapsed = time.time() - t0

    per_sku = {}
    ok_n = 0
    for sku, g in gt.items():
        cands = [it for it in report.items if it.sku_code == sku]
        hit = any(
            it.total_quantity == g["total_quantity"]
            and abs((it.total_net_weight or -1) - g["total_net_weight"]) < TOL
            and abs((it.total_gross_weight or -1) - g["total_gross_weight"]) < TOL
            for it in cands
        )
        ok_n += hit
        per_sku[sku] = {
            "gt": {k: g[k] for k in ("total_quantity", "total_net_weight", "total_gross_weight")},
            "extracted": [
                {"qty": it.total_quantity, "nw": it.total_net_weight, "gw": it.total_gross_weight,
                 "review": it.needs_human_review}
                for it in cands
            ],
            "hit": hit,
        }
    return {
        "factory": factory,
        "elapsed_sec": round(elapsed, 1),
        "sku_total": len(gt),
        "sku_hit": ok_n,
        "blocking": report.has_blocking,
        "issues": [
            {"level": i.level, "type": i.type, "message": i.message, "file": i.file}
            for i in report.issues
        ],
        "targets": report.targets,
        "stats": report.stats,
        "per_sku": per_sku,
    }


def main() -> int:
    factories = [sys.argv[1]] if len(sys.argv) > 1 else list_factories()
    results = []
    for f in factories:
        print(f"[运行] {f} ...", flush=True)
        r = check_factory(f)
        results.append(r)
        mark = "✓" if r["sku_hit"] == r["sku_total"] and not r["blocking"] else "✗"
        n_issue = len([i for i in r["issues"] if i["level"] != "info"])
        print(f"  {mark} 命中 {r['sku_hit']}/{r['sku_total']}  "
              f"耗时 {r['elapsed_sec']}s  反馈 {n_issue} 条", flush=True)
        for i in r["issues"]:
            if i["level"] != "info":
                print(f"    {i['level']} {i['type']}: {i['message'][:100]}", flush=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).parent / "results" / f"agent_{ts}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(r["sku_total"] for r in results)
    hit = sum(r["sku_hit"] for r in results)
    print(f"\n总计: {hit}/{total} SKU 全字段命中 | 明细: {out}")
    return 0 if hit == total else 1


if __name__ == "__main__":
    sys.exit(main())
