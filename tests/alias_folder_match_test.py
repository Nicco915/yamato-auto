# -*- coding: utf-8 -*-
"""alias_folder 档（别名/短名即文件夹名）测试。

场景：文件夹以别名命名（如文件夹「YIYI」、工厂 short_name「依依」），
既有档位（alias→short_name→文件夹）打不通；alias_folder 档用主数据里
该工厂的 short_name 与勾选「文件夹匹配」的别名本身做规范化精确匹配。

覆盖：
- match_factory_folder 纯函数：
  - factory_name 查找，别名命中文件夹（exact 失败、fuzzy 之前命中）
  - short_name 本身即文件夹名（抢在 contains 70 分之前以 100 分命中）
  - 装箱单名字本身是别名，别名自身即文件夹（alias 档落空后救回）
  - 优先级：误导性 fuzzy 候选存在时 alias_folder 仍胜出
  - 无 folder_candidates 时行为与旧版完全一致（回退 fuzzy/contains）
- load_folder_match_candidates DB 侧：
  - factory_name / 别名 key 均映射到完整候选列表（short_name + 别名）
  - use_folder_match=False 的别名不参与
  - short_name 为空的工厂仅有别名候选

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/alias_folder_match_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from app.db.models import Factory, FactoryAlias  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.factory_match import (  # noqa: E402
    load_folder_match_candidates, match_factory_folder)

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_alias_folder_test_")


# ---------- match_factory_folder 纯函数 ----------

def test_alias_folder_hit_by_factory_name():
    """工厂名 exact 失败，别名「YIYI」即文件夹名 → alias_folder 命中。"""
    folders = ["YIYI", "中地"]
    candidates = {"天津依依衛生用品": ["依依", "YIYI"]}
    folder, score, method = match_factory_folder(
        "天津依依衛生用品", folders, {}, cutoff=80.0,
        folder_candidates=candidates)
    assert (folder, score, method) == ("YIYI", 100.0, "alias_folder")


def test_alias_folder_short_name_as_folder():
    """short_name「依依」本身即文件夹名：抢在 contains（70 分）之前 100 分命中。"""
    folders = ["依依"]
    candidates = {"天津依依衛生用品": ["依依"]}
    folder, score, method = match_factory_folder(
        "天津依依衛生用品", folders, {}, cutoff=80.0,
        folder_candidates=candidates)
    assert (folder, score, method) == ("依依", 100.0, "alias_folder")


def test_alias_folder_rescues_when_short_name_not_a_folder():
    """装箱单名字=别名：alias 档解析出 short_name 但不是文件夹，
    别名自身才是文件夹 → alias_folder 救回。"""
    folders = ["YIYI大阪"]
    alias_map = {"依依貿易": "依依"}          # 既有档：别名→short_name
    candidates = {"依依貿易": ["依依", "YIYI大阪"]}
    folder, score, method = match_factory_folder(
        "依依貿易", folders, alias_map, cutoff=80.0,
        folder_candidates=candidates)
    assert (folder, score, method) == ("YIYI大阪", 100.0, "alias_folder")


def test_alias_folder_beats_misleading_fuzzy():
    """存在高相似误导文件夹时，确定性 alias_folder 优先于 fuzzy。"""
    folders = ["天津依依衛生", "YIYI"]   # fuzzy 会命中前者（名字几乎相同）
    candidates = {"天津依依衛生用品": ["依依", "YIYI"]}
    folder, score, method = match_factory_folder(
        "天津依依衛生用品", folders, {}, cutoff=60.0,
        folder_candidates=candidates)
    assert (folder, score, method) == ("YIYI", 100.0, "alias_folder")


def test_alias_folder_normalized_hit():
    """别名与文件夹仅差全半角/空白/大小写 → 规范化后命中。"""
    folders = ["ＹＩＹＩ "]  # 全角 + 尾空格
    candidates = {"天津依依衛生用品": ["依依", "yiyi"]}
    folder, score, method = match_factory_folder(
        "天津依依衛生用品", folders, {}, cutoff=80.0,
        folder_candidates=candidates)
    assert (folder, score, method) == ("ＹＩＹＩ ", 100.0, "alias_folder")


def test_no_candidates_backward_compatible():
    """不传 folder_candidates：行为与旧版一致（exact 失败 → contains 兜底）。"""
    folders = ["依依"]
    folder, score, method = match_factory_folder(
        "天津依依衛生用品", folders, {}, cutoff=80.0)
    assert (folder, score, method) == ("依依", 70.0, "contains")


# ---------- load_folder_match_candidates DB 侧 ----------

def _seed_factory(factory_name, short_name, aliases):
    """aliases: [(alias, use_folder_match), ...]"""
    with get_session() as sess:
        f = Factory(factory_name=factory_name, short_name=short_name)
        sess.add(f)
        sess.flush()
        for alias, use_fm in aliases:
            sess.add(FactoryAlias(
                factory_id=f.factory_id, alias=alias, use_folder_match=use_fm))
        sess.commit()


def test_load_candidates_factory_and_alias_keys():
    _seed_factory("华旭阳", "华旭阳", [
        ("华旭阳工贸", True), ("HUAXUYANG", True)])
    cands = load_folder_match_candidates()
    assert cands["华旭阳"] == ["华旭阳", "华旭阳工贸", "HUAXUYANG"]
    assert cands["华旭阳工贸"] == ["华旭阳", "华旭阳工贸", "HUAXUYANG"]
    assert cands["HUAXUYANG"] == ["华旭阳", "华旭阳工贸", "HUAXUYANG"]


def test_load_candidates_excludes_non_folder_match_alias():
    _seed_factory("中地", "中地", [
        ("山東中地", True), ("中地报表变体", False)])
    cands = load_folder_match_candidates()
    assert cands["中地"] == ["中地", "山東中地"]
    # 未勾选文件夹匹配的别名不作为 key，也不进候选
    assert "中地报表变体" not in cands


def test_load_candidates_empty_short_name():
    _seed_factory("无短名工厂", None, [("ONLY_ALIAS", True)])
    cands = load_folder_match_candidates()
    assert cands["无短名工厂"] == ["ONLY_ALIAS"]
    assert cands["ONLY_ALIAS"] == ["ONLY_ALIAS"]
