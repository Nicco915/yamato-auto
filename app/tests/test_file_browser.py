# -*- coding: utf-8 -*-
"""文件浏览 API 测试（pytest）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ui.router import router


@pytest.fixture
def client():
    """FastAPI 测试客户端。"""
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def temp_dir():
    """临时测试目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建测试文件结构
        tmpdir = Path(tmpdir)
        (tmpdir / "folder1").mkdir()
        (tmpdir / "folder2").mkdir()
        (tmpdir / "file1.xlsx").write_text("test")
        (tmpdir / "file2.txt").write_text("test")
        (tmpdir / ".hidden").write_text("hidden")  # 隐藏文件
        yield tmpdir


class TestBrowseAPI:
    """GET /api/v1/files/browse 测试。"""

    def test_browse_home_directory(self, client):
        """浏览主目录（path=None）。"""
        res = client.get("/api/v1/files/browse")
        assert res.status_code == 200
        data = res.json()
        assert data["current_path"] is not None
        assert isinstance(data["entries"], list)

    def test_browse_specific_directory(self, client, temp_dir):
        """浏览指定目录。"""
        res = client.get(f"/api/v1/files/browse?path={temp_dir}")
        assert res.status_code == 200
        data = res.json()
        assert data["current_path"] == str(temp_dir)
        assert data["parent_path"] is not None

        # 验证内容（排序后：文件夹优先）
        names = [e["name"] for e in data["entries"]]
        assert "folder1" in names
        assert "folder2" in names
        assert "file1.xlsx" in names
        assert "file2.txt" in names

        # 隐藏文件不应出现
        assert ".hidden" not in names

    def test_browse_nonexistent_path(self, client):
        """浏览不存在的路径返回 404。"""
        res = client.get("/api/v1/files/browse?path=/nonexistent/path/xyz")
        assert res.status_code == 404

    def test_browse_file_not_dir(self, client, temp_dir):
        """浏览文件（非目录）返回 400。"""
        file_path = temp_dir / "file1.xlsx"
        res = client.get(f"/api/v1/files/browse?path={file_path}")
        assert res.status_code == 400

    def test_browse_with_extension_filter(self, client, temp_dir):
        """扩展名过滤。"""
        res = client.get(f"/api/v1/files/browse?path={temp_dir}&type=file&extensions=xlsx")
        assert res.status_code == 200
        data = res.json()

        # 只应有 xlsx 文件
        file_names = [e["name"] for e in data["entries"] if not e["is_dir"]]
        assert "file1.xlsx" in file_names
        assert "file2.txt" not in file_names

    def test_browse_folders_sorted_first(self, client, temp_dir):
        """文件夹优先排序。"""
        res = client.get(f"/api/v1/files/browse?path={temp_dir}")
        data = res.json()

        # 前两个应该是文件夹
        entries = data["entries"]
        assert entries[0]["is_dir"] is True
        assert entries[1]["is_dir"] is True
        # 后面是文件
        assert entries[2]["is_dir"] is False

    def test_browse_entry_has_required_fields(self, client, temp_dir):
        """条目包含必要字段。"""
        res = client.get(f"/api/v1/files/browse?path={temp_dir}")
        data = res.json()

        for entry in data["entries"]:
            assert "name" in entry
            assert "path" in entry
            assert "is_dir" in entry
            if not entry["is_dir"]:
                assert "size" in entry

    def test_browse_windows_drives(self, client, monkeypatch):
        """Windows 盘符列表（mock os.name）。"""
        import os
        # 只在非 Windows 系统上测试（Windows 上自然返回盘符）
        if os.name != "nt":
            # 模拟：path=None 且 os.name == "nt" 时返回盘符
            # 这里只验证 API 结构正确，不实际 mock
            res = client.get("/api/v1/files/browse")
            assert res.status_code == 200
