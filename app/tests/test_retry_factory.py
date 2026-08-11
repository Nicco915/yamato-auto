# -*- coding: utf-8 -*-
"""retry_factory_extraction 单厂重试逻辑测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import service
from app.factory_match import load_alias_map, match_factory_folder
from app.graph import NODE5


class TestRetryFactoryExtraction:
    """retry_factory_extraction 对 folder_path 为空工厂的自动重匹配。"""

    def _make_mock_graph(self, values, captured):
        snap = SimpleNamespace(values=values, next=(NODE5,))

        class MockGraph:
            def get_state(self, cfg):
                return snap

            def update_state(self, cfg, update, as_node=None):
                captured["update"] = update
                captured["as_node"] = as_node

            def stream(self, *args, **kwargs):
                return iter([])

        return MockGraph()

    def test_auto_rematches_when_folder_path_is_none(
        self, monkeypatch, tmp_path
    ):
        """folder_path=None 时，retry 会枚举上游目录并调用 match_factory_folder，
        匹配成功后把 folder_path 与 factory_alias_overrides 写入 update。
        """
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        factory_dir = upstream / "山東中地"
        factory_dir.mkdir()

        values = {
            "current_factory_data": {
                "factory_name": "山東中地",
                "folder_path": None,
            },
            "upstream_root": str(upstream),
            "factory_alias_overrides": {},
        }
        captured: dict = {}
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values, captured)
        )
        monkeypatch.setattr(service, "_write_batch_state", lambda *a, **kw: None)

        class FakeSettings:
            upstream_root = str(upstream)
            fuzzy_match_score_cutoff = 60.0

        monkeypatch.setattr(service, "get_settings", lambda: FakeSettings())
        monkeypatch.setattr(load_alias_map, "__call__", lambda: {})

        # spy match_factory_folder 调用
        original = match_factory_folder
        calls = []

        def spy(factory, folders, alias_map, cutoff, overrides=None):
            calls.append((factory, list(folders)))
            return original(factory, folders, alias_map, cutoff, overrides)

        monkeypatch.setattr(
            "app.factory_match.match_factory_folder", spy
        )

        result = service.retry_factory_extraction("t-1")

        assert result["status"] == "completed"
        assert len(calls) == 1
        assert calls[0][0] == "山東中地"
        assert "山東中地" in calls[0][1]

        update = captured["update"]
        assert update["force_reextract"] is True
        assert captured["as_node"] == "node2_folder_router"
        assert update["current_factory_data"]["folder_path"] == str(factory_dir)
        assert update["factory_alias_overrides"]["山東中地"] == "山東中地"

    def test_explicit_folder_takes_precedence_over_auto_match(
        self, monkeypatch, tmp_path
    ):
        """显式传入 folder 参数时，不再走自动重匹配分支。"""
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        explicit_dir = upstream / "ExplicitFolder"
        explicit_dir.mkdir()

        values = {
            "current_factory_data": {
                "factory_name": "山東中地",
                "folder_path": None,
            },
            "upstream_root": str(upstream),
            "factory_alias_overrides": {},
        }
        captured: dict = {}
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values, captured)
        )
        monkeypatch.setattr(service, "_write_batch_state", lambda *a, **kw: None)

        class FakeSettings:
            upstream_root = str(upstream)
            fuzzy_match_score_cutoff = 60.0

        monkeypatch.setattr(service, "get_settings", lambda: FakeSettings())

        calls = []

        def spy(factory, folders, alias_map, cutoff, overrides=None):
            calls.append(factory)
            return None, 0.0, "none"

        monkeypatch.setattr("app.factory_match.match_factory_folder", spy)

        result = service.retry_factory_extraction(
            "t-1", folder="ExplicitFolder"
        )

        assert result["status"] == "completed"
        assert calls == []  # 未触发自动匹配
        update = captured["update"]
        assert update["current_factory_data"]["folder_path"] == str(explicit_dir)
        assert update["factory_alias_overrides"]["山東中地"] == "ExplicitFolder"

    def test_still_no_folder_matched_when_auto_match_fails(
        self, monkeypatch, tmp_path
    ):
        """自动重匹配仍失败时，不写入 folder_path，保持原行为。"""
        upstream = tmp_path / "upstream"
        upstream.mkdir()

        values = {
            "current_factory_data": {
                "factory_name": "山東中地",
                "folder_path": None,
            },
            "upstream_root": str(upstream),
            "factory_alias_overrides": {},
        }
        captured: dict = {}
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values, captured)
        )
        monkeypatch.setattr(service, "_write_batch_state", lambda *a, **kw: None)

        class FakeSettings:
            upstream_root = str(upstream)
            fuzzy_match_score_cutoff = 60.0

        monkeypatch.setattr(service, "get_settings", lambda: FakeSettings())
        monkeypatch.setattr("app.factory_match.load_alias_map", lambda: {})
        monkeypatch.setattr(
            "app.factory_match.match_factory_folder",
            lambda *args, **kwargs: (None, 0.0, "none"),
        )

        result = service.retry_factory_extraction("t-1")

        assert result["status"] == "completed"
        update = captured["update"]
        assert update["force_reextract"] is True
        assert "current_factory_data" not in update
        assert "factory_alias_overrides" not in update
