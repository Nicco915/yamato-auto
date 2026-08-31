# -*- coding: utf-8 -*-
"""create_batch 预览轮2：alias_decisions 合并进工厂对照预扫测试。

回归背景：轮2 用户已给出 alias_decisions 时，预览的 W5 预扫三档
（resolved/candidates/unmatched）原先独立重算、不知道本轮决定，导致：
- 被决定的工厂仍出现在 candidates/unmatched（确认卡照常显示 [存疑]）；
- 摘要照常写「工厂对照存疑 N 家」；
修复后：决定先硬校验，通过的工厂从 candidates/unmatched 移除并注入
resolved（method=本次决定/永久对照），摘要与三档 lines 读过滤后的结果。

覆盖：
1. 全部存疑被决定（candidates + unmatched 各一家，save=false）：
   scan 三档已过滤、resolved 注入 method=本次决定、摘要不含「存疑」、
   lines 保留「本次工厂对照决定」清单（[仅本次]）；
2. save=true 时 method=永久对照、lines 含 [永久保存]；
3. 部分决定：未决定工厂仍在 unmatched、摘要仍报存疑家数；
4. 校验失败（坏 folder）：blocked=True + 摘要「工厂对照校验未通过」。

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 全部
指向临时目录（见 validation/_test_isolation.py）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 python3 validation/create_batch_preview_decision_test.py

说明：本测试只直调 _preview_create_batch（确认门预览的共享实现），
不经调度引擎。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openpyxl import Workbook  # noqa: E402

from app.dispatcher.tools import _preview_create_batch  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 夹具目录（隔离前先建好，ALIAS_MAP_PATH 随隔离指向它）----
FIX = Path(tempfile.mkdtemp(prefix="yamato_cbpd_fixture_"))
ALIAS_PATH = FIX / "alias_map.json"
ALIAS_PATH.write_text("{}\n", encoding="utf-8")

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_cbpd_test_",
                     extra_env={"ALIAS_MAP_PATH": str(ALIAS_PATH)})

# ---- 共享夹具：装箱单三工厂 + 上游目录 ----
# 工厂A：exact 命中；依依衛生用品：低置信候选（包含信号→候选依依）；
# ZZZ幽灵厂：无候选
XLSX = FIX / "downstream.xlsx"
_wb = Workbook()
_ws = _wb.active
_ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
for _factory, _sku in [("工厂A", "4900000000001"),
                       ("依依衛生用品", "4900000000002"),
                       ("ZZZ幽灵厂", "4900000000003")]:
    _ws.append([_factory, _sku, "ITEM", 10])
_wb.save(XLSX)

UPSTREAM = FIX / "upstream"
(UPSTREAM / "工厂A").mkdir(parents=True)
(UPSTREAM / "依依").mkdir(parents=True)
(UPSTREAM / "备用甲").mkdir(parents=True)
(UPSTREAM / "备用乙").mkdir(parents=True)


def _preview(decisions: list[dict], tag: str) -> dict:
    return _preview_create_batch({
        "thread_id": f"CBPD-{tag}",
        "downstream_file_path": str(XLSX),
        "upstream_root": str(UPSTREAM),
        "alias_decisions": decisions,
    })


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_all_doubtful_decided() -> None:
    """全部存疑被决定：三档过滤 + resolved 注入 + 摘要无「存疑」+ 清单保留。"""
    result = _preview([
        {"factory": "依依衛生用品", "folder": "备用甲", "save": False},
        {"factory": "ZZZ幽灵厂", "folder": "备用乙", "save": False},
    ], "ALL")
    assert "error" not in result, result
    assert not result.get("blocked"), result

    scan = result["factory_scan"]
    assert "依依衛生用品" not in (scan.get("candidates") or {}), scan
    assert "ZZZ幽灵厂" not in (scan.get("unmatched") or []), scan
    resolved = scan.get("resolved") or {}
    for factory, folder in (("依依衛生用品", "备用甲"),
                            ("ZZZ幽灵厂", "备用乙")):
        hit = resolved.get(factory)
        assert hit is not None, f"{factory} 未注入 resolved: {scan}"
        assert hit["folder"] == folder, hit
        assert hit["score"] == 100.0, hit
        assert hit["method"] == "本次决定", hit
    # 原本确定命中的工厂不受影响
    assert resolved.get("工厂A", {}).get("folder") == "工厂A", scan
    print("  ✓ 被决定工厂已从 candidates/unmatched 移除并注入 resolved"
          "（method=本次决定）")

    assert "存疑" not in result["summary"], result["summary"]
    print(f"  ✓ 摘要不再报存疑: {result['summary']}")

    text = "\n".join(result["lines"])
    assert "本次工厂对照决定:" in text, text
    assert "[仅本次] 依依衛生用品 -> 备用甲" in text, text
    assert "[仅本次] ZZZ幽灵厂 -> 备用乙" in text, text
    print("  ✓ 预览 lines 保留「本次工厂对照决定」清单（[仅本次]）")


def test_save_true_method_permanent() -> None:
    """save=true：resolved method=永久对照，lines 含 [永久保存]。"""
    result = _preview([
        {"factory": "ZZZ幽灵厂", "folder": "备用乙", "save": True},
    ], "SAVE")
    assert "error" not in result and not result.get("blocked"), result

    scan = result["factory_scan"]
    hit = (scan.get("resolved") or {}).get("ZZZ幽灵厂")
    assert hit is not None and hit["method"] == "永久对照", scan
    assert hit["folder"] == "备用乙" and hit["score"] == 100.0, hit
    assert "ZZZ幽灵厂" not in (scan.get("unmatched") or []), scan
    print("  ✓ save=true：resolved method=永久对照")

    text = "\n".join(result["lines"])
    assert "[永久保存] ZZZ幽灵厂 -> 备用乙" in text, text
    # 未决定的 依依衛生用品 仍在 candidates，摘要仍报存疑
    assert "依依衛生用品" in (scan.get("candidates") or {}), scan
    assert "存疑" in result["summary"], result["summary"]
    print("  ✓ 未决定工厂仍在 candidates，摘要照常报存疑")


def test_partial_decision_keeps_unmatched() -> None:
    """部分决定：只决定一家无候选工厂，另一家仍在 unmatched + 摘要报存疑。"""
    result = _preview([
        {"factory": "ZZZ幽灵厂", "folder": "备用乙", "save": False},
    ], "PARTIAL")
    assert "error" not in result and not result.get("blocked"), result

    scan = result["factory_scan"]
    assert "ZZZ幽灵厂" not in (scan.get("unmatched") or []), scan
    assert "依依衛生用品" in (scan.get("candidates") or {}), scan
    assert (scan.get("resolved") or {}).get("ZZZ幽灵厂", {}) \
        .get("method") == "本次决定", scan
    assert "存疑" in result["summary"], result["summary"]
    assert "工厂对照存疑 1 家" in result["summary"], result["summary"]
    print(f"  ✓ 部分决定：摘要仍报存疑 1 家: {result['summary']}")


def test_invalid_decision_blocked() -> None:
    """校验失败（folder 不存在）：blocked=True，摘要「工厂对照校验未通过」。"""
    result = _preview([
        {"factory": "ZZZ幽灵厂", "folder": "不存在的目录", "save": False},
    ], "BAD")
    assert result.get("blocked") is True, result
    assert result["summary"] == "工厂对照校验未通过", result["summary"]
    assert any("拒绝写入对照" in w for w in result["warnings"]), result
    print("  ✓ 坏 folder：blocked=True + 校验未通过摘要 + warnings 带原因")


CASES = [
    ("1. 全部存疑被决定（三档过滤/摘要/清单）", test_all_doubtful_decided),
    ("2. save=true method=永久对照", test_save_true_method_permanent),
    ("3. 部分决定保留未决存疑", test_partial_decision_keeps_unmatched),
    ("4. 校验失败 blocked", test_invalid_decision_blocked),
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
        print("🎉 create_batch 预览轮2 决定合并全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
