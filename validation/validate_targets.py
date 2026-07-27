# -*- coding: utf-8 -*-
"""目标识别器验证：10 个工厂的识别结果 vs 人工核对的标准答案。

用法：python3 validate_targets.py
标准答案来源：2026-07-27 人工逐个核对（见 PROGRESS.md 第 5 节）。
全程不调 LLM（识别器为确定性规则），可重复运行。
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "app"))

from extraction.target_identifier import identify_targets  # noqa: E402

BASE = "/Users/nz/Downloads/yamato/96/工厂"

# 人工核对确定的各工厂正确提取目标（只比对文件名，忽略子目录前缀）
EXPECTED: dict[str, list[str]] = {
    "贝来": ["清关单据 CCK2619001-11.xls", "清关单据 CCK2619002-4.xls"],
    "达安": ["DA26461  箱单.xls"],
    "东基恒": ["请款资料/PACKING.pdf"],
    "中地": ["XD-269760PackingList.xlsx"],
    "兆丰": ["总 清款资料/Packing XD-269765-001-7.13.xlsx"],
    "华旭阳": ["XD269764-001 pl&ci.xls"],
    "益尚": ["清关资料MX2-265537-002.pdf", "清关资料XD-269758-001.pdf"],
    "TOP": ["XD-269766-----请款资料.xls"],
    "正达": ["XD INV PL  请款用.xls"],
    "亿钻": ["装箱单通用RWS261233@.doc", "装箱单通用RWS261232@.pdf"],
}


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).replace(" ", " ").split())


def main() -> int:
    pass_n = 0
    for factory, expected in EXPECTED.items():
        got = identify_targets(f"{BASE}/{factory}")
        got_names = sorted(_norm(p.path.split("/")[-1]) for p in got)
        exp_names = sorted(_norm(e.split("/")[-1]) for e in expected)
        ok = got_names == exp_names
        pass_n += ok
        print(f"{'✓' if ok else '✗'} {factory}")
        if not ok:
            print("   期望:", exp_names)
            print("   实际:", got_names)
    print(f"\n{pass_n}/{len(EXPECTED)} 通过")
    return 0 if pass_n == len(EXPECTED) else 1


if __name__ == "__main__":
    sys.exit(main())
