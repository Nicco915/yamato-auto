"""人工双屏审核界面 —— FastAPI 路由。

端点：
- GET  /review                                    审核单页（原生 HTML/JS）
- GET  /api/v1/review/{thread_id}/payload         当前挂起的审核 payload
- GET  /api/v1/review/{thread_id}/document        原始单据查看（左屏数据源）
        ?path=<绝对路径>&page=<N, PDF/Excel>
        PDF   → image/png（响应头 X-Page-Count 给出总页数，整份渲染带缓存）
        Excel → xls/xlsx/xlsm 经 soffice 转 PDF 后同样走 PNG 管线（原格式还原，
                支持翻页/缩放；soffice 不可用或转换失败回退 HTML 快照）；
                csv 无格式可还原，固定走 HTML 快照
        图片  → 原样流式返回

安全：path 经 resolve() 后必须落在白名单根目录内（默认 settings.upstream_root），
否则 403，防目录穿越。缓存键含文件 mtime，文件变更自动失效。

数据源通过 ReviewBackend 协议注入：
- 生产：RealBackend（从 LangGraph checkpoint 的 state.tasks[].interrupts 读取）
- 演示：demo_server.py 注入 MockBackend
"""
from __future__ import annotations

import hashlib
import html
import logging
import mimetypes
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.config import get_settings
from app.extraction.excel_channel import excel_to_markdown
from app.extraction.pipeline import convert_excel_to_pdf
from app.extraction.vision_channel import render_pdf_pages

logger = logging.getLogger(__name__)

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
PDF_SUFFIXES = {".pdf"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}
# 走 soffice→PDF→PNG 管线的 Excel 格式；csv 无格式可还原，固定 HTML 快照
EXCEL_VIA_PDF_SUFFIXES = {".xlsx", ".xlsm", ".xls"}

# 图片扩展名 -> MIME 硬编码映射：Windows 精简系统上 mimetypes 依赖注册表
# 可能返回 None，常见图片类型优先查此表，mimetypes 仅作兜底
_IMAGE_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

# ---------------------------------------------------------------------------
# 路径白名单
# ---------------------------------------------------------------------------

_allowed_roots: list[Path] = []
# 白名单来源：str=来自 settings.upstream_root 的原文（_auto_refresh_roots 的
# 比较基准，允许自动跟随 settings 变化）；None=显式 configure_review 配置
# （demo 等场景），settings 不是其事实来源，不做自动刷新
_allowed_roots_source: str | None = None
_roots_lock = threading.Lock()


def configure_review(allowed_roots: list[str] | None = None) -> None:
    """配置文件访问白名单根目录（可多个）。不传则用 settings.upstream_root。

    显式传 allowed_roots 时记录来源为 None（不自动刷新）；缺省时记录
    settings 原文，供 _auto_refresh_roots 每请求字符串比较。
    """
    global _allowed_roots, _allowed_roots_source
    if allowed_roots is None:
        roots = [get_settings().upstream_root]
        source: str | None = roots[0]
    else:
        roots = allowed_roots
        source = None
    with _roots_lock:
        _allowed_roots = [Path(r).resolve() for r in roots]
        _allowed_roots_source = source


def _auto_refresh_roots() -> None:
    """每请求白名单自动感知：settings.upstream_root 字符串变了才加锁重建。

    快路径只做字符串比较（get_settings 有 lru_cache，零 I/O）；唯一刷新
    来源是 settings.upstream_root，无任何外部注入口子。显式配置来源
    （_allowed_roots_source 为 None，demo）不自动刷新。
    """
    global _allowed_roots, _allowed_roots_source
    source = _allowed_roots_source
    if source is None:
        return
    current = get_settings().upstream_root
    if current == source and _allowed_roots:
        return
    with _roots_lock:
        # 双检：等锁期间可能已有别的请求重建过
        if current == _allowed_roots_source and _allowed_roots:
            return
        _allowed_roots = [Path(current).resolve()]
        _allowed_roots_source = current
        logger.info("[白名单] settings.upstream_root 已变更，白名单自动刷新: %s", current)


def refresh_review_roots() -> None:
    """按 settings.upstream_root 显式重建白名单（对话改路径后立即生效）。

    apply_paths 改完 upstream_root 后调用，无需等下一次请求的懒刷新。
    显式 configure_review(allowed_roots=...) 的部署（demo）不受影响。
    """
    if _allowed_roots_source is None and _allowed_roots:
        return  # 显式配置部署：settings 不是白名单事实来源，不覆盖
    configure_review()


# 批次级 root 小缓存（thread_id → checkpoint state 里的 upstream_root）：
# 只缓存 state 显式记录的 root；走 .env 缺省的批次（service 返回 None）
# 与查询异常一律不缓存——缺省批次由全局白名单（自动跟随 settings）覆盖，
# 缓存"当时的 settings"会在改路径后产生陈旧放行
_batch_root_cache: dict[str, Path] = {}


def _batch_upstream_root(thread_id: str) -> Path | None:
    """批次 checkpoint state 里的 upstream_root（服务端派生，创建时已校验 is_dir）。

    安全红线：绝不从请求参数取 root，唯一来源是 checkpoint state；
    state 无此键（走 .env 缺省）返回 None，由全局白名单覆盖。
    """
    cached = _batch_root_cache.get(thread_id)
    if cached is not None:
        return cached
    try:
        from app.api import service  # 延迟导入，避免 demo 依赖 LangGraph

        raw = service.get_batch_upstream_root(thread_id)
    except Exception as e:  # noqa: BLE001 checkpoint 读取失败不阻塞白名单判定
        logger.warning("[白名单] 批次 root 查询失败 thread=%s：%s: %s",
                       thread_id, type(e).__name__, e)
        return None
    if raw is None:
        return None
    root = Path(raw).expanduser().resolve()
    if len(_batch_root_cache) > 64:
        _batch_root_cache.clear()
    _batch_root_cache[thread_id] = root
    return root


def _resolve_whitelisted(path: str, thread_id: str | None = None) -> Path:
    """把请求路径解析为白名单内的真实文件，越权/不存在则抛 HTTP 错误。

    放行顺序：全局白名单（自动跟随 settings）→ 带 thread_id 时二级查
    批次 checkpoint state 的 upstream_root（改全局路径后旧批次仍可审）。
    """
    if not _allowed_roots:
        configure_review()
    _auto_refresh_roots()
    p = Path(path).expanduser().resolve()  # resolve 会同时解开符号链接
    if not any(p.is_relative_to(root) for root in _allowed_roots):
        batch_root = _batch_upstream_root(thread_id) if thread_id else None
        if batch_root is None or not p.is_relative_to(batch_root):
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
# Excel 原格式管线：soffice 转 PDF → render_pdf_pages → PNG（与 PDF 分支同构）
# ---------------------------------------------------------------------------

# Excel 渲染 PNG 缓存（键 = (源路径, mtime)，与 _pdf_cache 同模式）
_excel_png_cache: dict[tuple[str, float], list[bytes]] = {}


def _excel_pdf_cache_dir() -> Path:
    """Excel→PDF 转换结果缓存目录（数据目录下 cache/excel_pdf/）。"""
    d = get_settings().checkpoint_db_abs.parent / "cache" / "excel_pdf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _excel_to_pdf_cached(p: Path) -> Path | None:
    """Excel→PDF（文件名 = 源路径 hash，避免特殊字符/重名冲突）。

    新鲜度判定：PDF mtime >= 源文件 mtime 即视为最新（源文件变更后重转），
    跨进程重启也有效；转换在缓存目录下的临时工作区进行（含独立 soffice
    profile），成功后 move 为 hash 命名，失败清理现场返回 None。
    """
    cache_dir = _excel_pdf_cache_dir()
    digest = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:16]
    pdf = cache_dir / f"{digest}.pdf"
    if pdf.exists() and pdf.stat().st_mtime >= p.stat().st_mtime:
        return pdf
    work = Path(tempfile.mkdtemp(prefix="conv_", dir=cache_dir))
    try:
        produced = convert_excel_to_pdf(str(p), str(work))
        if produced is None:
            return None
        shutil.move(produced, pdf)
        return pdf
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_excel_pages_cached(p: Path) -> list[bytes] | None:
    """Excel→PDF→PNG 整份渲染（带缓存）；soffice 不可用/转换失败返回 None。

    返回 None 时由路由回退 _render_excel_cached 的 HTML 快照，
    绝不让单据查看因 LibreOffice 缺失而 500。
    """
    key = (str(p), p.stat().st_mtime)
    if key not in _excel_png_cache:
        pdf = _excel_to_pdf_cached(p)
        if pdf is None:
            return None
        try:
            pages = render_pdf_pages(str(pdf))
        except Exception:  # noqa: BLE001 - 转换出的 PDF 异常同样回退 HTML
            logger.warning("[excel渲染] PDF 渲染 PNG 失败，回退 HTML 快照：%s",
                           p, exc_info=True)
            return None
        if len(_excel_png_cache) > 20:
            _excel_png_cache.clear()
        _excel_png_cache[key] = pages
    return _excel_png_cache[key]


# ---------------------------------------------------------------------------
# 数据源后端（生产=LangGraph checkpoint；演示=mock）
# ---------------------------------------------------------------------------

class ReviewBackend(Protocol):
    """审核界面所需的最小数据接口。"""

    def get_payload(self, thread_id: str) -> dict[str, Any] | None:
        """返回当前挂起的审核 payload；未挂起/不存在返回 None。"""
        ...


class RealBackend:
    """生产后端：从 LangGraph checkpoint 读取 interrupt payload。"""

    def get_payload(self, thread_id: str) -> dict[str, Any] | None:
        from app.api import service  # 延迟导入，避免 demo 依赖 LangGraph

        return service.get_review_payload(thread_id)


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
    thread_id: str,
    path: str = Query(..., description="单据文件绝对路径（必须在白名单内）"),
    page: int = Query(1, ge=1, description="PDF 页码，从 1 开始"),
) -> Response:
    """左屏单据查看：PDF→PNG 单页 / Excel→HTML 快照 / 图片流式返回。

    thread_id 参与白名单判定：全局白名单未命中时，二级查该批次 checkpoint
    state 的 upstream_root（改全局路径后旧批次单据仍可放行）。
    """
    p = _resolve_whitelisted(path, thread_id=thread_id)
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

        if suffix in EXCEL_VIA_PDF_SUFFIXES:
            # 原格式路径：soffice→PDF→PNG，与 PDF 分支同构（翻页/缩放全复用）；
            # soffice 缺失或转换失败回退 HTML 快照，绝不让查看 500
            pages = _render_excel_pages_cached(p)
            if pages is not None:
                if page > len(pages):
                    raise HTTPException(
                        status_code=404,
                        detail=f"页码超出范围（共 {len(pages)} 页）")
                return Response(
                    content=pages[page - 1],
                    media_type="image/png",
                    headers={
                        "X-Page-Count": str(len(pages)),
                        "Cache-Control": "private, max-age=300",
                    },
                )
            logger.warning("[excel渲染] 走 HTML 快照回退：%s", p)
            return HTMLResponse(
                content=_render_excel_cached(p),
                headers={"X-Page-Count": "1"},
            )

        if suffix in EXCEL_SUFFIXES:  # .csv：无格式可还原，固定 HTML 快照
            return HTMLResponse(
                content=_render_excel_cached(p),
                headers={"X-Page-Count": "1"},
            )

        if suffix in IMAGE_SUFFIXES:
            # 优先硬编码映射（避免 Windows 精简系统注册表缺失导致 None），
            # mimetypes 仅作兜底
            mime = (_IMAGE_MIME_MAP.get(suffix)
                    or mimetypes.guess_type(p.name)[0]
                    or "application/octet-stream")
            return FileResponse(p, media_type=mime, headers={"X-Page-Count": "1"})

    except HTTPException:
        raise
    except Exception as e:  # 渲染失败（损坏文件等）
        raise HTTPException(status_code=500, detail=f"单据渲染失败: {e}") from e

    raise HTTPException(status_code=415, detail=f"不支持的文件类型: {suffix}")
