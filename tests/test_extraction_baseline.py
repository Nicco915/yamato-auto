#!/usr/bin/env python3
"""提取层回归基线快照测试（批次 2 / Step 2.1）。

目的：
  - 达安/兆丰两个 fixture 目录的基线快照文件已存在
  - 达安快照含 1 个文件（DA26107.xls）
  - 兆丰快照含 3 个文件（Packing XD-261830-001-1.26/1.28/1.30）
  - 达安快照的 markdown 字段含 "BARCODE" 关键字（A2a 单射形态条码回填已触发）
  - 兆丰快照的 markdown 总行数 ≥ 50

测试红线：必须 YAMATO_TEST_MODE=1 才允许跑（避免误伤生产数据）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ----- 测试红线守卫 -----
assert os.environ.get("YAMATO_TEST_MODE") == "1", (
    "test_extraction_baseline 仅用于测试，必须设 YAMATO_TEST_MODE=1 后再跑"
)

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

BASELINE_DIR = APP_ROOT / "tests" / "snapshots" / "baseline"


def _load_snapshots(factory_slug: str) -> list[dict]:
    """读取某工厂的所有快照文件。"""
    snap_dir = BASELINE_DIR / factory_slug
    if not snap_dir.exists():
        return []
    snaps: list[dict] = []
    for p in sorted(snap_dir.glob("*.json")):
        snaps.append(json.loads(p.read_text(encoding="utf-8")))
    return snaps


def test_daan_snapshot_exists():
    """达安 fixture 已生成基线快照。"""
    snaps = _load_snapshots("daan")
    assert len(snaps) == 1, f"达安快照应有 1 个文件，实际 {len(snaps)}"
    print("  ✓ test_daan_snapshot_exists")


def test_zhaofeng_snapshots_exist():
    """兆丰 fixture 已生成 3 份基线快照（test-84 兆丰 XD-261830 三份 Packing）。"""
    snaps = _load_snapshots("zhaofeng")
    assert len(snaps) == 3, f"兆丰快照应有 3 个文件，实际 {len(snaps)}"
    # 校验文件名后缀特征（确保 1.26 / 1.28 / 1.30 三份都在）
    names = sorted(s["source_filename"] for s in snaps)
    assert any("1.26" in n for n in names), f"缺 1.26：{names}"
    assert any("1.28" in n for n in names), f"缺 1.28：{names}"
    assert any("1.30" in n for n in names), f"缺 1.30：{names}"
    print("  ✓ test_zhaofeng_snapshots_exist")


def test_daan_snapshot_contains_barcode():
    """达安快照的 markdown 字段含 BARCODE 关键字（A2a 单射形态条码回填触发证据）。"""
    snaps = _load_snapshots("daan")
    assert snaps, "达安快照为空"
    md = snaps[0]["markdown"]
    assert "BARCODE" in md, f"达安 markdown 缺 BARCODE 关键字（A2a 未触发？）前 500 字：{md[:500]}"
    print("  ✓ test_daan_snapshot_contains_barcode")


def test_zhaofeng_markdown_total_lines():
    """兆丰快照的 markdown 总行数 ≥ 50（3 份各 31 行 ≈ 93 行）。"""
    snaps = _load_snapshots("zhaofeng")
    assert snaps, "兆丰快照为空"
    total = sum(s["markdown_lines"] for s in snaps)
    assert total >= 50, f"兆丰 markdown 总行数应 ≥ 50，实际 {total}"
    print(f"  ✓ test_zhaofeng_markdown_total_lines (total={total})")


def test_daan_profile_is_candidate():
    """达安 fixture 是候选箱单（验证 target_identifier 判定不被破坏）。"""
    snaps = _load_snapshots("daan")
    assert snaps, "达安快照为空"
    prof = snaps[0]["profile"]
    assert prof["is_candidate"], f"达安应判定为候选，实际 {prof}"
    assert prof["barcodes"], f"达安 barcodes 应非空，实际 {prof['barcodes']}"
    assert "4936695359672" in prof["barcodes"], f"达安应含 13 位条码 4936695359672，实际 {prof['barcodes']}"
    print("  ✓ test_daan_profile_is_candidate")


def test_zhaofeng_drop_zero_filter_engaged():
    """兆丰 1.28/1.30 至少一份 markdown_after_zero_filter_lines < markdown_lines（全 0 占位行预过滤触发）。"""
    snaps = _load_snapshots("zhaofeng")
    assert snaps, "兆丰快照为空"
    engaged = [s for s in snaps if s["markdown_after_zero_filter_lines"] < s["markdown_lines"]]
    assert engaged, "兆丰至少一份应触发 _drop_zero_rows 预过滤（全 0 占位行）"
    print(f"  ✓ test_zhaofeng_drop_zero_filter_engaged ({len(engaged)}/{len(snaps)})")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_daan_snapshot_exists,
        test_zhaofeng_snapshots_exist,
        test_daan_snapshot_contains_barcode,
        test_zhaofeng_markdown_total_lines,
        test_daan_profile_is_candidate,
        test_zhaofeng_drop_zero_filter_engaged,
    ]

    print(f"\n=== 提取层回归基线快照测试（{len(tests)} 个用例）===\n")

    failed: list[tuple[str, BaseException]] = []
    for t in tests:
        try:
            t()
        except Exception as e:
            failed.append((t.__name__, e))
            print(f"  ✗ {t.__name__}: {e}")

    print()
    if failed:
        print(f"失败 {len(failed)}/{len(tests)}")
        for name, e in failed:
            print(f"  - {name}: {e}")
        return 1
    print(f"全部通过 {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())