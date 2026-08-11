# -*- coding: utf-8 -*-
"""本机打开最终输出文件 + 下载白名单校验测试。

覆盖：
- POST /api/v1/batches/{thread_id}/open 的 403/404/415/200/500
- GET/HEAD /api/v1/batches/{thread_id}/output 的 403 路径逃逸
- app.ui.open_file.open_with_default_app 跨平台命令分发与异常包装

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/open_output_test.py -v
    或直接：python3 tests/open_output_test.py

隔离：通过 validation/_test_isolation.isolate_to_tmp 把 output_dir 指向临时目录。
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# 防御：提取/调度走 mock，避免 import 链触发真实 LLM 调用
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.ui.open_file import OpenFileError, open_with_default_app  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_open_output_test_")

# TestClient 默认 host 为 "testclient"，天然触发本机闸门 403
client = TestClient(app)
# 显式 127.0.0.1，用于测试闸门放行后的分支
local_client = TestClient(app, client=("127.0.0.1", 12345))


def _make_output_file(rel_path: str) -> Path:
    """在临时 output_dir 下创建空文件，返回绝对路径（未 resolve）。"""
    settings = get_settings()
    path = settings.output_dir_abs / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


# ---------------------------------------------------------------------------
# POST /api/v1/batches/{thread_id}/open
# ---------------------------------------------------------------------------


def test_open_rejects_non_localhost():
    """非本机请求（TestClient 默认 host=testclient）应 403。"""
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": ""}
        r = client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 403
    assert "本机" in r.json()["detail"]


def test_open_allows_localhost():
    """127.0.0.1 client 通过本机闸门，正常路径下返回 200。"""
    path = _make_output_file("test.xlsx")
    with patch("app.ui.router.service.get_batch_detail") as mock_detail, \
         patch("app.ui.router.open_with_default_app") as mock_open:
        mock_detail.return_value = {"final_output_path": str(path)}
        mock_open.return_value = None
        r = local_client.post("/api/v1/batches/TEST/open")

    assert r.status_code == 200, r.text
    expected_path = str(path.resolve())
    assert r.json() == {"ok": True, "path": expected_path}
    mock_open.assert_called_once_with(path.resolve())


def test_open_rejects_path_escape():
    """final_output_path 指向输出目录外时应 403。"""
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": "/etc/passwd"}
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 403
    assert "超出允许目录范围" in r.json()["detail"]


def test_open_not_found_batch():
    """批次不存在（get_batch_detail 抛 ValueError）→ 404。"""
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.side_effect = ValueError("not found")
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_open_empty_path():
    """final_output_path 为空 → 404。"""
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": ""}
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 404
    assert "没有最终输出文件路径" in r.json()["detail"]


def test_open_file_not_found():
    """路径在输出目录内但文件不存在 → 404。"""
    settings = get_settings()
    ghost = settings.output_dir_abs / "ghost.xlsx"
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": str(ghost)}
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 404
    assert "文件不存在" in r.json()["detail"]


def test_open_unsupported_extension():
    """路径在输出目录内但后缀不是 .xlsx/.xls → 415。"""
    path = _make_output_file("test.txt")
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": str(path)}
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 415
    assert "不支持的文件类型" in r.json()["detail"]


def test_open_returns_503_on_open_error():
    """open_with_default_app 抛 OpenFileError → 503，detail 透传中文消息。"""
    path = _make_output_file("test.xlsx")
    with patch("app.ui.router.service.get_batch_detail") as mock_detail, \
         patch("app.ui.router.open_with_default_app") as mock_open:
        mock_detail.return_value = {"final_output_path": str(path)}
        mock_open.side_effect = OpenFileError("中文消息")
        r = local_client.post("/api/v1/batches/TEST/open")
    assert r.status_code == 503
    assert r.json()["detail"] == "中文消息"


# ---------------------------------------------------------------------------
# GET /api/v1/batches/{thread_id}/output
# ---------------------------------------------------------------------------


def test_download_rejects_path_escape():
    """GET /output 对输出目录外的路径返回 403（新增白名单校验）。"""
    with patch("app.ui.router.service.get_batch_detail") as mock_detail:
        mock_detail.return_value = {"final_output_path": "/etc/passwd"}
        r = client.get("/api/v1/batches/TEST/output")
    assert r.status_code == 403
    assert "超出允许目录范围" in r.json()["detail"]


# ---------------------------------------------------------------------------
# app/ui/open_file.py 纯单元测试
# ---------------------------------------------------------------------------


def _open_file_module():
    """延迟 import，避免在模块顶层污染被测模块。"""
    from app.ui import open_file as _m
    return _m


@contextmanager
def _patch_platform(mod, *, os_name: str, sys_platform: str):
    """只改 os.name / sys.platform，不动整个模块对象（保留真实异常类）。"""
    with patch.object(mod.os, "name", os_name), patch.object(
        mod.sys, "platform", sys_platform
    ):
        yield


def test_open_file_macos():
    """macOS 下调用 open 命令。"""
    mod = _open_file_module()
    path = Path("/tmp/test.xlsx")
    with _patch_platform(mod, os_name="posix", sys_platform="darwin"), \
         patch.object(mod.subprocess, "run") as mock_run:
        mod.open_with_default_app(path)
    mock_run.assert_called_once_with(
        ["open", str(path)], check=True, timeout=10, capture_output=True
    )


def test_open_file_linux():
    """Linux 下调用 xdg-open 命令。"""
    mod = _open_file_module()
    path = Path("/tmp/test.xlsx")
    with _patch_platform(mod, os_name="posix", sys_platform="linux"), \
         patch.object(mod.subprocess, "run") as mock_run:
        mod.open_with_default_app(path)
    mock_run.assert_called_once_with(
        ["xdg-open", str(path)], check=True, timeout=10, capture_output=True
    )


def test_open_file_windows():
    """Windows 下调用 os.startfile。"""
    mod = _open_file_module()
    path = Path("C:/tmp/test.xlsx")
    with _patch_platform(mod, os_name="nt", sys_platform="win32"), \
         patch.object(mod.os, "startfile", create=True) as mock_startfile:
        mod.open_with_default_app(path)
    mock_startfile.assert_called_once_with(str(path))


def test_open_file_unsupported_platform():
    """不支持的平台抛 OpenFileError。"""
    mod = _open_file_module()
    with _patch_platform(mod, os_name="posix", sys_platform="freebsd"):
        with pytest.raises(OpenFileError) as exc_info:
            mod.open_with_default_app(Path("/tmp/test.xlsx"))
    assert "不支持的操作系统平台" in str(exc_info.value)


def test_open_file_wraps_file_not_found():
    """FileNotFoundError 包装为 OpenFileError。"""
    mod = _open_file_module()
    with _patch_platform(mod, os_name="posix", sys_platform="darwin"), \
         patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("command not found")):
        with pytest.raises(OpenFileError) as exc_info:
            mod.open_with_default_app(Path("/tmp/test.xlsx"))
    assert "不存在" in str(exc_info.value)


def test_open_file_wraps_called_process_error():
    """CalledProcessError 包装为 OpenFileError，并透传 stderr。"""
    mod = _open_file_module()
    err = subprocess.CalledProcessError(
        1, ["open", "/tmp/test.xlsx"], stderr=b"launch failed"
    )
    with _patch_platform(mod, os_name="posix", sys_platform="darwin"), \
         patch.object(mod.subprocess, "run", side_effect=err):
        with pytest.raises(OpenFileError) as exc_info:
            mod.open_with_default_app(Path("/tmp/test.xlsx"))
    assert "执行失败" in str(exc_info.value)
    assert "launch failed" in str(exc_info.value)


def test_open_file_wraps_timeout_expired():
    """TimeoutExpired 包装为 OpenFileError。"""
    mod = _open_file_module()
    err = subprocess.TimeoutExpired(["open", "/tmp/test.xlsx"], 10)
    with _patch_platform(mod, os_name="posix", sys_platform="darwin"), \
         patch.object(mod.subprocess, "run", side_effect=err):
        with pytest.raises(OpenFileError) as exc_info:
            mod.open_with_default_app(Path("/tmp/test.xlsx"))
    assert "10 秒内未启动默认程序" in str(exc_info.value)


def test_open_file_wraps_windows_oserror():
    """Windows os.startfile 抛 OSError 时包装为 OpenFileError。"""
    mod = _open_file_module()
    path = Path("C:/tmp/test.xlsx")
    with _patch_platform(mod, os_name="nt", sys_platform="win32"), \
         patch.object(mod.os, "startfile", create=True,
                      side_effect=OSError("no association")):
        with pytest.raises(OpenFileError) as exc_info:
            mod.open_with_default_app(path)
    assert "Windows 系统启动默认程序失败" in str(exc_info.value)


def main():
    """支持直接运行本文件。"""
    sys.exit(pytest.main([__file__, "-v"]))


if __name__ == "__main__":
    main()
