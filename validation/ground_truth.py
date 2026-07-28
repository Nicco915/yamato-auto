# -*- coding: utf-8 -*-
"""Ground truth 构建与缓存。

选定来源：/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx
的「バンニングリスト」sheet（下游买家装箱表，812 行 × 60 列，净重/毛重/件数列 100% 非空）。
2026-07-27 用户明确：GT 以此文件（ContentsOfTheContainer）为准；
已与 报关匹配.xlsx 交叉核对，两文件该 sheet 数值一致（如贝来 10 SKU 全等）。

聚合口径：按（工厂文件夹名, SHOHIN_CD）分组，对件数(SOTOBAKO_D_HACCHU_SU)、
净重、毛重求和 —— 即"每个工厂每个 SKU 的总件数/总净重/总毛重"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

GT_SOURCE = "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx"
GT_SHEET = "バンニングリスト"
# 兜底源：报关匹配.xlsx。ContentsOfTheContainer 是**待填**的下游表，部分行净重/毛重/件数
# 为空（TOP KOPH 21 行真空，正是系统要填的行）；报关匹配.xlsx 是人工填好的同构表，
# 行序一致（同 index 同 SHOHIN_CD），用于填补这些空单元格（2026-07-27 agent 端到端
# 测试发现 TOP GT 全零/残缺即此原因）。
GT_FALLBACK = "/Users/nz/Downloads/yamato/96/报关匹配.xlsx"
CACHE_PATH = Path(__file__).parent / "results" / "ground_truth_cache.json"

# 工厂文件夹名 → バンニングリスト中的 MAKER_MEI_KJ（日文工厂抬头）
FACTORY_MAKER_MAP: dict[str, list[str]] = {
    "中地": ["山東中地"],
    "正达": ["Ｃ．正達工芸品"],
    "达安": ["青島達安"],
    "兆丰": ["青島兆豊家居"],
    "东基恒": ["東基恒"],
    "益尚": ["益尚国際貿易（山東）有限会社"],
    "贝来": ["青島貝来", "青島貝来国際貿易有限公司"],
    "亿钻": ["上海億鑽五金工具（青島）", "上海億鑽五金工具有限公司（青島）"],
    "华旭阳": ["青島華旭陽"],
    "TOP": ["TOP KOPH（青島）"],
}

FACTORY_FOLDER = "/Users/nz/Downloads/yamato/96/工厂"


def build_ground_truth(use_cache: bool = True) -> dict:
    """构建全部工厂的 ground truth。

    返回结构：
    {
      "中地": {
        "4549509515197": {
          "total_quantity": 500.0, "total_net_weight": 3100.0,
          "total_gross_weight": 3600.0,
          "names": ["FUTON SET 三件套 ...", ...]   # 出现过的品名（日文/英文）
        }, ...
      }, ...
    }
    """
    if use_cache and CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    df = pd.read_excel(GT_SOURCE, sheet_name=GT_SHEET)
    # 行级填补：ContentsOfTheContainer 的空单元格用报关匹配同位置值补齐
    fb = pd.read_excel(GT_FALLBACK, sheet_name=GT_SHEET)
    for col in ("净重", "毛重", "SOTOBAKO_D_HACCHU_SU"):
        mask = df[col].isna() & fb[col].notna() & (df["SHOHIN_CD"] == fb["SHOHIN_CD"])
        df.loc[mask, col] = fb.loc[mask, col]
    gt: dict[str, dict] = {}
    for folder, makers in FACTORY_MAKER_MAP.items():
        sub = df[df["MAKER_MEI_KJ"].isin(makers)]
        factory_gt: dict[str, dict] = {}
        for sku, grp in sub.groupby("SHOHIN_CD"):
            names = sorted(
                {
                    str(x)
                    for col in ("SHOHIN_MEI_KJ", "SHOHIN_MEI_E", "中文品名")
                    for x in grp[col].dropna().unique()
                }
            )
            factory_gt[str(sku)] = {
                "total_quantity": float(grp["SOTOBAKO_D_HACCHU_SU"].sum()),
                "total_net_weight": float(grp["净重"].sum()),
                "total_gross_weight": float(grp["毛重"].sum()),
                "names": names,
            }
        gt[folder] = factory_gt

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(gt, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return gt


def get_factory_ground_truth(factory: str) -> dict:
    """取单个工厂的 ground truth（sku_code -> 指标 dict）。"""
    gt = build_ground_truth()
    if factory not in gt:
        raise KeyError(
            f"工厂 {factory!r} 不在 ground truth 映射中，可选：{sorted(gt)}"
        )
    return gt[factory]


def list_factories() -> list[str]:
    """所有有 ground truth 且本地有文件夹的工厂。"""
    root = Path(FACTORY_FOLDER)
    return sorted(f for f in FACTORY_MAKER_MAP if (root / f).is_dir())


if __name__ == "__main__":
    gt = build_ground_truth(use_cache=False)
    for f, skus in gt.items():
        print(f"{f}: {len(skus)} SKUs")
