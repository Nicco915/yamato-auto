# -*- coding: utf-8 -*-
"""审核页「打开本地文件」端点单元测试。

覆盖（任务清单 Step 1.2）：
1. 白名单内路径返回 200（mock subprocess.Popen / os.startfile / xdg-open）。
2. ../ 越权返回 403（被 _resolve_whitelisted 截下）。
3. 占位符路径（extraction_error: / reconstructed_from_output_excel /
   no_items_extracted / no_folder_matched）直接 400，不调系统。
4. 文件不存在返回 404。
5. darwin / win32 / linux 三平台 dispatch 代码路径都触达
   （monkeypatch app.review.router.sys.platform 切换分支）。
6. 不存在的 thread_id 不影响白名单行为（thread 仅参与 upstream_root 二级查询，
   不存在时走全局白名单）。

隔离：与 open_output_test 同源套路——isolate_to_tmp 之后用
configure_review(allowed_roots=[tmp_root]) 把白名单绑到临时根目录，
所有测试不碰 app/data/ 真实库。

安全红线：mock subprocess.Popen / os.startfile 防止测试期间真的弹窗/开程序。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# 防御：避免 import 链触发真实 LLM 调用（本测试不涉及提取线）
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.review import router as review_router  # noqa: E402
from app.review.router import configure_review  # noqa: E402
from app.ui.open_file import OpenFileError  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import app 模块之后（llm_client load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_review_open_local_test_")

# 把白名单显式绑到临时根（demo 风格：_allowed_roots_source=None，
# 不会被 _auto_refresh_roots 用 settings 字符串比较覆盖）
WHITELIST_ROOT = TMP / "factory_root"
WHITELIST_ROOT.mkdir(parents=True, exist_ok=True)
configure_review(allowed_roots=[str(WHITELIST_ROOT)])

# TestClient 默认 host="testclient"；本端点不做本机闸门
# （只复用路径白名单），所以默认 client 即可。
client = TestClient(app)


# ---------------------------------------------------------------------------
# 公共 helper
# ---------------------------------------------------------------------------

def _make_file(rel_path: str, content: bytes = b"x") -> Path:
    """在白名单根下造一个真实文件，返回绝对路径。"""
    p = WHITELIST_ROOT / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p.resolve()


# ---------------------------------------------------------------------------
# 1. 白名单内路径 → 200，dispatch 平台正确
# ---------------------------------------------------------------------------

def test_open_local_whitelisted_darwin():
    """白名单内路径，darwin 平台 → 200，subprocess.Popen 收到 ["open", path]。"""
    p = _make_file("good.xlsx")
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(p)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "path": str(p)}
    # args 必须是 list（shell=False 防注入），且第一项是 "open"
    args = mock_popen.call_args[0][0]
    assert isinstance(args, list)
    assert args[0] == "open"
    assert args[1] == str(p)
    # shell=False 关键字参数
    assert mock_popen.call_args.kwargs.get("shell", False) is False


def test_open_local_whitelisted_windows():
    """白名单内路径，win32 平台 → 200，os.startfile 被调用。"""
    p = _make_file("good2.pdf")
    with patch.object(review_router.sys, "platform", "win32"), \
         patch.object(review_router.os, "startfile", create=True) as mock_start:
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(p)},
        )
    assert r.status_code == 200, r.text
    mock_start.assert_called_once_with(str(p))


def test_open_local_whitelisted_linux():
    """白名单内路径，linux 平台 → 200，subprocess.Popen 收到 ["xdg-open", path]。"""
    p = _make_file("good3.png")
    with patch.object(review_router.sys, "platform", "linux"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(p)},
        )
    assert r.status_code == 200, r.text
    args = mock_popen.call_args[0][0]
    assert args[0] == "xdg-open"
    assert args[1] == str(p)
    assert mock_popen.call_args.kwargs.get("shell", False) is False


# ---------------------------------------------------------------------------
# 2. ../ 越权 → 403
# ---------------------------------------------------------------------------

def test_open_local_path_escape_403():
    """白名单外的 ../ 路径 → 403。"""
    outside = WHITELIST_ROOT.parent / "outside.txt"
    # 不创建文件——白名单判定应早于文件存在性检查
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(outside)},
        )
    assert r.status_code == 403
    assert "白名单" in r.json()["detail"]
    mock_popen.assert_not_called()


def test_open_local_relative_escape_403():
    """相对路径含 ../ 跳出白名单根 → 403。"""
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(WHITELIST_ROOT / "sub" / ".." / ".." / "etc" / "passwd")},
        )
    assert r.status_code == 403
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# 3. 占位符路径 → 400，不调系统
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placeholder", [
    "extraction_error:文件损坏",
    "reconstructed_from_output_excel",
    "no_items_extracted",
    "no_folder_matched",
    "",                # 空串同样视为无效
])
def test_open_local_placeholder_400(placeholder):
    """占位符 / 空路径 → 400（不传给 _resolve_whitelisted，也不调系统）。"""
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen, \
         patch.object(review_router.os, "startfile", create=True) as mock_start:
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": placeholder},
        )
    assert r.status_code == 400, (placeholder, r.text)
    assert "占位符" in r.json()["detail"] or "路径" in r.json()["detail"]
    mock_popen.assert_not_called()
    mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# 4. 文件不存在 → 404
# ---------------------------------------------------------------------------

def test_open_local_file_not_found_404():
    """白名单内路径，但文件已被删除 → 404。"""
    ghost = WHITELIST_ROOT / "ghost.xlsx"
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        r = client.post(
            f"/api/v1/review/TEST/open",
            params={"path": str(ghost)},
        )
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# 5. 三平台 dispatch 代码路径都触达（额外显式用例，覆盖"分支到达"语义）
# ---------------------------------------------------------------------------

def test_dispatch_darwin_uses_open_cmd():
    """darwin: subprocess.Popen 第一参数 = "open"。"""
    p = _make_file("dispatch_darwin.xlsx")
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        client.post(f"/api/v1/review/T/open", params={"path": str(p)})
    assert mock_popen.called
    assert mock_popen.call_args[0][0][0] == "open"


def test_dispatch_win32_uses_startfile():
    """win32: os.startfile 被调用（不经 subprocess.Popen）。"""
    p = _make_file("dispatch_win32.pdf")
    with patch.object(review_router.sys, "platform", "win32"), \
         patch.object(review_router.os, "startfile", create=True) as mock_start, \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        client.post(f"/api/v1/review/T/open", params={"path": str(p)})
    mock_start.assert_called_once_with(str(p))
    mock_popen.assert_not_called()


def test_dispatch_linux_uses_xdg_open():
    """linux: subprocess.Popen 第一参数 = "xdg-open"。"""
    p = _make_file("dispatch_linux.png")
    with patch.object(review_router.sys, "platform", "linux"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        client.post(f"/api/v1/review/T/open", params={"path": str(p)})
    assert mock_popen.called
    assert mock_popen.call_args[0][0][0] == "xdg-open"


def test_dispatch_unsupported_platform_503():
    """不支持的平台（freebsd）→ 503（OpenFileError 包装），detail 是中文提示。"""
    p = _make_file("dispatch_unsupported.docx")
    with patch.object(review_router.sys, "platform", "freebsd"):
        r = client.post(f"/api/v1/review/T/open", params={"path": str(p)})
    assert r.status_code == 503
    assert "不支持" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 不存在的 thread_id 不影响白名单（走全局白名单）
# ---------------------------------------------------------------------------

def test_unknown_thread_id_still_uses_global_whitelist():
    """thread_id 在 checkpoint 找不到时（service.get_batch_upstream_root 抛错），
    白名单仍按全局放行——thread 仅参与二级查 upstream_root。"""
    p = _make_file("with_unknown_thread.xlsx")
    # 让 service.get_batch_upstream_root 抛异常模拟 thread 不存在
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen, \
         patch(
            "app.api.service.get_batch_upstream_root",
            side_effect=RuntimeError("thread not found"),
         ) as mock_get:
        mock_popen.return_value = MagicMock()
        r = client.post(
            f"/api/v1/review/UNKNOWN_THREAD/open",
            params={"path": str(p)},
        )
    assert r.status_code == 200, r.text
    # 任何 thread_id（即使 service 抛错）都不会影响全局白名单放行——
    # 全局命中时 _batch_upstream_root 根本不会被调用（仅在 _allowed_roots
    # 不命中时才走二级）。mock_get 的 call_count 在全局命中分支下为 0 也合法。
    args = mock_popen.call_args[0][0]
    assert args[0] == "open"
    assert args[1] == str(p)


def test_thread_specific_root_fallback():
    """白名单不命中但 thread 专属 upstream_root 命中 → 200。

    证明 thread 确实参与白名单二级查询；不存在的 thread_id 退化为走
    全局白名单（test_unknown_thread_id_still_uses_global_whitelist）。
    """
    # 另一个根：全局白名单外，但作为 thread 的 upstream_root
    thread_root = TMP / "thread_specific_root"
    thread_root.mkdir(parents=True, exist_ok=True)
    p = thread_root / "in_thread_only.xlsx"
    p.write_bytes(b"x")

    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(review_router.subprocess, "Popen") as mock_popen, \
         patch(
            "app.api.service.get_batch_upstream_root",
            return_value=str(thread_root),
         ):
        mock_popen.return_value = MagicMock()
        r = client.post(
            f"/api/v1/review/EXISTING_THREAD/open",
            params={"path": str(p)},
        )
    assert r.status_code == 200, r.text
    args = mock_popen.call_args[0][0]
    assert args[0] == "open"


# ---------------------------------------------------------------------------
# 7. 错误透传：OpenFileError → 503（不泄露系统命令细节）
# ---------------------------------------------------------------------------

def test_open_local_open_file_error_503():
    """OpenFileError → 503，detail 是中文（与 app/ui/router 一致语义）。"""
    p = _make_file("openerr.xlsx")
    # 让 darwin 分支里 Popen 抛 OpenFileError 模拟「命令不存在」
    with patch.object(review_router.sys, "platform", "darwin"), \
         patch.object(
             review_router.subprocess, "Popen",
             side_effect=OpenFileError("macOS open 命令不存在"),
         ):
        r = client.post(f"/api/v1/review/T/open", params={"path": str(p)})
    assert r.status_code == 503
    assert "open 命令不存在" in r.json()["detail"]


def main():
    sys.exit(pytest.main([__file__, "-v"]))


if __name__ == "__main__":
    main()
