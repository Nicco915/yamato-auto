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
- GET  /api/v1/review/{thread_id}/document/hit    SKU 高亮定位（左屏叠加层数据源）
        ?path=<绝对路径>&q=<搜索文本>
        → {"hits": [{"page": 页码, "rects": [[x0,y0,x1,y1] 页面分数坐标]}, ...]}
        无文本层/无命中/搜索失败一律返回空 hits，前端静默降级为普通翻页

安全：path 经 resolve() 后必须落在白名单根目录内（默认 settings.upstream_root），
否则 403，防目录穿越。缓存键含文件 mtime，文件变更自动失效。

数据源通过 ReviewBackend 协议注入：
- 生产：RealBackend（从 LangGraph checkpoint 的 state.tasks[].interrupts 读取）
- 演示：demo_server.py 注入 MockBackend
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from app.ui.open_file import OpenFileError

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
# SKU 高亮定位：在 PDF 文本层搜索 SKU，返回整行扩展后的页面分数坐标
# ---------------------------------------------------------------------------


def _find_text_hits(pdf_path: str, query: str) -> list[dict]:
    """在 PDF 各页搜索 query，返回命中页码 + 整行扩展后的页面相对分数坐标。

    - 逐页 page.search_for(query)；无文本层（扫描件）/无命中 → 空列表，不抛错；
    - 分数坐标 = rect / page.rect 宽高：PNG 渲染是页面的等比缩放，分数坐标
      与渲染 dpi 无关，前端直接当百分比用（缩放时高亮框天然跟随）；
    - 整行扩展：取垂直中心落在命中 rect y 带（上下各放宽 50% 带高容差）内的
      所有词，横向扩到这些词的 min(x0)/max(x1)（视觉上即整行高亮）；
      y 带保持命中 rect 原样；x 范围 cap 在 [0, 页宽]；
    - 返回 [{"page": 1-based 页码, "rects": [[x0,y0,x1,y1], ...]}]，跳过无命中页。
    """
    import fitz  # 模块顶部已引入 vision_channel（其依赖 fitz），此处局部导入保持函数自洽

    hits: list[dict] = []
    doc = fitz.open(pdf_path)
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            found = page.search_for(query)
            if not found:
                continue
            pw = page.rect.width or 1.0
            ph = page.rect.height or 1.0
            words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
            rects: list[list[float]] = []
            for r in found:
                y0, y1 = r.y0, r.y1
                band_h = (y1 - y0) or 1.0
                cy0, cy1 = y0 - band_h * 0.5, y1 + band_h * 0.5
                row_words = [w for w in words if cy0 <= (w[1] + w[3]) / 2 <= cy1]
                if row_words:
                    x0 = min(min(w[0] for w in row_words), r.x0)
                    x1 = max(max(w[2] for w in row_words), r.x1)
                else:
                    x0, x1 = r.x0, r.x1
                x0 = max(0.0, min(x0, pw))
                x1 = max(0.0, min(x1, pw))
                rects.append([
                    round(x0 / pw, 5), round(y0 / ph, 5),
                    round(x1 / pw, 5), round(y1 / ph, 5),
                ])
            if rects:
                hits.append({"page": page_index + 1, "rects": rects})
    finally:
        doc.close()
    return hits


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


@router.get("/api/v1/review/{thread_id}/reopen/{factory_name}")
async def get_reopen_payload(thread_id: str, factory_name: str) -> dict[str, Any]:
    """重开已审核工厂的可编辑 payload（reopen 模式）。

    从 checkpoint state + ReviewAudit 反向构建，不污染 LangGraph state。
    找不到工厂或数据时返回 404。
    """
    from app.api import service
    payload = service.reopen_factory_for_edit(thread_id, factory_name)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到工厂 {factory_name} 的审核数据",
        )
    return payload


@router.post("/api/v1/review/{thread_id}/reopen/{factory_name}")
async def post_reopen_payload(thread_id: str, factory_name: str,
                              request: dict[str, Any]) -> dict[str, Any]:
    """把 reopen 模式的编辑结果写回 Excel + master.db（不污染 LangGraph state）。

    request 结构同 resume_data：{"approved": bool, "items": [...]}
    """
    from app.api import service
    try:
        return await __import__("asyncio").to_thread(
            service.apply_reopen_payload, thread_id, factory_name, request,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"reopen 写回失败: {type(e).__name__}: {e}",
        )


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


# 可搜索定位的单据类型（csv 无格式快照、图片无文本层，不支持命中定位）
_HIT_SEARCHABLE_SUFFIXES = PDF_SUFFIXES | EXCEL_VIA_PDF_SUFFIXES


@router.get("/api/v1/review/{thread_id}/document/hit")
async def get_document_hit(
    thread_id: str,
    path: str = Query(..., description="单据文件绝对路径（必须在白名单内）"),
    q: str = Query(..., description="搜索文本（通常是 SKU 条码）"),
) -> dict[str, Any]:
    """左屏高亮定位：在单据（PDF / Excel 转换出的 PDF）中搜索 q，
    返回命中页码 + 整行扩展的页面分数坐标（前端叠加层画框用）。

    定位是增强功能：无文本层 / 无命中 / 转换或搜索失败一律返回空 hits
    （带 reason），前端静默降级为普通翻页，绝不 500 阻塞审核。
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="q 不能为空")
    p = _resolve_whitelisted(path, thread_id=thread_id)
    suffix = p.suffix.lower()
    if suffix not in _HIT_SEARCHABLE_SUFFIXES:
        return {"hits": [], "total": 0, "reason": "unsupported_type"}

    if suffix in PDF_SUFFIXES:
        pdf_path: Path | None = p
    else:
        # Excel：复用 /document 同一条 soffice→PDF 磁盘缓存，不重复转换
        pdf_path = await asyncio.to_thread(_excel_to_pdf_cached, p)
        if pdf_path is None:
            return {"hits": [], "total": 0, "reason": "pdf_unavailable"}

    try:
        hits = await asyncio.to_thread(_find_text_hits, str(pdf_path), q)
    except Exception as e:  # noqa: BLE001 - 定位失败降级为空，不阻塞审核
        logger.warning("[高亮定位] 搜索失败 path=%s: %s: %s", p, type(e).__name__, e)
        return {"hits": [], "total": 0, "reason": "search_failed"}
    total = sum(len(h["rects"]) for h in hits)
    return {"hits": hits, "total": total}


# ---------------------------------------------------------------------------
# 「打开本地文件」端点：左屏 doc-toolbar 触发
# ---------------------------------------------------------------------------

# 与前端 isRealDocPath() 对齐的占位符集合——禁止传给后端去开系统程序
# （前端已经会先灰显按钮，后端再做一道防御：避免外部伪造请求）
_DENY_PATH_MARKERS = {
    "reconstructed_from_output_excel",
    "no_items_extracted",
    "no_folder_matched",
}


def _is_placeholder_path(p: str) -> bool:
    """路径是不是审核页 source_documents 里的「占位符」（非真实文件）？

    - extraction_error:xxx           提取失败留痕
    - reconstructed_from_output_excel / no_items_extracted / no_folder_matched
    - 空串
    """
    if not p or not p.strip():
        return True
    if p in _DENY_PATH_MARKERS:
        return True
    if p.startswith("extraction_error:"):
        return True
    return False


def _launch_local(path: Path) -> None:
    """跨平台「用系统默认程序打开本地文件」（模块顶层独立函数便于测试 mock）。

    分派规则（与 app/ui/open_file.open_with_default_app 同源，差异：
    - 这里用 subprocess.Popen（异步启动，立即返回，不等默认程序退出），
      避免 os.startfile 之外还阻塞在 subprocess.run 的 timeout 上；
    - 错误以 OpenFileError 统一包装。
    """
    # Windows：os.startfile 非阻塞
    if sys.platform == "win32":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
        except OSError as e:
            raise OpenFileError(f"Windows 系统启动默认程序失败: {e}") from e
        return

    cmd: list[str]
    if sys.platform == "darwin":
        cmd = ["open", str(path)]
        err_label = "macOS open 命令"
    elif sys.platform.startswith("linux"):
        cmd = ["xdg-open", str(path)]
        err_label = "xdg-open 命令"
    else:
        raise OpenFileError(
            f"不支持的操作系统平台: sys.platform={sys.platform}, os.name={os.name}"
        )

    try:
        # shell=False + list args 防命令注入；截断 stderr 防止意外信息泄露
        subprocess.Popen(
            cmd,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as e:
        # 命令不存在（Linux 上 xdg-utils 未装最常见）
        raise OpenFileError(f"{err_label}不存在，无法打开文件: {e}") from e
    except Exception as e:
        raise OpenFileError(f"{err_label}执行失败: {e}") from e


@router.post("/api/v1/review/{thread_id}/open")
async def open_local(
    thread_id: str,
    path: str = Query(..., description="单据文件绝对路径（必须在白名单内）"),
) -> dict[str, Any]:
    """左屏 doc-toolbar「打开本地文件」按钮：服务端启动系统默认程序。

    与 GET /document（PNG 渲染回浏览器）相对——本端点把文件丢给本机默认
    程序（Excel / WPS / Adobe / 图片查看器等），用户编辑保存后直接回到
    服务器上的原文件。

    安全：
    - path 占位符（前端 isRealDocPath 为 false 的四种值）→ 400，不调系统；
    - 复用 _resolve_whitelisted 走全局白名单 + thread 二级 upstream_root，
      越权 403；
    - 文件不存在 404；
    - subprocess.Popen shell=False + list args 防命令注入；
    - 失败仅记录 logger.warning（路径+原因），前端只看到 5xx 的中文 detail
      （不泄露命令细节）。

    异常：
    - 400：path 是占位符
    - 403：路径越权（_resolve_whitelisted 抛）
    - 404：文件不存在（_resolve_whitelisted 抛）
    - 503：OpenFileError（命令不存在 / 启动失败）
    - 500：其他未捕获异常
    """
    # 1) 占位符防御（早于白名单，避免白名单 resolve 把 ext 异常当 500）
    if _is_placeholder_path(path):
        raise HTTPException(
            status_code=400,
            detail="该路径是占位符，无法打开本地文件",
        )

    # 2) 白名单 + 存在性（thread_id 仅参与批次 upstream_root 二级查询）
    p = _resolve_whitelisted(path, thread_id=thread_id)

    # 3) 异步启动系统默认程序（放线程池，不阻塞事件循环）
    try:
        await asyncio.to_thread(_launch_local, p)
    except OpenFileError as e:
        # logger 留痕（含路径与原因，运维排障用），前端只看到 503 中文 detail
        logger.warning("[打开本地文件] 启动默认程序失败 path=%s: %s", p, e)
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.warning("[打开本地文件] 未预期异常 path=%s: %s: %s",
                       p, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="启动默认程序失败") from e

    return {"ok": True, "path": str(p)}


# ---------------------------------------------------------------------------
# D2「重新识别这个文件」端点：左屏 doc-toolbar 触发，强制重提选中单据
# ---------------------------------------------------------------------------


@router.post("/api/v1/review/{thread_id}/reextract")
async def reextract_document(thread_id: str,
                             request: dict[str, Any]) -> dict[str, Any]:
    """强制重提单个单据文件，返回 review item 形状供前端合并。

    body: {"path": "<绝对路径>", "factory_name": "<工厂名>"}

    安全：
    - path 占位符 → 400（与 open_local 同口径防御）；
    - 复用 _resolve_whitelisted 走全局白名单 + thread 二级 upstream_root，
      越权 403 / 文件不存在 404；
    - 提取失败（通道报错/不支持类型/主库异常）→ 400/500 with detail。

    返回 {"items": [...], "source_file": path}——纯一次性计算，
    不写 LangGraph checkpoint、不写 session 缓存。
    """
    path = str((request or {}).get("path") or "")
    factory_name = str((request or {}).get("factory_name") or "")
    if not path or not factory_name:
        raise HTTPException(status_code=400,
                            detail="path 与 factory_name 均为必填")
    if _is_placeholder_path(path):
        raise HTTPException(status_code=400,
                            detail="该路径是占位符，无法重新识别")

    p = _resolve_whitelisted(path, thread_id=thread_id)

    from app.api import service
    try:
        return await asyncio.to_thread(
            service.reextract_document, thread_id, str(p), factory_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.warning("[reextract] 强制重提失败 path=%s: %s: %s",
                       p, type(e).__name__, e)
        raise HTTPException(
            status_code=500,
            detail=f"重新识别失败: {type(e).__name__}: {e}",
        ) from e


# ---------------------------------------------------------------------------
# 「保存为永久对照」端点：把本批次临时对照/模糊命中落为 factory_aliases 永久别名
# ---------------------------------------------------------------------------

# 允许从 current_factory_data 反推 folder 的匹配方式（非确定性命中才需要人工确认落别名）
_SAVEABLE_MATCH_METHODS = ("fuzzy", "contains")


class SaveAliasRequest(BaseModel):
    """save-alias 请求体。安全红线：只有 factory 一个字段——
    folder 绝不从客户端读，一律由服务端从 checkpoint state 派生。"""
    factory: str


def _get_batch_state_values(thread_id: str) -> dict[str, Any] | None:
    """读取批次 checkpoint state.values（模块顶层独立函数，便于测试 monkeypatch）。

    复用 service.get_order_state（与 get_batch_summary 等同一条取 state 路径）；
    thread 不存在（无 checkpoint）返回 None。
    """
    from app.api import service  # 延迟导入，避免 demo 依赖 LangGraph

    state = service.get_order_state(thread_id)
    if not state.get("exists"):
        return None
    return state.get("values") or {}


def _resolve_savable_folder(values: dict[str, Any], factory: str) -> str | None:
    """从 state 派生可保存的 工厂→文件夹 对照（唯一 folder 来源，绝不读客户端）。

    两个来源，按优先级：
    1. factory_alias_overrides[factory]：本批次临时对照（用户本轮确认过）；
    2. current_factory_data：仅当当前工厂就是目标工厂、match_method 为
       fuzzy/contains（非确定性命中）且 folder_path 非空时，取 folder_path
       的 basename。
    都没有返回 None（路由层转 400）。
    """
    overrides = values.get("factory_alias_overrides") or {}
    folder = overrides.get(factory)
    if folder:
        return str(folder)

    cur = values.get("current_factory_data") or {}
    if (
        cur.get("factory_name") == factory
        and cur.get("match_method") in _SAVEABLE_MATCH_METHODS
        and cur.get("folder_path")
    ):
        return Path(str(cur["folder_path"])).name
    return None


def _ensure_factory_short_name(factory: str, folder: str) -> None:
    """保证 factory 行存在且 short_name 与 folder 一致（冲突抛 HTTPException 409）。

    - factory 不存在：新建（factory_name=factory, short_name=folder）；
    - short_name 为空：回填 folder；
    - short_name 非空且 != folder：409，请用户到主数据维护页处理；
    - folder 已被其他工厂占用为 short_name：409（否则 save_alias_entries
      按 short_name 反查会把别名错挂到别的工厂）。
    """
    from sqlalchemy import select

    from app.db.models import Factory
    from app.db.session import get_session

    with get_session() as sess:
        fac = sess.scalar(select(Factory).where(Factory.factory_name == factory))
        if fac is not None and fac.short_name and fac.short_name != folder:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"该工厂已有中文短名 {fac.short_name}，与文件夹 {folder} "
                    "不一致，请到主数据维护页处理"
                ),
            )
        owner = sess.scalar(select(Factory).where(Factory.short_name == folder))
        if owner is not None and (fac is None or owner.factory_id != fac.factory_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"文件夹 {folder} 已是工厂 {owner.factory_name} 的中文短名，"
                    "请到主数据维护页处理"
                ),
            )
        if fac is None:
            sess.add(Factory(factory_name=factory, short_name=folder))
        elif not fac.short_name:
            fac.short_name = folder
        sess.commit()


def _audit_save_alias(thread_id: str, factory: str, folder: str,
                      saved: dict[str, Any]) -> None:
    """写 review_audits 留痕（result_status='save_alias'）。

    审计是辅助设施：任何失败只记警告，绝不阻断主流程（同 service._write_audit）。
    """
    try:
        from app.db.models import ReviewAudit
        from app.db.session import get_session

        with get_session() as sess:
            sess.add(ReviewAudit(
                thread_id=thread_id,
                factory_name=factory,
                approved=True,
                edited_count=0,
                changes_json=json.dumps(
                    [{"field": "alias", "old": None, "new": folder}],
                    ensure_ascii=False,
                ),
                new_skus_json="[]",
                result_status="save_alias",
            ))
            sess.commit()
    except Exception as e:  # noqa: BLE001 故意包死，见 docstring
        logger.warning("⚠️⚠️ [审计落库失败] thread=%s 别名已保存，但 "
                       "save_alias 留痕写入失败：%s: %s",
                       thread_id, type(e).__name__, e)


def _save_alias_impl(thread_id: str, factory: str) -> dict[str, Any]:
    """save-alias 主流程（同步实现，由路由层 asyncio.to_thread 包裹）。"""
    from app.factory_match import save_alias_entries, validate_subfolder

    factory = (factory or "").strip()
    if not factory:
        raise HTTPException(status_code=400, detail="工厂名不能为空")

    # 1) 批次存在性 + state 派生 folder（安全红线：folder 绝不来自客户端）
    values = _get_batch_state_values(thread_id)
    if values is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {thread_id}")
    folder = _resolve_savable_folder(values, factory)
    if not folder:
        raise HTTPException(status_code=400, detail="该工厂没有可保存的对照建议")

    # 2) folder 必须是 upstream_root（state 优先，settings 兜底）下现存一级子目录
    upstream_root = values.get("upstream_root") or get_settings().upstream_root
    try:
        validate_subfolder(upstream_root, folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 3) short_name 冲突处理（409）+ factory 行创建/回填
    _ensure_factory_short_name(factory, folder)

    # 4) 落永久别名（DB 优先、json 回退；upsert 语义，重复调用幂等）
    saved = save_alias_entries({factory: folder})

    # 5) 审计留痕（失败不阻断）
    _audit_save_alias(thread_id, factory, folder, saved)

    logger.info("[save-alias] thread=%s 工厂=%s -> 文件夹=%s saved=%s",
                thread_id, factory, folder, saved)
    return {
        "ok": True,
        "alias": factory,
        "short_name": folder,
        "overwritten": saved.get("overwritten") or [],
    }


@router.post("/api/v1/review/{thread_id}/save-alias")
async def save_alias(thread_id: str, request: SaveAliasRequest) -> dict[str, Any]:
    """把本批次临时对照/模糊命中的 工厂→文件夹 存为永久别名（A 级确认）。

    folder 由服务端从 checkpoint state 派生（factory_alias_overrides 或
    current_factory_data 的 fuzzy/contains 命中），绝不从请求体读取；
    派生出的 folder 再经 factory_match.validate_subfolder 校验（必须是
    upstream_root 下现存一级子目录）。

    响应：
    - 200: {"ok": true, "alias": 工厂名, "short_name": 文件夹名, "overwritten": [...]}
    - 400: 无对照建议可保存 / folder 校验失败
    - 404: 批次不存在
    - 409: 工厂已有不一致的中文短名（或文件夹已被其他工厂占用为短名）
    """
    return await asyncio.to_thread(_save_alias_impl, thread_id, request.factory)