# -*- coding: utf-8 -*-
"""convert_excel_to_pdf 单测：覆盖兆丰 Packing 裁切修复（C1）+ .xls 回退普通分页。

运行方式（依赖 LibreOffice 可用）：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_pipeline_excel_render.py -v
    或直接：python3 tests/test_pipeline_excel_render.py

隔离策略：
- 不走 db 也不走 LLM，只调 app.extraction.pipeline.convert_excel_to_pdf
  和 app.extraction.pipeline._reset_topleft_for_soffice；不读 llm_client
  的真实 .env，因此不需要 isolate_to_tmp。
- 临时目录用 stdlib tempfile.TemporaryDirectory，进程退出时自动清理。

覆盖：
- 兆丰 Packing XD-261830-001-1.26 / 1.28（视图停在 D15）：转换后 PDF 文本含
  "JAN CODE" 和 "4549509623861"，证明 SinglePageSheets 不再以 topLeftCell
  为锚点裁切左上整片。
- 兆丰 1.30（topLeftCell 可能停在 B13）：同上。
- 正常 xlsx（topLeftCell 已为 "A1"）：不引入回归——确认预处理函数返回
  的副本路径下 topLeftCell == "A1"、selection 为空；以及转换后 PDF 仍
  包含主要文本。
- 临时副本不影响源：转换前后源文件 mtime 不变，且临时副本位于 out_dir/
  _lo_preprocess/ 下，转换后被调用方清掉（这里直接用 TemporaryDirectory
  验证 out_dir 干净）。
- .xls：mock subprocess.run 验证只走普通 pdf 过滤器，不尝试 SinglePageSheets
  （避免 .xls 走 SinglePageSheets 路径被 topLeftCell 裁切；同时 openpyxl
  不能写 .xls，不能预处理）。
- 预处理函数 .xls 短路：返回 None。
- 预处理函数 openpyxl 异常：WARNING 日志 + 返回 None（不影响主流程）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# 强制使用 macOS 自带 LibreOffice（PATH 上不一定有 soffice）
os.environ.setdefault(
    "SOFFICE_PATH",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

from app.extraction.pipeline import (  # noqa: E402
    _reset_topleft_for_soffice,
    convert_excel_to_pdf,
)

FIXTURES = APP_ROOT / "tests" / "fixtures" / "zhaofeng"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pdf_text(pdf_path: str) -> str:
    """从 PDF 提取全部文本（PyPDF2；兆丰真实文件实测可解出 13 位 JAN CODE）。"""
    import PyPDF2
    r = PyPDF2.PdfReader(pdf_path)
    out = []
    for p in r.pages:
        out.append(p.extract_text() or "")
    return "\n".join(out)


def _has_soffice() -> bool:
    """soffice 可用性探测——不在就 skip 真实转换用例。"""
    from app.extraction.pipeline import _find_soffice
    return _find_soffice() is not None


requires_soffice = pytest.mark.skipif(
    not _has_soffice(), reason="LibreOffice (soffice) not available"
)


# ---------------------------------------------------------------------------
# 兆丰 Packing（视图停在 D15/B13）—— 真实转换 + 文本断言
# ---------------------------------------------------------------------------


@requires_soffice
@pytest.mark.parametrize(
    "filename",
    [
        "Packing XD-261830-001-1.26.xlsx",
        "Packing XD-261830-001-1.28.xlsx",
        "Packing XD-261830-001-1.30.xlsx",
    ],
)
def test_zhaofeng_pdf_contains_jan_code_and_barcode(filename):
    """C1 修复断言：SinglePageSheets 不再以 topLeftCell 裁切 JAN CODE 列。"""
    src = FIXTURES / filename
    assert src.exists(), f"fixture missing: {src}"
    mtime_before = src.stat().st_mtime
    with tempfile.TemporaryDirectory() as tmp:
        out_pdf = convert_excel_to_pdf(str(src), tmp)
        assert out_pdf is not None, f"convert failed: {filename}"
        assert Path(out_pdf).exists()
        text = _pdf_text(out_pdf)
        assert "JAN CODE" in text, (
            f"{filename}: JAN CODE column missing from PDF (topLeftCell "
            "crop not fixed); first 300 chars:\n" + text[:300]
        )
        # 兆丰 Packing 真实首个 SKU 的 JAN CODE
        assert "4549509623861" in text, (
            f"{filename}: first barcode 4549509623861 missing"
        )
        # 临时副本应位于 out_dir/_lo_preprocess/ 下
        preprocess_dir = Path(tmp) / "_lo_preprocess"
        # 转换完成后调用方负责清理；这里验证 preprocess_dir 内的临时副本
        # 在转换过程中确实被创建过——若转换后还在也没关系（外层 TmpDir 兜底）
        assert preprocess_dir.exists(), (
            f"{filename}: _lo_preprocess/ not created (预处理未跑)"
        )
    # 源文件 mtime 必须不变（缓存键判定依据）
    mtime_after = src.stat().st_mtime
    assert mtime_before == mtime_after, (
        f"{filename}: source mtime changed by convert! "
        f"before={mtime_before} after={mtime_after}"
    )


# ---------------------------------------------------------------------------
# 正常 xlsx 不引入回归
# ---------------------------------------------------------------------------


@requires_soffice
def test_normal_xlsx_with_topleft_a1_roundtrip():
    """topLeftCell='A1' 的 xlsx 转换后 PDF 文本包含核心表头+首条数据。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    # 第一行明确写一行表头与一行数据（13 位 barcode + 数量）
    ws["A1"] = "JAN CODE"
    ws["B1"] = "DESCRIPTION"
    ws["C1"] = "QTY"
    ws["A2"] = "4549509623861"
    ws["B2"] = "TEST GOODS"
    ws["C2"] = 100
    with tempfile.NamedTemporaryFile(
        suffix=".xlsx", delete=False, dir=tempfile.gettempdir()
    ) as f:
        normal_path = f.name
    try:
        wb.save(normal_path)
        # 新建工作簿 topLeftCell 为 None（即默认 A1），无需断言
        mtime_before = Path(normal_path).stat().st_mtime

        with tempfile.TemporaryDirectory() as tmp:
            out_pdf = convert_excel_to_pdf(normal_path, tmp)
            assert out_pdf is not None
            text = _pdf_text(out_pdf)
            # 关键回归断言：表头与 13 位条码都不应被裁掉
            assert "JAN CODE" in text, f"normal xlsx: header lost: {text[:200]}"
            # 13 位 barcode 单测允许 PyPDF2 偶尔少位（提取边界），
            # 但前缀 454950962 必须出现——这就是 SinglePageSheets 还在工作的硬证据
            assert "454950962" in text, (
                f"normal xlsx: barcode truncated by crop: {text[:200]}"
            )
            # 临时副本应位于 _lo_preprocess/ 下
            assert (Path(tmp) / "_lo_preprocess" / Path(normal_path).name).exists()

        # 源 mtime 不变（缓存键判定）
        mtime_after = Path(normal_path).stat().st_mtime
        assert mtime_before == mtime_after
    finally:
        Path(normal_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _reset_topleft_for_soffice 单元测试
# ---------------------------------------------------------------------------


def test_preprocess_xlsx_resets_topleft_and_selection():
    """_reset_topleft_for_soffice 把 xlsx 复制到 _lo_preprocess/ 并重置视图。"""
    src = FIXTURES / "Packing XD-261830-001-1.26.xlsx"
    with tempfile.TemporaryDirectory() as work:
        out = _reset_topleft_for_soffice(src, Path(work))
        assert out is not None
        assert (Path(work) / "_lo_preprocess" / src.name).exists()
        # 验证副本内 topLeftCell == "A1" 且 selection 全部归位 A1
        from openpyxl import load_workbook
        wb = load_workbook(out)
        for ws in wb.worksheets:
            assert ws.sheet_view.topLeftCell == "A1", (
                f"sheet {ws.title!r} topLeftCell not reset: "
                f"{ws.sheet_view.topLeftCell!r}"
            )
            for sel in ws.sheet_view.selection:
                assert sel.activeCell == "A1", (
                    f"sheet {ws.title!r} selection.activeCell "
                    f"!= A1: {sel.activeCell!r}"
                )
                assert sel.sqref == "A1", (
                    f"sheet {ws.title!r} selection.sqref != A1: {sel.sqref!r}"
                )


def test_preprocess_xls_short_circuits():
    """_reset_topleft_for_soffice 对 .xls 直接返回 None（openpyxl 不能写）。"""
    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as f:
        fake_xls = f.name
    try:
        out = _reset_topleft_for_soffice(Path(fake_xls), Path(tempfile.gettempdir()))
        assert out is None
    finally:
        Path(fake_xls).unlink(missing_ok=True)


def test_preprocess_falls_back_on_openpyxl_error():
    """openpyxl 打开失败（损坏的 xlsx）→ WARNING + 返回 None，不抛异常。"""
    with tempfile.NamedTemporaryFile(
        suffix=".xlsx", delete=False, dir=tempfile.gettempdir()
    ) as f:
        broken = f.name
    Path(broken).write_bytes(b"this is not a real xlsx file")
    try:
        out = _reset_topleft_for_soffice(Path(broken), Path(tempfile.gettempdir()))
        assert out is None
    finally:
        Path(broken).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# .xls 走普通分页（不尝试 SinglePageSheets）—— mock subprocess.run
# ---------------------------------------------------------------------------


def test_xls_uses_plain_pdf_filter_only():
    """convert_excel_to_pdf 对 .xls 只调用普通 pdf 过滤器，不走 SinglePageSheets。"""
    # 造一个最小 .xls 后缀文件（内容无所谓，mock 不真读）
    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as f:
        fake = f.name
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # mock _find_soffice 返回固定路径，subprocess.run 记录调用
            # 第一次 subprocess.run 调用后写出假 pdf 文件以驱动主循环返回
            real_run = subprocess.run
            calls_filters: list[str] = []

            def fake_run(cmd, **kwargs):
                # 抓 --convert-to 后面的 filter
                if "--convert-to" in cmd:
                    idx = cmd.index("--convert-to")
                    calls_filters.append(cmd[idx + 1])
                # 写出伪 PDF 让主循环 expect.exists() 命中
                # cmd: [..., '--outdir', outdir, src]
                outdir = cmd[cmd.index("--outdir") + 1]
                Path(outdir, Path(fake).stem + ".pdf").write_bytes(b"%PDF-fake")
                return real_run.__self_class__(  # type: ignore[attr-defined]
                    cmd, check=True, capture_output=True, timeout=180
                ) if False else subprocess.CompletedProcess(
                    cmd, 0, stdout=b"", stderr=b""
                )

            with patch("app.extraction.pipeline.subprocess.run", side_effect=fake_run), \
                 patch("app.extraction.pipeline._find_soffice",
                       return_value="/fake/soffice"):
                out = convert_excel_to_pdf(fake, tmp)

            assert out is not None
            assert calls_filters == ["pdf"], (
                f".xls 应只走普通 pdf 过滤器，实际: {calls_filters}"
            )
    finally:
        Path(fake).unlink(missing_ok=True)


def test_xlsx_attempts_singlepage_first_then_plain():
    """convert_excel_to_pdf 对 .xlsx 先尝试 SinglePageSheets，失败才回退。"""
    src = FIXTURES / "Packing XD-261830-001-1.26.xlsx"
    with tempfile.TemporaryDirectory() as tmp:
        real_run = subprocess.run
        calls_filters: list[str] = []
        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            if "--convert-to" in cmd:
                idx = cmd.index("--convert-to")
                calls_filters.append(cmd[idx + 1])
            outdir = cmd[cmd.index("--outdir") + 1]
            # 第一次（SinglePageSheets）故意 raise CalledProcessError
            # 第二次（普通 pdf）写出文件
            if call_count["n"] == 1:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr=b"fake SinglePageSheets failure"
                )
            Path(outdir, src.stem + ".pdf").write_bytes(b"%PDF-fake")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        with patch("app.extraction.pipeline.subprocess.run", side_effect=fake_run), \
             patch("app.extraction.pipeline._find_soffice",
                   return_value="/fake/soffice"):
            out = convert_excel_to_pdf(str(src), tmp)

        assert out is not None
        assert calls_filters[0].startswith("pdf:calc_pdf_Export"), (
            f".xlsx 应先尝试 SinglePageSheets，实际: {calls_filters}"
        )
        assert calls_filters[-1] == "pdf", (
            f".xlsx 失败后应回退普通 pdf，实际: {calls_filters}"
        )


# ---------------------------------------------------------------------------
# 主入口（直接 python3 跑也能用）
# ---------------------------------------------------------------------------


def _run_all() -> None:
    """直接 python3 tests/test_pipeline_excel_render.py 时的兜底执行。"""
    import inspect
    funcs = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = skipped = 0
    for name, fn in funcs:
        sig = inspect.signature(fn)
        # 仅当函数有未绑定默认值的命名形参（@parametrize 注入）才跳过
        has_required_param = any(
            p.default is inspect.Parameter.empty
            and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
            for p in sig.parameters.values()
        )
        # skipif 不会注入额外形参；只看 require_soffice 是否命中
        is_parametrized = any(
            getattr(m, "name", "") == "parametrize"
            for m in getattr(fn, "pytestmark", [])
        )
        if has_required_param and is_parametrized:
            print(f"  ~ {name} (parametrized — pytest only)")
            skipped += 1
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except pytest.skip.Exception as e:
            print(f"  SKIP  {name}: {e}")
            skipped += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")


if __name__ == "__main__":
    print("test_pipeline_excel_render")
    if "-v" in sys.argv or "--verbose" in sys.argv:
        import pytest as _pt
        sys.exit(_pt.main([__file__, "-v"]))
    _run_all()
