# -*- coding: utf-8 -*-
"""Ground truth 构建与缓存。

选定来源：/Users/nz/Downloads/yamato/96/报关匹配.xlsx 的「バンニングリスト」sheet
（人工报关产出的バンニングリスト，812 行覆盖全部 10 个工厂，净重/毛重/件数列 100% 非空；
已与工厂原始装箱单抽样核对一致，例如中地 SKU 4549509515197：
件数 500 / 净重 3100 / 毛重 3600 与 XD-269760PackingList.xlsx 原文完全一致）。

四个「报关匹配*.xlsx」（东京/名古屋/神户横滨）的バンニングリスト sheet 内容完全相同，
故只用 报关匹配.xlsx 一份即可。

聚合口径：按（工厂文件夹名, SHOHIN_CD）分组，对件数(SOTOBAKO_D_HACCHU_SU)、
净重、毛重求和 —— 即"每个工厂每个 SKU 的总件数/总净重/总毛重"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

GT_SOURCE = "/Users/nz/Downloads/yamato/96/报关匹配.xlsx"
GT_SHEET = "バンニングリスト"
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
