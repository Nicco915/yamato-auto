"""人工双屏审核界面 —— FastAPI 路由。

端点：
- GET  /review                                    审核单页（原生 HTML/JS）
- GET  /api/v1/review/{thread_id}/payload         当前挂起的审核 payload
- GET  /api/v1/review/{thread_id}/document        原始单据查看（左屏数据源）
        ?path=<绝对路径>&page=<N, 仅 PDF>
        PDF   → image/png（响应头 X-Page-Count 给出总页数，整份渲染带缓存）
        Excel → text/html 快照（excel_to_markdown → HTML 表格，带缓存）
        图片  → 原样流式返回

安全：path 经 resolve() 后必须落在白名单根目录内（默认 settings.upstream_root），
否则 403，防目录穿越。缓存键含文件 mtime，文件变更自动失效。

数据源通过 ReviewBackend 协议注入：
- 生产：RealBackend（从 LangGraph checkpoint 的 state.tasks[].interrupts 读取）
- 演示：demo_server.py 注入 MockBackend
"""
from __future__ import annotations

import html
import mimetypes
import time
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.config import get_settings
from app.extraction.excel_channel import excel_to_markdown
from app.extraction.vision_channel import render_pdf_pages

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
PDF_SUFFIXES = {".pdf"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}

# ---------------------------------------------------------------------------
# 路径白名单
# ---------------------------------------------------------------------------

_allowed_roots: list[Path] = []


def configure_review(allowed_roots: list[str] | None = None) -> None:
    """配置文件访问白名单根目录（可多个）。不传则用 settings.upstream_root。"""
    global _allowed_roots
    roots = allowed_roots or [get_settings().upstream_root]
    _allowed_roots = [Path(r).resolve() for r in roots]


def _resolve_whitelisted(path: str) -> Path:
    """把请求路径解析为白名单内的真实文件，越权/不存在则抛 HTTP 错误。"""
    if not _allowed_roots:
        configure_review()
    p = Path(path).expanduser().resolve()  # resolve 会同时解开符号链接
    if not any(p.is_relative_to(root) for root in _allowed_roots):
        raise HTTPException(status_code=403, detail="路径不在工厂文件夹白名单内")
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {p.name}")
    return p


# ---------------------------------------------------------------------------
# 渲染缓存（键含 mtime，文件变更自动失效；简单 dict，规模受工厂文件数约束）
# ---------------------------------------------------------------------------

_pdf_cache: dict[tuple[str, float], list[bytes]] = {}
_excel_cache: dict[tuple[str, float], str] = {}


def _render_pdf_cached(p: Path) -> list[bytes]:
    key = (str(p), p.stat().st_mtime)
    if key not in _pdf_cache:
        # 防止缓存无限增长：超过 20 份时清空（内网工具量级足够）
        if len(_pdf_cache) > 20:
            _pdf_cache.clear()
        _pdf_cache[key] = render_pdf_pages(str(p))
    return _pdf_cache[key]


def _markdown_to_html(md: str, title: str) -> str:
    """把 excel_to_markdown 的管道表转成带样式的 HTML 表格；非表格行转义后原样展示。"""
    rows_html: list[str] = []
    for line in md.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # 跳过分隔行 |---|---|
        if cells and all(set(c) <= set("-: ") and c for c in cells):
            continue
        if line.strip().startswith("|"):
            tds = "".join(f"<td>{html.escape(c)}</td>" for c in cells)
            rows_html.append(f"<tr>{tds}</tr>")
        elif line.strip():
            rows_html.append(f"<tr><td>{html.escape(line)}</td></tr>")
    table = "\n".join(rows_html)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; margin: 12px; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  td {{ border: 1px solid #bbb; padding: 4px 8px; white-space: nowrap; }}
  tr:first-child td {{ background: #f0f4f8; font-weight: 600; }}
</style></head>
<body><table>
{table}
</table></body></html>"""


def _render_excel_cached(p: Path) -> str:
    key = (str(p), p.stat().st_mtime)
    if key not in _excel_cache:
        if len(_excel_cache) > 20:
            _excel_cache.clear()
        md = excel_to_markdown(str(p))
        _excel_cache[key] = _markdown_to_html(md, p.name)
    return _excel_cache[key]


# ---------------------------------------------------------------------------
# 数据源后端（生产=LangGraph checkpoint；演示=mock）
# ---------------------------------------------------------------------------

class ReviewBackend(Protocol):
    """审核界面所需的最小数据接口。"""

    def get_payload(self, thread_id: str) -> dict[str, Any] | None:
        """返回当前挂起的审核 payload；未挂起/不存在返回 None。"""
        ...


class RealBackend:
    """生产后端：从 LangGraph checkpoint 读取 interrupt payload。

    需要 service.py 提供 get_review_payload(thread_id)（见设计文档 6.2 节）。
    """

    def get_payload(self, thread_id: str) -> dict[str, Any] | None:
        from app.api import service  # 延迟导入，避免 demo 依赖 LangGraph

        return service.get_review_payload(thread_id)  # type: ignore[attr-defined]


_backend: ReviewBackend | None = None


def set_review_backend(backend: ReviewBackend) -> None:
    """注入数据源（demo_server 用 MockBackend；生产合并时用 RealBackend）。"""
    global _backend
    _backend = backend


def _get_backend() -> ReviewBackend:
    return _backend or RealBackend()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("/review", response_class=HTMLResponse)
async def review_page() -> HTMLResponse:
    """审核单页。用法：/review?thread_id=xxx"""
    return HTMLResponse((STATIC_DIR / "review.html").read_text(encoding="utf-8"))


@router.get("/api/v1/review/{thread_id}/payload")
async def get_payload(thread_id: str) -> dict[str, Any]:
    """获取当前挂起待审的 payload（Node5 interrupt 抛出值）。"""
    payload = _get_backend().get_payload(thread_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="该任务没有待审核数据（未挂起或不存在）")
    return payload


@router.get("/api/v1/review/{thread_id}/document")
async def get_document(
    thread_id: str,  # noqa: ARG001 预留：未来可按 thread 收窄白名单到本工厂目录
    path: str = Query(..., description="单据文件绝对路径（必须在白名单内）"),
    page: int = Query(1, ge=1, description="PDF 页码，从 1 开始"),
) -> Response:
    """左屏单据查看：PDF→PNG 单页 / Excel→HTML 快照 / 图片流式返回。"""
    p = _resolve_whitelisted(path)
    suffix = p.suffix.lower()

    try:
        if suffix in PDF_SUFFIXES:
            t0 = time.monotonic()
            pages = _render_pdf_cached(p)
            if page > len(pages):
                raise HTTPException(status_code=404, detail=f"页码超出范围（共 {len(pages)} 页）")
            png = pages[page - 1]
            elapsed = time.monotonic() - t0
            return Response(
                content=png,
                media_type="image/png",
                headers={
                    "X-Page-Count": str(len(pages)),
                    "X-Render-Time": f"{elapsed:.2f}",
                    "Cache-Control": "private, max-age=300",
                },
            )

        if suffix in EXCEL_SUFFIXES:
            return HTMLResponse(
                content=_render_excel_cached(p),
                headers={"X-Page-Count": "1"},
            )

        if suffix in IMAGE_SUFFIXES:
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            return FileResponse(p, media_type=mime, headers={"X-Page-Count": "1"})

    except HTTPException:
        raise
    except Exception as e:  # 渲染失败（损坏文件等）
        raise HTTPException(status_code=500, detail=f"单据渲染失败: {e}") from e

    raise HTTPException(status_code=415, detail=f"不支持的文件类型: {suffix}")
