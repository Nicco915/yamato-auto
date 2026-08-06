# -*- coding: utf-8 -*-
"""factory_match DB 权威源改造单测（pytest）。

DB（factory_aliases / factories）优先，alias_map.json / config 退化为回退：
- DB 优先：真实 master.db（12 条别名）直出 DB 结果；
- 回退：空 DB / DB 异常时行为与旧版（json/config）完全一致；
- save 写 DB：upsert 可查、load_alias_map 包含该条，测试后清理；
- 行为 diff：DB 结果与原 alias_map.json 逐条一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import factory_match
from app.config import get_settings
from app.db.models import Base, Factory, FactoryAlias
from app.db.session import get_session
from app.factory_match import (
    load_alias_map,
    load_excel_normalize_map,
    load_inspection_factories,
    save_alias_entries,
)


def _json_alias_map() -> dict:
    return json.loads(
        get_settings().alias_map_abs.read_text(encoding="utf-8"))


def _bind_empty_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """把 factory_match 的 get_session 绑到临时空 DB（表结构齐、零数据）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(factory_match, "get_session", lambda: sm())


# ---------------------------------------------------------------------------
# 1. DB 优先
# ---------------------------------------------------------------------------

def test_load_alias_map_db_first():
    """真实 master.db 已有 12 条别名，load_alias_map() 直出 DB 结果。"""
    m = load_alias_map()
    assert len(m) == 12
    assert m["山東中地"] == "中地"
    assert m["青島貝来国際貿易有限公司"] == "贝来"
    # short_name 为空的工厂（天津市依依衛生用品）不产生条目
    assert "天津市依依衛生用品" not in m


# ---------------------------------------------------------------------------
# 2. 回退：空 DB / DB 异常 → json，行为与旧版一致
# ---------------------------------------------------------------------------

def test_load_alias_map_fallback_empty_db(monkeypatch, tmp_path):
    """DB 查得到但为空（未迁移）→ 回退 alias_map.json 全量内容。"""
    _bind_empty_db(monkeypatch, tmp_path)
    assert load_alias_map() == _json_alias_map()


def test_load_alias_map_fallback_db_error(monkeypatch):
    """DB 查询抛异常 → 记日志回退 json，绝不抛。"""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(factory_match, "get_session", _boom)
    assert load_alias_map() == _json_alias_map()


def test_load_alias_map_explicit_path_ignores_db(tmp_path):
    """显式传 path 时只读指定 json 文件（旧语义保留）。"""
    p = tmp_path / "alias.json"
    p.write_text(json.dumps({"A社": "A"}, ensure_ascii=False),
                 encoding="utf-8")
    assert load_alias_map(p) == {"A社": "A"}


def test_save_alias_entries_fallback_db_error(monkeypatch, tmp_path):
    """DB 写入异常 → 回退 json 原子写，返回结构不变。"""
    def _boom():
        raise RuntimeError("db down")

    p = tmp_path / "alias.json"
    monkeypatch.setattr(factory_match, "get_session", _boom)
    # path=None 的回退写默认 alias_map.json；指向临时文件避免污染真实数据
    monkeypatch.setattr(
        factory_match, "_alias_path", lambda path=None: Path(path) if path else p)

    res = save_alias_entries({"B社": "B"})
    assert res == {"saved": 1, "overwritten": [], "path": str(p)}
    assert json.loads(p.read_text(encoding="utf-8")) == {"B社": "B"}


def test_load_excel_normalize_map_fallback(monkeypatch, tmp_path):
    _bind_empty_db(monkeypatch, tmp_path)
    assert load_excel_normalize_map() == dict(
        get_settings().FACTORY_NORMALIZE_MAP)


def test_load_inspection_factories_fallback(monkeypatch, tmp_path):
    _bind_empty_db(monkeypatch, tmp_path)
    assert load_inspection_factories() == list(
        get_settings().INSPECTION_FACTORIES)


# ---------------------------------------------------------------------------
# 3. save 写 DB（真实 master.db，测试后清理）
# ---------------------------------------------------------------------------

def test_save_alias_entries_to_db():
    alias, short = "テスト工厂X", "测试短名X"
    try:
        res = save_alias_entries({alias: short})
        assert res["saved"] == 1
        assert res["path"] == "db:factory_aliases"

        with get_session() as sess:
            row = (
                sess.query(FactoryAlias)
                .filter(FactoryAlias.alias == alias)
                .one()
            )
            assert row.use_folder_match is True
            factory = (
                sess.query(Factory)
                .filter(Factory.factory_id == row.factory_id)
                .one()
            )
            assert factory.short_name == short

        assert load_alias_map()[alias] == short

        # 同 alias 改指向既有其他工厂（short_name=中地 → 山東中地）→ 记入 overwritten
        res2 = save_alias_entries({alias: "中地"})
        assert res2["overwritten"] == [alias]
        assert load_alias_map()[alias] == "中地"
    finally:
        with get_session() as sess:
            sess.query(FactoryAlias).filter(
                FactoryAlias.alias == alias).delete()
            sess.query(Factory).filter(
                Factory.factory_name == alias).delete()
            sess.commit()
    assert alias not in load_alias_map()


# ---------------------------------------------------------------------------
# 4. load_excel_normalize_map（DB 优先）
# ---------------------------------------------------------------------------

def test_load_excel_normalize_map_db_first():
    m = load_excel_normalize_map()
    assert m["青島貝来国際貿易有限公司"] == "青島貝来"
    assert (
        m["上海億鑽五金工具有限公司（青島）"] == "上海億鑽五金工具（青島）"
    )


# ---------------------------------------------------------------------------
# 5. load_inspection_factories（DB 优先）
# ---------------------------------------------------------------------------

def test_load_inspection_factories_db_first():
    names = load_inspection_factories()
    assert "青島貝来" in names
    assert "Ｃ．正達工芸品" in names


# ---------------------------------------------------------------------------
# 6. 行为 diff 为空：DB 结果与原 json 逐条一致
# ---------------------------------------------------------------------------

def test_alias_map_db_matches_json():
    assert load_alias_map() == _json_alias_map()
