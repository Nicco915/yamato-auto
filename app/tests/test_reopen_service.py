# -*- coding: utf-8 -*-
"""reopen 已审核工厂相关 service 函数测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

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


# ---------------------------------------------------------------------------
# 新格式快照 reopen（与 Node5 首次审核完全对齐）
# ---------------------------------------------------------------------------


class TestReopenWithNewSnapshotFormat:
    """factory_outputs[factory] 为 dict 快照时，reopen 走 build_review_payload。"""

    def _make_mock_graph(self, values):
        snap = SimpleNamespace(values=values, tasks=[])
        return SimpleNamespace(get_state=lambda cfg: snap)

    def _make_db_records_mock(self, records_by_factory):
        """构造 _enrich_items_with_db_records 用的主库 session mock。"""
        def factory_for(name):
            f = MagicMock()
            f.factory_id = 1
            return f

        def fake_get_session():
            class _Ctx:
                def __enter__(self_inner):
                    session = MagicMock()
                    session.scalar.side_effect = lambda q: (
                        factory_for("any") if "Factory" in str(type(q)) else None
                    )
                    session.scalars.return_value.all.return_value = [
                        SimpleNamespace(
                            sku_code=r["sku_code"],
                            name_cn=r.get("name_cn"),
                            name_en=r.get("name_en"),
                            name_jp=r.get("name_jp"),
                            hs_code=r.get("hs_code"),
                            inspection_required=r.get("inspection_required"),
                            unit_net_weight=r.get("unit_net_weight"),
                            unit_gross_weight=r.get("unit_gross_weight"),
                        )
                        for r in records_by_factory.get("any", [])
                    ]
                    return session

                def __exit__(self_inner, *args):
                    return False

            return _Ctx()

        return fake_get_session

    def test_new_snapshot_uses_factory_source_documents(self, monkeypatch):
        """新格式快照应原样使用快照里的 source_documents（不是当前工厂）。"""
        snapshot = {
            "factory_name": "山東中地",
            "calculated_items": [
                {
                    "sku": "4549509515197",
                    "extracted_data": {
                        "total_quantity": 500,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                    },
                    "is_new_sku": False,
                }
            ],
            "folder_path": "/data/factory/中地",
            "source_documents": ["/data/factory/中地/箱单.pdf"],
            "missing_skus": ["missing_1"],
            "extraction_issues": [],
            "extraction_coverage": {"extracted": 1, "expected": 2},
        }
        values = {
            "current_factory_data": {"factory_name": "青島達安"},
            "factory_outputs": {"山東中地": snapshot},
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )
        monkeypatch.setattr(service, "get_session", self._make_db_records_mock({}))

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["factory_name"] == "山東中地"
        assert payload["source_documents"] == ["/data/factory/中地/箱单.pdf"]
        assert payload["missing_skus"] == ["missing_1"]
        assert payload["reopen_mode"] is True

    def test_new_snapshot_old_sku_has_db_record_after_enrich(self, monkeypatch):
        """主库刷新后老 SKU 的 db_record 非空。"""
        snapshot = {
            "factory_name": "山東中地",
            "calculated_items": [
                {
                    "sku": "4549509515197",
                    "extracted_data": {
                        "total_quantity": 500,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                    },
                    "is_new_sku": False,
                }
            ],
            "folder_path": "/data/factory/中地",
            "source_documents": ["/data/factory/中地/箱单.pdf"],
            "missing_skus": [],
            "extraction_issues": [],
            "extraction_coverage": {},
        }
        values = {
            "current_factory_data": {"factory_name": "青島達安"},
            "factory_outputs": {"山東中地": snapshot},
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )

        # 直接 stub _enrich_items_with_db_records，跳过 SQL mock 的脆弱性
        def enrich(items, factory_name):
            for item in items:
                item["db_record"] = {
                    "name_cn": "中文品名",
                    "hs_code": "1234.56.78",
                    "inspection_required": 0,
                    "unit_net_weight": 6.2,
                }
            return items

        monkeypatch.setattr(service, "_enrich_items_with_db_records", enrich)

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        item = payload["items"][0]
        # 老 SKU 顶层字段从 db_record 提取
        assert item["name_cn"] == "中文品名"
        assert item["hs_code"] == "1234.56.78"
        assert item["db_record"]["unit_net_weight"] == 6.2
        # 没有 fields_to_fill
        assert "fields_to_fill" not in item

    def test_new_snapshot_new_sku_has_fields_to_fill(self, monkeypatch):
        """新 SKU 应带 fields_to_fill 和工厂默认商检。"""
        snapshot = {
            "factory_name": "正达",
            "calculated_items": [
                {
                    "sku": "9999999999999",
                    "extracted_data": {
                        "total_quantity": 100,
                        "total_net_weight": 50,
                        "total_gross_weight": 60,
                    },
                    "is_new_sku": True,
                }
            ],
            "folder_path": "/data/factory/正达",
            "source_documents": [],
            "missing_skus": [],
            "extraction_issues": [],
            "extraction_coverage": {},
        }
        values = {
            "current_factory_data": {"factory_name": "其他"},
            "factory_outputs": {"正达": snapshot},
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )
        monkeypatch.setattr(service, "get_session", self._make_db_records_mock({}))

        payload = service.reopen_factory_for_edit("thread-1", "正达")
        item = payload["items"][0]
        assert item["is_new_sku"] is True
        assert item["fields_to_fill"] == [
            "name_cn", "hs_code", "inspection_required",
        ]
        # 正达配置 inspection_required=1
        assert item["inspection_required"] == 1

    def test_new_snapshot_is_human_edited_preserved(self, monkeypatch):
        """is_human_edited 标记保留到 items（Node5 合并后结果不应被改）。"""
        snapshot = {
            "factory_name": "山東中地",
            "calculated_items": [
                {
                    "sku": "4549509515197",
                    "is_human_edited": True,
                    "extracted_data": {
                        "total_quantity": 999,
                        "total_net_weight": 3100,
                        "total_gross_weight": 3600,
                    },
                }
            ],
            "folder_path": None,
            "source_documents": [],
            "missing_skus": [],
            "extraction_issues": [],
            "extraction_coverage": {},
        }
        values = {
            "current_factory_data": {"factory_name": "其他"},
            "factory_outputs": {"山東中地": snapshot},
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )
        monkeypatch.setattr(service, "get_session", self._make_db_records_mock({}))

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["items"][0]["is_human_edited"] is True
        assert payload["items"][0]["extracted_data"]["total_quantity"] == 999

    def test_new_snapshot_extraction_field_preserved(self, monkeypatch):
        """extraction_issues / coverage 来自快照本身，不再取当前工厂。"""
        snapshot = {
            "factory_name": "山東中地",
            "calculated_items": [],
            "folder_path": "/x",
            "source_documents": [],
            "missing_skus": [],
            "extraction_issues": [{"level": "warning", "msg": "来自快照"}],
            "extraction_coverage": {"extracted": 0, "expected": 0},
        }
        values = {
            "current_factory_data": {
                "factory_name": "其他",
                "extraction_issues": [{"level": "warning", "msg": "当前工厂（错误）"}],
                "extraction_coverage": {"extracted": 99, "expected": 99},
            },
            "factory_outputs": {"山東中地": snapshot},
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )
        monkeypatch.setattr(service, "get_session", self._make_db_records_mock({}))

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["extraction_issues"][0]["msg"] == "来自快照"
        assert payload["extraction_coverage"] == {"extracted": 0, "expected": 0}


class TestReopenWithLegacyListFormat:
    """factory_outputs[factory] 为 list（list of items）旧格式兼容。"""

    def _make_mock_graph(self, values):
        snap = SimpleNamespace(values=values, tasks=[])
        return SimpleNamespace(get_state=lambda cfg: snap)

    def test_list_format_wraps_with_missing_skus(self, monkeypatch):
        """旧格式列表包装为 dict，missing_skus 按 downstream_requirements 算。"""
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
            "current_factory_data": {"factory_name": "其他"},
            "factory_outputs": factory_outputs,
            "downstream_requirements": {
                "山東中地": ["4549509515197", "missing_sku_1"],
            },
        }
        monkeypatch.setattr(
            service, "get_graph", lambda: self._make_mock_graph(values),
        )
        monkeypatch.setattr(service, "_list_source_documents", lambda folder: [])
        monkeypatch.setattr(service, "_resolve_reopen_factory_folder",
                             lambda state, name: None)

        payload = service.reopen_factory_for_edit("thread-1", "山東中地")
        assert payload["missing_skus"] == ["missing_sku_1"]
        assert payload["factory_name"] == "山東中地"
        assert payload["reopen_mode"] is True


class TestResolveFactorySnapshot:
    """_resolve_factory_snapshot 优先级链测试。"""

    def _make_values(self, factory_outputs_value, current_factory_data=None):
        return {
            "current_factory_data": current_factory_data or {},
            "factory_outputs": factory_outputs_value or {},
        }

    def test_new_format_snapshot_takes_priority(self):
        snapshot = {"factory_name": "X", "calculated_items": []}
        values = self._make_values({"X": snapshot})
        result = service._resolve_factory_snapshot(values, "X", {})
        # 浅拷贝（避免污染原 state），但内容等价
        assert result == snapshot
        assert result["factory_name"] == "X"

    def test_legacy_list_format_wrapped_to_dict(self):
        values = self._make_values({"X": [{"sku": "A"}]})
        result = service._resolve_factory_snapshot(values, "X", {})
        assert isinstance(result, dict)
        assert result["calculated_items"] == [{"sku": "A"}]
        assert result["factory_name"] == "X"

    def test_current_factory_when_snapshot_missing(self):
        cur = {"factory_name": "X", "calculated_items": [{"sku": "A"}]}
        values = self._make_values({}, cur)
        result = service._resolve_factory_snapshot(values, "X", cur)
        # 内容等价；dict() 浅拷贝
        assert result["factory_name"] == "X"
        assert result["calculated_items"] == [{"sku": "A"}]


    def test_returns_none_when_nothing_available(self):
        values = self._make_values({})
        # cur 没有匹配；Excel 兜底也没有文件路径
        result = service._resolve_factory_snapshot(values, "X", {})
        assert result is None

