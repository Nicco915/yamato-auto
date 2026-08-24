# -*- coding: utf-8 -*-
"""新工厂自动学习（A+C 级）后端核心测试（2026-08-24）。

覆盖三条改动：
1. Node2 folder_router：match_method 写入 current_factory_data（下游 C 级回填
   与审核页建议卡都依赖本字段）；
2. Node6 writer._upsert_db：C 级 short_name 自动回填 —— 高置信档
   （override/alias/alias_ci/exact）+ short_name 为空 + folder_path 非空时
   取文件夹名沉淀 short_name，并写 review_audits 留痕；
   fuzzy/contains/none 档不回填（走审核页建议卡人工确认）；
   short_name 已有值绝不覆盖；
3. review_payload.build_review_payload：新增 alias_suggestion 字段
   （override 命中 → kind=override；fuzzy/contains → kind=fuzzy；
   别名已沉淀 → None；short_name 冲突 → conflict=True）。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/alias_autolearn_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from sqlalchemy import select  # noqa: E402

from app.db.models import Factory, FactoryAlias, ReviewAudit  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.nodes.folder_router import folder_router  # noqa: E402
from app.nodes.review_payload import build_review_payload  # noqa: E402
from app.nodes.writer import _upsert_db  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）；
# ALIAS_MAP_PATH 指向临时空表，杜绝真实 alias_map.json 影响匹配分档
_TMP = Path(__import__("tempfile").mkdtemp(prefix="yamato_alias_env_"))
_EMPTY_ALIAS = _TMP / "alias_map.json"
_EMPTY_ALIAS.write_text("{}\n", encoding="utf-8")
TMP = isolate_to_tmp("yamato_alias_autolearn_test_",
                     extra_env={"ALIAS_MAP_PATH": str(_EMPTY_ALIAS)})

_FACTORY_SEQ = itertools.count(1)


def _new_factory_name() -> str:
    """每个用例唯一工厂名：factories.factory_name 全表唯一，复用会被污染。"""
    return f"株式会社テスト{next(_FACTORY_SEQ):03d}"


def _writer_state(factory_name: str, *, match_method: str,
                  folder_path: str | None, batch_id: str = "test-batch") -> dict:
    """构造驱动 _upsert_db 的最小 AgentState（它是纯函数式，只读 current_factory_data）。"""
    return {
        "batch_id": batch_id,
        "current_factory_data": {
            "factory_name": factory_name,
            "folder_path": folder_path,
            "match_method": match_method,
            "match_score": 100.0,
            "calculated_items": [],
        },
    }


def _get_factory(factory_name: str) -> Factory | None:
    with get_session() as s:
        f = s.scalar(select(Factory).where(Factory.factory_name == factory_name))
        if f is not None:
            s.expunge(f)
        return f


def _audits_for(factory_name: str) -> list[ReviewAudit]:
    with get_session() as s:
        rows = s.scalars(
            select(ReviewAudit).where(ReviewAudit.factory_name == factory_name)
        ).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


# ---------------------------------------------------------------------------
# 任务 1：folder_router 把 match_method 写入 state
# ---------------------------------------------------------------------------


def test_folder_router_puts_match_method_in_state():
    """Node2 匹配后 current_factory_data 必须带 match_method（下游两级消费）。"""
    upstream = TMP / "upstream_router"
    (upstream / "中地").mkdir(parents=True)
    (upstream / "中地" / "packing.xlsx").write_bytes(b"x")  # 有单据即可

    state = {
        "pending_factories": ["中地"],
        "deferred_factories": [],
        "upstream_root": str(upstream),
        "downstream_requirements": {},
    }
    update = folder_router(state)
    cur = update["current_factory_data"]
    assert cur["match_method"] == "exact"
    assert cur["folder_path"] == str(upstream / "中地")
    assert cur["match_score"] == 100.0


def test_folder_router_no_match_method_none():
    """未匹配时 match_method 为 none（不是缺键）。"""
    upstream = TMP / "upstream_router_none"
    upstream.mkdir(parents=True, exist_ok=True)  # 空根目录

    state = {
        "pending_factories": ["不存在的工厂xyz"],
        "deferred_factories": [],
        "upstream_root": str(upstream),
        "downstream_requirements": {},
    }
    update = folder_router(state)
    cur = update["current_factory_data"]
    assert cur["match_method"] == "none"
    assert cur["folder_path"] is None


# ---------------------------------------------------------------------------
# 任务 2：writer._upsert_db 的 C 级 short_name 自动回填
# ---------------------------------------------------------------------------


def test_upsert_backfills_short_name_on_exact():
    """exact 档 + short_name 空 + folder_path 非空 → 回填文件夹名 + 审计留痕。"""
    name = _new_factory_name()
    _upsert_db(_writer_state(name, match_method="exact",
                             folder_path="/upstream/中地"))
    f = _get_factory(name)
    assert f is not None
    assert f.short_name == "中地"

    audits = _audits_for(name)
    assert len(audits) == 1
    assert audits[0].result_status == "auto_short_name"
    assert audits[0].approved is True
    assert '"new": "中地"' in audits[0].changes_json


def test_upsert_backfills_on_all_high_confidence_methods():
    """override / alias / alias_ci 同属高置信集合，均可触发回填。"""
    for method in ("override", "alias", "alias_ci"):
        name = _new_factory_name()
        _upsert_db(_writer_state(name, match_method=method,
                                 folder_path=f"/upstream/短名{method}"))
        assert _get_factory(name).short_name == f"短名{method}"


def test_upsert_no_backfill_on_fuzzy():
    """fuzzy 档不回填（走审核页建议卡人工确认）。"""
    name = _new_factory_name()
    _upsert_db(_writer_state(name, match_method="fuzzy",
                             folder_path="/upstream/中地"))
    f = _get_factory(name)
    assert f is not None
    assert f.short_name is None
    assert _audits_for(name) == []


def test_upsert_no_backfill_on_contains_or_none():
    """contains / none 档同样不回填。"""
    for method in ("contains", "none"):
        name = _new_factory_name()
        _upsert_db(_writer_state(name, match_method=method,
                                 folder_path="/upstream/中地"))
        assert _get_factory(name).short_name is None


def test_upsert_no_backfill_when_short_name_exists():
    """short_name 已有值 → 绝不覆盖（即使高置信档）。"""
    name = _new_factory_name()
    with get_session() as s:
        s.add(Factory(factory_name=name, short_name="原短名"))
        s.commit()
    _upsert_db(_writer_state(name, match_method="exact",
                             folder_path="/upstream/中地"))
    assert _get_factory(name).short_name == "原短名"
    assert _audits_for(name) == []


def test_upsert_no_backfill_when_folder_path_empty():
    """folder_path 为空（未匹配文件夹）→ 不回填。"""
    name = _new_factory_name()
    _upsert_db(_writer_state(name, match_method="exact", folder_path=None))
    assert _get_factory(name).short_name is None


def test_upsert_no_backfill_when_match_method_missing():
    """老批次快照没有 match_method 键 → 视为低置信，不回填。"""
    name = _new_factory_name()
    state = _writer_state(name, match_method="exact",
                          folder_path="/upstream/中地")
    del state["current_factory_data"]["match_method"]
    _upsert_db(state)
    assert _get_factory(name).short_name is None


# ---------------------------------------------------------------------------
# 任务 3：build_review_payload 的 alias_suggestion 字段
# ---------------------------------------------------------------------------


def _payload_cur(factory_name: str, *, match_method: str,
                 folder_path: str | None, match_score: float = 72.0) -> dict:
    return {
        "factory_name": factory_name,
        "folder_path": folder_path,
        "match_method": match_method,
        "match_score": match_score,
        "calculated_items": [],
    }


def test_alias_suggestion_override_kind():
    """工厂在批次覆盖 overrides 里 → kind=override，folder=覆盖值。"""
    name = _new_factory_name()
    payload = build_review_payload(
        _payload_cur(name, match_method="override",
                     folder_path="/upstream/中地", match_score=100.0),
        overrides={name: "中地"},
    )
    sug = payload["alias_suggestion"]
    assert sug["factory"] == name
    assert sug["folder"] == "中地"
    assert sug["kind"] == "override"
    assert sug["match_score"] == 100.0
    assert sug["current_short_name"] is None
    assert sug["conflict"] is False


def test_alias_suggestion_fuzzy_kind():
    """fuzzy 档 + folder_path 非空 → kind=fuzzy，folder=文件夹名。"""
    name = _new_factory_name()
    payload = build_review_payload(
        _payload_cur(name, match_method="fuzzy",
                     folder_path="/upstream/依依", match_score=68.5),
    )
    sug = payload["alias_suggestion"]
    assert sug["kind"] == "fuzzy"
    assert sug["folder"] == "依依"
    assert sug["match_score"] == 68.5


def test_alias_suggestion_contains_kind():
    """contains 档同属低置信建议集合 → kind=fuzzy。"""
    name = _new_factory_name()
    payload = build_review_payload(
        _payload_cur(name, match_method="contains",
                     folder_path="/upstream/正达", match_score=70.0),
    )
    assert payload["alias_suggestion"]["kind"] == "fuzzy"
    assert payload["alias_suggestion"]["folder"] == "正达"


def test_alias_suggestion_none_on_high_confidence():
    """exact/alias 等高置信档不出建议卡（Node6 C 级已自动回填）。"""
    name = _new_factory_name()
    payload = build_review_payload(
        _payload_cur(name, match_method="exact",
                     folder_path="/upstream/中地", match_score=100.0),
    )
    assert payload["alias_suggestion"] is None


def test_alias_suggestion_none_when_already_saved():
    """别名已沉淀（factory_aliases 指向 short_name == folder）→ None 不再提示。"""
    name = _new_factory_name()
    with get_session() as s:
        f = Factory(factory_name=name, short_name="中地")
        s.add(f)
        s.flush()
        s.add(FactoryAlias(factory_id=f.factory_id, alias=name,
                           use_folder_match=True))
        s.commit()
    payload = build_review_payload(
        _payload_cur(name, match_method="fuzzy",
                     folder_path="/upstream/中地", match_score=80.0),
    )
    assert payload["alias_suggestion"] is None


def test_alias_suggestion_conflict():
    """current_short_name 非空且 != folder → conflict=True。"""
    name = _new_factory_name()
    with get_session() as s:
        s.add(Factory(factory_name=name, short_name="正达"))
        s.commit()
    payload = build_review_payload(
        _payload_cur(name, match_method="fuzzy",
                     folder_path="/upstream/中地", match_score=75.0),
    )
    sug = payload["alias_suggestion"]
    assert sug["current_short_name"] == "正达"
    assert sug["conflict"] is True


def test_alias_suggestion_no_conflict_when_same():
    """short_name 与 folder 相同但别名未存 → conflict=False 且仍提示。"""
    name = _new_factory_name()
    with get_session() as s:
        s.add(Factory(factory_name=name, short_name="中地"))
        s.commit()
    payload = build_review_payload(
        _payload_cur(name, match_method="fuzzy",
                     folder_path="/upstream/中地", match_score=75.0),
    )
    sug = payload["alias_suggestion"]
    assert sug is not None
    assert sug["current_short_name"] == "中地"
    assert sug["conflict"] is False


def test_alias_suggestion_none_without_folder():
    """fuzzy 档但 folder_path 为空 → None。"""
    name = _new_factory_name()
    payload = build_review_payload(
        _payload_cur(name, match_method="fuzzy", folder_path=None),
    )
    assert payload["alias_suggestion"] is None


def test_alias_suggestion_match_score_defaults_zero():
    """match_score 缺失 → 0。"""
    name = _new_factory_name()
    cur = _payload_cur(name, match_method="fuzzy",
                       folder_path="/upstream/中地")
    del cur["match_score"]
    payload = build_review_payload(cur)
    assert payload["alias_suggestion"]["match_score"] == 0.0
