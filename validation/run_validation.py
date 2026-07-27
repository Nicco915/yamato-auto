# -*- coding: utf-8 -*-
"""提取验证 CLI。

用法：
  python3 run_validation.py --factory 中地 [--max-files 2]   # 单工厂（可限文件数控制成本）
  python3 run_validation.py --all [--max-files 3]            # 全部工厂
  python3 run_validation.py --mock --all                     # 不调 LLM，假数据走通全流程自检

结果 JSON 写入 validation/results/，汇总报告写入 validation/report.md。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# 让脚本可以直接 import app/app 下的 extraction 包
APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "app"))

import extraction  # noqa: E402
from extraction import llm_client  # noqa: E402
from extraction.excel_channel import ChannelResult  # noqa: E402
from extraction.schemas import ExtractedItem  # noqa: E402

from ground_truth import (  # noqa: E402
    FACTORY_FOLDER,
    get_factory_ground_truth,
    list_factories,
)

RESULTS_DIR = Path(__file__).parent / "results"
REPORT_PATH = Path(__file__).parent / "report.md"

TOLERANCE = 0.005  # 字段对比容差 0.5%
NAME_MATCH_THRESHOLD = 0.6  # 品名模糊匹配阈值（无 sku_code 时的兜底）


# ---------------------------------------------------------------------------
# Mock 模式：不调用 LLM，用 ground truth 造假提取结果，走通全流程
# ---------------------------------------------------------------------------

def _install_mock(gt: dict) -> None:
    """把 pipeline 内的两个通道函数替换为假实现（数据直接来自 ground truth）。"""
    fake_items = [
        ExtractedItem(
            sku_name=(info["names"][0] if info["names"] else sku),
            sku_code=sku,
            total_quantity=info["total_quantity"],
            total_net_weight=info["total_net_weight"],
            total_gross_weight=info["total_gross_weight"],
            weight_unit="KG",
            needs_human_review=False,
        )
        for sku, info in gt.items()
    ]

    def fake_channel(file_path: str) -> ChannelResult:
        items = [
            it.model_copy(update={"source_file": file_path}) for it in fake_items
        ]
        return ChannelResult(items=items, json_attempts=1, json_parse_failures=0)

    extraction.pipeline.extract_excel = fake_channel
    extraction.pipeline.extract_vision = fake_channel


def _install_max_files(n: int) -> None:
    """限制每个工厂处理的文件数（控制成本）。"""
    orig = extraction.pipeline._iter_files

    def limited(folder_path: str):
        return orig(folder_path)[:n]

    extraction.pipeline._iter_files = limited


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def _norm_code(code) -> str:
    """归一化商品编码：只保留数字。"""
    return re.sub(r"\D", "", str(code or ""))


def _norm_name(name) -> str:
    return re.sub(r"\s+", "", str(name or "")).upper()


def _close(a, b, tol: float = TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    if b == 0:
        return abs(a) < 1e-9
    return abs(a - b) / abs(b) <= tol


def _match_items(gt_sku: str, gt_names: list[str], extracted: list[dict]) -> list[dict]:
    """把提取结果匹配到某个 ground truth SKU：sku_code 命中与品名模糊命中取并集。

    注意不能用"编码命中就短路"：同一 SKU 可能在 A 文件带编码但只给单件重量、
    在 B 文件不带编码但给了合计重量，两者都要纳入候选。
    """
    code = _norm_code(gt_sku)
    candidates: list[dict] = []
    seen: set[int] = set()

    def _add(it: dict) -> None:
        if id(it) not in seen:
            seen.add(id(it))
            candidates.append(it)

    if code:
        for it in extracted:
            if _norm_code(it.get("sku_code")) == code:
                _add(it)
    gt_norms = [_norm_name(n) for n in gt_names if n]
    for it in extracted:
        name = _norm_name(it.get("sku_name"))
        if not name:
            continue
        for gn in gt_norms:
            if gn and (name in gn or gn in name or SequenceMatcher(None, name, gn).ratio() >= NAME_MATCH_THRESHOLD):
                _add(it)
                break
    return candidates


def compute_metrics(factory: str, report, gt: dict) -> dict:
    """对单个工厂计算字段级准确率、SKU 覆盖率等指标。"""
    extracted: list[dict] = list(report)
    review_count = sum(1 for it in extracted if it.get("needs_human_review"))

    per_sku: dict[str, dict] = {}
    covered = 0
    field_correct = {"total_quantity": 0, "total_net_weight": 0, "total_gross_weight": 0}

    for sku, info in gt.items():
        candidates = _match_items(sku, info.get("names", []), extracted)
        sku_result = {
            "gt": {k: info[k] for k in ("total_quantity", "total_net_weight", "total_gross_weight")},
            "matched_items": len(candidates),
            "match_by": "sku_code"
            if any(_norm_code(c.get("sku_code")) == _norm_code(sku) for c in candidates)
            else ("name" if candidates else "none"),
            "fields": {},
            "all_correct": False,
        }
        if candidates:
            covered += 1
            all_ok = True
            for field in field_correct:
                gt_val = info[field]
                vals = [c.get(field) for c in candidates if c.get(field) is not None]
                single_hit = any(_close(v, gt_val) for v in vals)
                # 多文件拆分场景：同一 SKU 的数值可能分散在不同文件，尝试去重求和
                dedup_vals = list({(c.get("source_file"), c.get(field)) for c in candidates if c.get(field) is not None})
                sum_hit = _close(sum(v for _, v in dedup_vals), gt_val) if dedup_vals else False
                ok = single_hit or sum_hit
                sku_result["fields"][field] = {
                    "gt": gt_val,
                    "extracted_values": vals[:6],
                    "correct": ok,
                    "via": "single" if single_hit else ("sum" if sum_hit else "none"),
                }
                if ok:
                    field_correct[field] += 1
                else:
                    all_ok = False
            sku_result["all_correct"] = all_ok
        per_sku[sku] = sku_result

    total_skus = len(gt)
    stats = getattr(report, "stats", {}) or {}
    return {
        "factory": factory,
        "sku_total": total_skus,
        "sku_covered": covered,
        "sku_coverage": round(covered / total_skus, 4) if total_skus else None,
        "sku_all_fields_correct": sum(1 for r in per_sku.values() if r["all_correct"]),
        "field_accuracy": {
            f: (round(c / total_skus, 4) if total_skus else None)
            for f, c in field_correct.items()
        },
        "extracted_item_count": len(extracted),
        "needs_human_review_rate": (
            round(review_count / len(extracted), 4) if extracted else None
        ),
        "json_parse_success_rate": stats.get("json_parse_success_rate"),
        "unsupported_files": getattr(report, "unsupported_files", []),
        "file_errors": getattr(report, "file_errors", {}),
        "token_usage": stats.get("token_usage", {}),
        "raw_items": extracted,  # 原始提取结果（失败案例分析用）
        "per_sku": per_sku,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_factory(factory: str, mock: bool) -> dict:
    folder = str(Path(FACTORY_FOLDER) / factory)
    gt = get_factory_ground_truth(factory)
    if mock:
        _install_mock(gt)
    t0 = time.time()
    report = extraction.extract_folder(folder)
    elapsed = time.time() - t0
    metrics = compute_metrics(factory, report, gt)
    metrics["elapsed_sec"] = round(elapsed, 1)
    metrics["mock"] = mock
    return metrics


def save_results(all_metrics: list[dict], mock: bool) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "mock" if mock else "real"
    path = RESULTS_DIR / f"validation_{tag}_{ts}.json"
    path.write_text(
        json.dumps(all_metrics, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


def generate_report(all_metrics: list[dict], mock: bool, results_path: Path) -> None:
    """生成中文 Markdown 报告。"""
    lines: list[str] = []
    mode = "Mock 自检（未调用 LLM）" if mock else "真实 LLM 提取"
    lines.append(f"# 提取引擎验证报告（{mode}）\n")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 结果明细 JSON：`{results_path}`")
    lines.append("- Ground truth：`96/报关匹配.xlsx` 的「バンニングリスト」sheet"
                 "（人工报关产出，812 行覆盖 10 个工厂，净重/毛重/件数列 100% 非空，"
                 "已与工厂原始装箱单抽样核对一致）\n")

    total_sku = sum(m["sku_total"] for m in all_metrics)
    total_cov = sum(m["sku_covered"] for m in all_metrics)
    total_all_ok = sum(m["sku_all_fields_correct"] for m in all_metrics)
    field_totals = {f: sum(int((m["field_accuracy"][f] or 0) * m["sku_total"]) for m in all_metrics)
                    for f in ("total_quantity", "total_net_weight", "total_gross_weight")}

    lines.append("## 整体指标\n")
    if total_sku:
        lines.append(f"- SKU 覆盖率：**{total_cov}/{total_sku} = {total_cov/total_sku:.1%}**")
        lines.append(f"- SKU 全字段正确率：**{total_all_ok}/{total_sku} = {total_all_ok/total_sku:.1%}**")
        for f, c in field_totals.items():
            lines.append(f"- 字段准确率 {f}：{c}/{total_sku} = {c/total_sku:.1%}")
    rates = [m["needs_human_review_rate"] for m in all_metrics if m["needs_human_review_rate"] is not None]
    if rates:
        lines.append(f"- needs_human_review 触发率（按工厂平均）：{sum(rates)/len(rates):.1%}")
    jrates = [m["json_parse_success_rate"] for m in all_metrics if m["json_parse_success_rate"] is not None]
    if jrates:
        lines.append(f"- JSON 解析成功率（按工厂平均）：{sum(jrates)/len(jrates):.1%}")
    lines.append("")

    lines.append("## 分工厂明细\n")
    lines.append("| 工厂 | SKU数 | 覆盖率 | 全字段正确率 | 件数准确率 | 净重准确率 | 毛重准确率 | 人工审核率 | 耗时(s) |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for m in all_metrics:
        fa = m["field_accuracy"]
        fmt = lambda x: f"{x:.1%}" if x is not None else "-"
        lines.append(
            f"| {m['factory']} | {m['sku_total']} | {fmt(m['sku_coverage'])} "
            f"| {fmt(m['sku_all_fields_correct']/m['sku_total'] if m['sku_total'] else None)} "
            f"| {fmt(fa['total_quantity'])} | {fmt(fa['total_net_weight'])} | {fmt(fa['total_gross_weight'])} "
            f"| {fmt(m['needs_human_review_rate'])} | {m['elapsed_sec']} |"
        )
    lines.append("")

    # 失败案例分析
    lines.append("## 失败案例分析\n")
    any_fail = False
    for m in all_metrics:
        fails = {s: r for s, r in m["per_sku"].items() if not r["all_correct"]}
        if not fails and not m["file_errors"] and not m["unsupported_files"]:
            continue
        any_fail = True
        lines.append(f"### {m['factory']}")
        if m["unsupported_files"]:
            lines.append(f"- 不支持文件 {len(m['unsupported_files'])} 个："
                         + "、".join(Path(f).name for f in m["unsupported_files"][:5]))
        if m["file_errors"]:
            for f, e in list(m["file_errors"].items())[:5]:
                lines.append(f"- 文件错误 `{Path(f).name}`：{e[:150]}")
        no_match = [s for s, r in fails.items() if r["matched_items"] == 0]
        wrong = [s for s, r in fails.items() if r["matched_items"] > 0]
        if no_match:
            lines.append(f"- 未提取到的 SKU（{len(no_match)} 个）：{', '.join(no_match[:8])}")
        for s in wrong[:5]:
            r = fails[s]
            bad = {f: v for f, v in r["fields"].items() if not v["correct"]}
            desc = "; ".join(
                f"{f}: GT={v['gt']} vs 提取={v['extracted_values'][:3]}" for f, v in bad.items()
            )
            lines.append(f"- SKU {s} 数值不符：{desc}")
        lines.append("")
    if not any_fail:
        lines.append("本次运行没有失败案例。\n")

    # 结论
    lines.append("## 结论\n")
    if total_sku:
        overall = total_all_ok / total_sku
        threshold = 0.90
        if mock:
            lines.append(f"- 本次为 Mock 自检：全字段正确率 {overall:.1%}（应接近 100%，用于验证指标计算与管线连通性）。")
        else:
            verdict = "达到可投产水平" if overall >= threshold else "未达到可投产水平"
            lines.append(f"- SKU 全字段正确率 {overall:.1%}，门槛 90%，结论：**{verdict}**。")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def consolidate_report() -> Path | None:
    """从 results/ 中取每个工厂最新一次真实运行结果，汇总生成 report.md。"""
    real_files = sorted(RESULTS_DIR.glob("validation_real_*.json"))
    if not real_files:
        print("[汇总] 尚无真实运行结果")
        return None
    latest: dict[str, dict] = {}
    for path in real_files:  # 文件名带时间戳，按序后者覆盖前者
        for m in json.loads(path.read_text(encoding="utf-8")):
            latest[m["factory"]] = m
    all_metrics = [latest[f] for f in sorted(latest)]
    pseudo_path = RESULTS_DIR / "(多文件汇总)"
    generate_report(all_metrics, mock=False, results_path=pseudo_path)
    return REPORT_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="提取引擎验证")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--factory", help="单个工厂名，如：中地")
    group.add_argument("--all", action="store_true", help="验证全部工厂")
    group.add_argument("--consolidate", action="store_true", help="不跑提取，仅汇总最新结果生成 report.md")
    parser.add_argument("--max-files", type=int, default=None, help="每工厂最多处理的文件数（控制成本）")
    parser.add_argument("--mock", action="store_true", help="不调用 LLM，用假数据走通全流程自检")
    args = parser.parse_args()

    if args.consolidate:
        path = consolidate_report()
        if path:
            print(f"[完成] 汇总报告：{path}")
        return 0

    factories = list_factories() if args.all else [args.factory]

    if args.max_files:
        _install_max_files(args.max_files)

    if not args.mock:
        # 提前检查 API key，给出清晰报错
        try:
            llm_client.get_settings()
        except llm_client.MissingAPIKeyError as e:
            print(f"[错误] {e}", file=sys.stderr)
            return 2

    all_metrics: list[dict] = []
    for factory in factories:
        print(f"[运行] 工厂={factory} mock={args.mock} max_files={args.max_files}")
        try:
            metrics = run_factory(factory, args.mock)
        except Exception as e:  # noqa: BLE001 - 单工厂失败不中断整批
            print(f"  [失败] {type(e).__name__}: {e}")
            metrics = {
                "factory": factory, "sku_total": 0, "sku_covered": 0,
                "sku_coverage": None, "sku_all_fields_correct": 0,
                "field_accuracy": {f: None for f in ("total_quantity", "total_net_weight", "total_gross_weight")},
                "extracted_item_count": 0, "needs_human_review_rate": None,
                "json_parse_success_rate": None, "unsupported_files": [],
                "file_errors": {"<factory>": f"{type(e).__name__}: {e}"},
                "token_usage": {}, "per_sku": {}, "elapsed_sec": 0, "mock": args.mock,
            }
        all_metrics.append(metrics)
        cov = metrics["sku_coverage"]
        ok = (metrics["sku_all_fields_correct"] / metrics["sku_total"]) if metrics["sku_total"] else 0
        print(f"  覆盖率={cov}  全字段正确率={ok:.1%}" if metrics["sku_total"] else "  无 GT 数据")

    results_path = save_results(all_metrics, args.mock)
    generate_report(all_metrics, args.mock, results_path)
    print(f"[完成] 结果：{results_path}")
    print(f"[完成] 报告：{REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
