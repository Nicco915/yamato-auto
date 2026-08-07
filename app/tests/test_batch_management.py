# -*- coding: utf-8 -*-
"""批次管理功能测试（pytest）。"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.config import get_settings


class TestBatchConfig:
    """批次配置文件读写测试。"""

    def test_write_and_read_batch_config(self, tmp_path):
        """写入并读取批次配置。"""
        from app.api.service import _write_batch_config, _read_batch_config

        batch_id = "test-batch-config-001"
        config = {
            "thread_id": batch_id,
            "downstream_file_path": "/data/test.xlsx",
            "upstream_root": "/data/factories/",
            "created_at": "2026-08-07T10:00:00",
            "last_run_at": "2026-08-07T10:05:00",
            "run_count": 1,
        }

        # Mock output_dir to tmp_path
        settings = get_settings()
        original_output_dir = settings.output_dir
        settings.output_dir = str(tmp_path / "output")

        try:
            _write_batch_config(batch_id, config)

            # 验证文件存在
            config_path = tmp_path / "output" / batch_id / "batch_config.json"
            assert config_path.exists()

            # 读取验证
            read_config = _read_batch_config(batch_id)
            assert read_config is not None
            assert read_config["thread_id"] == batch_id
            assert read_config["downstream_file_path"] == "/data/test.xlsx"
            assert read_config["run_count"] == 1
        finally:
            settings.output_dir = original_output_dir

    def test_read_nonexistent_config(self):
        """读取不存在的配置返回 None。"""
        from app.api.service import _read_batch_config

        result = _read_batch_config("nonexistent-batch-999")
        assert result is None


class TestBatchState:
    """批次状态文件写入测试。"""

    def test_write_batch_state(self, tmp_path):
        """写入批次状态文件。"""
        from app.api.service import _write_batch_state

        batch_id = "test-batch-state-001"
        state = {
            "downstream_requirements": {"工厂A": ["SKU1"], "工厂B": ["SKU2"]},
            "pending_factories": ["工厂A", "工厂B"],
            "current_factory_data": {"factory_name": "工厂A"},
            "deferred_factories": [],
        }

        settings = get_settings()
        original_output_dir = settings.output_dir
        settings.output_dir = str(tmp_path / "output")

        try:
            _write_batch_state(batch_id, "running", state=state)

            state_path = tmp_path / "output" / batch_id / "batch_state.json"
            assert state_path.exists()

            data = json.loads(state_path.read_text())
            assert data["batch_id"] == batch_id
            assert data["status"] == "running"
            assert "工厂A" in data["factories"]["total"]
            assert data["factories"]["current"] == "工厂A"
            assert data["started_at"] is not None
        finally:
            settings.output_dir = original_output_dir

    def test_write_batch_state_error(self, tmp_path):
        """写入错误状态。"""
        from app.api.service import _write_batch_state

        batch_id = "test-batch-error-001"

        settings = get_settings()
        original_output_dir = settings.output_dir
        settings.output_dir = str(tmp_path / "output")

        try:
            _write_batch_state(batch_id, "error", error="test error message")

            state_path = tmp_path / "output" / batch_id / "batch_state.json"
            data = json.loads(state_path.read_text())
            assert data["status"] == "error"
            assert data["error"] == "test error message"
        finally:
            settings.output_dir = original_output_dir


class TestParseDownstreamFile:
    """parse_downstream_file 函数测试。"""

    def test_parse_nonexistent_file(self):
        """解析不存在的文件抛异常。"""
        from app.nodes.parse_downstream import parse_downstream_file

        with pytest.raises(FileNotFoundError):
            parse_downstream_file("/nonexistent/path/file.xlsx")


class TestBatchPathUtils:
    """批次路径工具函数测试。"""

    def test_batch_output_dir(self):
        """批次输出目录路径正确。"""
        settings = get_settings()
        path = settings.batch_output_dir("ETD0725-中地")
        assert "ETD0725-中地" in str(path)
        assert str(path).endswith("ETD0725-中地")

    def test_batch_containers_dir(self):
        """装箱单目录路径正确。"""
        settings = get_settings()
        path = settings.batch_containers_dir("ETD0725-中地")
        assert "containers" in str(path)

    def test_batch_declarations_dir(self):
        """报关单目录路径正确。"""
        settings = get_settings()
        path = settings.batch_declarations_dir("ETD0725-中地")
        assert "declarations" in str(path)

    def test_safe_path_tag(self):
        """路径标签安全化。"""
        settings = get_settings()

        # 正常批次号不变
        assert settings.safe_path_tag("ETD0725-中地") == "ETD0725-中地"

        # 危险字符被替换
        assert "/" not in settings.safe_path_tag("batch/2026")
        assert ".." not in settings.safe_path_tag("../../../etc/passwd")

        # 中文保留
        assert "中地" in settings.safe_path_tag("ETD0725-中地")
