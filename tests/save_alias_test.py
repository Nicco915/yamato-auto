# -*- coding: utf-8 -*-
"""POST /api/v1/review/{thread_id}/save-alias 端点测试（新工厂自动学习 A 级确认）。

覆盖：
- 正常路径（factory_alias_overrides 派生 folder）→ 200 + factory_aliases 新行
  + factory.short_name 回填 + review_audits 留痕
- current_factory_data fuzzy/contains 命中派生 folder → 200
- 无可保存对照建议（含 match_method=exact 不算建议）→ 400
- short_name 冲突 → 409
- folder 不是 upstream_root 一级子目录 → 400
- 批次不存在 → 404
- 重复调用 → 幂等 ok（upsert，不产生 overwritten，别名行不重复）

state 造假方式：monkeypatch app.review.router._get_batch_state_values
返回构造好的 dict。直接往 checkpoint DB 写 graph state 需要完整跑图到
Node5 挂起（LLM 链路），代价高且与本端点逻辑无关；端点唯一依赖 state 的
入口就是该函数（生产实现复用 service.get_order_state），monkeypatch 它
等价于隔离了 LangGraph 层，单测聚焦本端点的派生/校验/落库逻辑。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/save_alias_test.py -v

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

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.api.main import app  # noqa: E402
from app.db.models import Factory, FactoryAlias, ReviewAudit  # noqa: E402
from app.db.session import get_session  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_save_alias_test_")

client = TestClient(app)

# 假 upstream_root：state 里显式给出（优先于 settings），含两个一级子目录
UPSTREAM = TMP / "upstream"
(UPSTREAM / "中地").mkdir(parents=True)
(UPSTREAM / "正达").mkdir(parents=True)

TID = "TEST-SAVE-ALIAS"


def _mkfolder(name: str) -> str:
    """在假 upstream_root 下建一个一级子目录（每个用例用独立文件夹名：
    short_name 唯一性守卫会让跨用例复用同一文件夹互相 409）。"""
    (UPSTREAM / name).mkdir(parents=True, exist_ok=True)
    return name


def _patch_state(monkeypatch, values):
    """把 checkpoint state.values 替换为构造好的 dict（None 表示批次不存在）。"""
    monkeypatch.setattr(
        "app.review.router._get_batch_state_values", lambda tid: values)


def _post(factory: str, thread_id: str = TID):
    return client.post(f"/api/v1/review/{thread_id}/save-alias",
                       json={"factory": factory})


def _get_factory(factory_name: str) -> Factory | None:
    with get_session() as s:
        return s.scalar(select(Factory).where(Factory.factory_name == factory_name))


def _alias_rows(alias: str) -> list[FactoryAlias]:
    with get_session() as s:
        return s.scalars(select(FactoryAlias).where(FactoryAlias.alias == alias)).all()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_save_alias_from_overrides_happy_path(monkeypatch):
    """state.factory_alias_overrides 有临时对照 → 落永久别名 + short_name 回填。"""
    folder = _mkfolder("中地主路径")
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"山東中地": folder},
    })
    r = _post("山東中地")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True,
        "alias": "山東中地",
        "short_name": "中地主路径",
        "overwritten": [],
    }
    # factory 行被创建且 short_name 已填文件夹名
    fac = _get_factory("山東中地")
    assert fac is not None
    assert fac.short_name == "中地主路径"
    # factory_aliases 有新行，挂在该工厂上
    rows = _alias_rows("山東中地")
    assert len(rows) == 1
    assert rows[0].factory_id == fac.factory_id
    assert rows[0].use_folder_match is True
    # 审计留痕
    with get_session() as s:
        audits = s.scalars(select(ReviewAudit).where(
            ReviewAudit.thread_id == TID,
            ReviewAudit.result_status == "save_alias",
        )).all()
    assert len(audits) == 1
    assert audits[0].factory_name == "山東中地"


def test_save_alias_from_current_factory_fuzzy(monkeypatch):
    """无 overrides 时，当前工厂 fuzzy 命中的 folder_path basename 可作建议。"""
    folder = _mkfolder("正达模糊")
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "current_factory_data": {
            "factory_name": "天津依依衛生用品",
            "match_method": "fuzzy",
            "folder_path": str(UPSTREAM / folder),
        },
    })
    r = _post("天津依依衛生用品")
    assert r.status_code == 200, r.text
    assert r.json()["short_name"] == folder
    assert _get_factory("天津依依衛生用品").short_name == folder


def test_save_alias_fills_empty_short_name(monkeypatch):
    """factory 已存在但 short_name 为空 → 回填文件夹名，不冲突。"""
    folder = _mkfolder("贝来")
    with get_session() as s:
        s.add(Factory(factory_name="青岛贝来", short_name=None))
        s.commit()
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"青岛贝来": folder},
    })
    r = _post("青岛贝来")
    assert r.status_code == 200, r.text
    assert _get_factory("青岛贝来").short_name == folder


# ---------------------------------------------------------------------------
# 400 / 404 / 409
# ---------------------------------------------------------------------------


def test_no_suggestion_400(monkeypatch):
    """overrides 与 current_factory_data 都没有可保存对照 → 400。"""
    _patch_state(monkeypatch, {"upstream_root": str(UPSTREAM)})
    r = _post("无建议工厂")
    assert r.status_code == 400, r.text
    assert "没有可保存的对照建议" in r.json()["detail"]


def test_exact_match_is_not_a_suggestion_400(monkeypatch):
    """当前工厂是 exact 命中（确定性匹配），不算需要确认的建议 → 400。"""
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "current_factory_data": {
            "factory_name": "精确命中工厂",
            "match_method": "exact",
            "folder_path": str(UPSTREAM / _mkfolder("精确命中目录")),
        },
    })
    r = _post("精确命中工厂")
    assert r.status_code == 400, r.text
    assert "没有可保存的对照建议" in r.json()["detail"]


def test_folder_not_subdir_400(monkeypatch):
    """对照指向的 folder 不是 upstream_root 下现存一级子目录 → 400。"""
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"坏对照工厂": "不存在的目录"},
    })
    r = _post("坏对照工厂")
    assert r.status_code == 400, r.text
    assert "一级子目录" in r.json()["detail"]


def test_folder_path_traversal_400(monkeypatch):
    """对照值含路径穿越片段 → 400（validate_subfolder 防注入）。"""
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"穿越工厂": ".."},
    })
    r = _post("穿越工厂")
    assert r.status_code == 400, r.text


def test_batch_not_found_404(monkeypatch):
    """批次无 checkpoint state → 404。"""
    _patch_state(monkeypatch, None)
    r = _post("山東中地", thread_id="NO-SUCH-BATCH")
    assert r.status_code == 404, r.text
    assert "批次不存在" in r.json()["detail"]


def test_short_name_conflict_409(monkeypatch):
    """工厂已有不一致的中文短名 → 409，提示去主数据维护页。"""
    with get_session() as s:
        s.add(Factory(factory_name="山東中地冲突", short_name="旧短名"))
        s.commit()
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"山東中地冲突": _mkfolder("冲突目录")},
    })
    r = _post("山東中地冲突")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "旧短名" in detail and "冲突目录" in detail
    assert "主数据维护页" in detail
    # 冲突不落别名
    assert _alias_rows("山東中地冲突") == []


def test_folder_owned_by_other_factory_409(monkeypatch):
    """文件夹已是其他工厂的 short_name → 409（防止别名错挂到别的工厂）。"""
    folder = _mkfolder("被占用目录")
    with get_session() as s:
        s.add(Factory(factory_name="占用者工厂", short_name=folder))
        s.commit()
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"被占用工厂": folder},
    })
    r = _post("被占用工厂")
    assert r.status_code == 409, r.text
    assert "占用者工厂" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_repeat_call_idempotent(monkeypatch):
    """重复保存同一对照 → 幂等 200，overwritten 为空，别名行不重复。"""
    folder = _mkfolder("幂等目录")
    _patch_state(monkeypatch, {
        "upstream_root": str(UPSTREAM),
        "factory_alias_overrides": {"幂等工厂": folder},
    })
    r1 = _post("幂等工厂")
    assert r1.status_code == 200, r1.text
    r2 = _post("幂等工厂")
    assert r2.status_code == 200, r2.text
    assert r2.json() == {
        "ok": True,
        "alias": "幂等工厂",
        "short_name": folder,
        "overwritten": [],
    }
    assert len(_alias_rows("幂等工厂")) == 1
