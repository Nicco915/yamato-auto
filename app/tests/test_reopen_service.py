# -*- coding: utf-8 -*-
"""reopen 已审核工厂相关 service 函数测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import service


class TestBuildReopenItemsPayload:
    """_build_reopen_items_payload 反向填充与拼写修复测试。"""

    def test_typo_fixed_and_runs_normally(self, monkeypatch):
        """拼写修复后函数不再抛 NameError，且正常返回反向填充后的 items。"""
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: None)

        items = [
            {
                "sku": "4549509515197",
                "extracted_data": {
                    "total_quantity": 500,
                    "total_net_weight": 3100,
                    "total_gross_weight": 3600,
                },
                "is_human_edited": False,
            }
        ]
        result = service._build_reopen_items_payload(items, "t-1", "山東中地")
        assert len(result) == 1
        assert result[0]["sku"] == "4549509515197"
        assert result[0]["extracted_data"]["total_net_weight"] == 3100
        assert result[0]["is_human_edited"] is False

    def test_audit_changes_override_extracted_data(self, monkeypatch):
        """changes_json 中的 diff 应覆写 extracted_data 对应字段。"""
        audit = SimpleNamespace(
            changes_json=json.dumps(
                [{"sku": "4549509515197", "field": "total_net_weight", "new": 3200}],
                ensure_ascii=False,
            ),
            new_skus_json="[]",
        )
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: audit)

        items = [
            {
                "sku": "4549509515197",
                "extracted_data": {"total_net_weight": 3100},
                "is_human_edited": False,
            }
        ]
        result = service._build_reopen_items_payload(items, "t-1", "山東中地")
        assert result[0]["extracted_data"]["total_net_weight"] == 3200
        assert result[0]["is_human_edited"] is True


class TestReopenFactoryForEdit:
    """reopen_factory_for_edit 数据读取测试。"""

    def _make_mock_graph(self, values):
        snap = SimpleNamespace(values=values, tasks=[])
        return SimpleNamespace(get_state=lambda cfg: snap)

    def test_read_from_factory_outputs_for_approved_factory(self, monkeypatch):
        """当前工厂不是目标工厂时，应从 factory_outputs 读取已审核数据。"""
        factory_outputs = {
            "山東中地": [
                {
                    "sku": "4549509515197",
                    "extracted_data": {
                        "total_quantity": 500,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                    },
                    "calculation": {"calculated_unit_net": 6.2},
                }
            ]
        }
        values = {
            "current_factory_data": {"factory_name": "青島達安"},
            "factory_outputs": factory_outputs,
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values)
        )
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: None)

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload is not None
        assert payload["factory_name"] == "山東中地"
        assert payload["reopen_mode"] is True
        assert len(payload["items"]) == 1
        assert payload["items"][0]["sku"] == "4549509515197"

    def test_falls_back_to_excel_when_factory_outputs_missing(self, monkeypatch):
        """factory_outputs 中无目标工厂时，应从最终输出 Excel 重建。"""
        values = {
            "current_factory_data": {"factory_name": "青島達安"},
            "factory_outputs": {},
            "final_output_path": (
                "/Users/nz/Downloads/yamato/app/app/output/test-3/containers/"
                "ContentsOfTheContainer_202550_青島ＸＤ_20260107_副本_filled.xlsx"
            ),
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values)
        )
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: None)

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload is not None
        assert payload["factory_name"] == "山東中地"
        skus = {i["sku"] for i in payload["items"]}
        assert "4549509515197" in skus

    def test_non_current_factory_source_documents_from_own_folder(
        self, monkeypatch, tmp_path
    ):
        """非当前工厂 reopen 时，source_documents 应来自 reopened 工厂自己的文件夹。"""
        own_docs = [str(tmp_path / "doc1.pdf"), str(tmp_path / "doc2.xlsx")]
        monkeypatch.setattr(
            service, "_list_source_documents", lambda folder: list(own_docs)
        )
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: None)

        factory_outputs = {
            "山東中地": [
                {
                    "sku": "4549509515197",
                    "extracted_data": {
                        "total_quantity": 500,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                        "source_file": str(tmp_path / "source.pdf"),
                    },
                }
            ]
        }
        values = {
            "current_factory_data": {
                "factory_name": "青島達安",
                "source_documents": ["/current/factory/other.pdf"],
            },
            "factory_outputs": factory_outputs,
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values)
        )

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["source_documents"] == own_docs
        assert "/current/factory/other.pdf" not in payload["source_documents"]

    def test_non_current_factory_missing_skus_computed_per_factory(
        self, monkeypatch
    ):
        """非当前工厂 reopen 时，missing_skus 按该工厂自己的 downstream_requirements 计算。"""
        monkeypatch.setattr(service, "_latest_approved_audit", lambda tid, fn: None)
        monkeypatch.setattr(service, "_list_source_documents", lambda folder: [])

        factory_outputs = {
            "山東中地": [
                {
                    "sku": "4549509515197",
                    "extracted_data": {
                        "total_quantity": 500,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                    },
                }
            ]
        }
        values = {
            "current_factory_data": {
                "factory_name": "青島達安",
                "missing_skus": ["should_not_appear"],
            },
            "factory_outputs": factory_outputs,
            "downstream_requirements": {
                "山東中地": ["4549509515197", "missing_sku_1"],
            },
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values)
        )

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["missing_skus"] == ["missing_sku_1"]
        assert "should_not_appear" not in payload["missing_skus"]


class TestRebuildItemsFromOutputExcel:
    """_rebuild_items_from_output_excel 聚合解析测试（test-3 实际输出回归）。"""

    def test_aggregates_sku_from_actual_output_excel(self):
        state = {
            "final_output_path": (
                "/Users/nz/Downloads/yamato/app/app/output/test-3/containers/"
                "ContentsOfTheContainer_202550_青島ＸＤ_20260107_副本_filled.xlsx"
            )
        }
        items = service._rebuild_items_from_output_excel(state, "山東中地")
        assert len(items) == 16

        by_sku = {i["sku"]: i for i in items}
        item = by_sku["4549509515197"]
        ext = item["extracted_data"]
        assert ext["total_quantity"] == 500.0
        assert ext["total_net_weight"] == 3100.0
        assert ext["total_gross_weight"] == 3600.0
        assert ext["weight_unit"] == "KG"
        assert ext["source_file"] != state["final_output_path"]
        assert ext["source_file"] == "reconstructed_from_output_excel"
        assert ext["sku_name"] == "◆ボリューム寝具３点セット　Ｓ"

        calc = item["calculation"]
        assert calc["calculated_unit_net"] == pytest.approx(6.2)
        assert calc["calculated_unit_gross"] == pytest.approx(7.2)

    def test_returns_empty_list_when_output_path_missing(self):
        assert service._rebuild_items_from_output_excel({}, "山東中地") == []

    def test_returns_empty_list_when_factory_not_in_excel(self):
        state = {
            "final_output_path": (
                "/Users/nz/Downloads/yamato/app/app/output/test-3/containers/"
                "ContentsOfTheContainer_202550_青島ＸＤ_20260107_副本_filled.xlsx"
            )
        }
        assert service._rebuild_items_from_output_excel(state, "不存在的工厂") == []
