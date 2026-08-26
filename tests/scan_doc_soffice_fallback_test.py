# -*- coding: utf-8 -*-
"""scan_file 的 doc 通道 soffice 回退测试。

背景 bug（2026-08-27 亿钻 .doc 生产事故）：scan_file 的 .doc/.docx 分支只认
macOS 自带的 textutil，Windows/Linux 上扫描阶段即把 doc 判为 unsupported，
永远走不到提取段的 soffice→PDF 通道（LibreOffice 已安装也无用）。

修复：doc_to_html 抛 UnsupportedFileError 时回退 soffice 转 PDF 抽文本层。

测试方式：monkeypatch doc_to_html / pipeline._convert_doc_to_pdf /
pdf_to_text，不依赖本机是否真有 textutil/soffice（开发机是 macOS，textutil
天然存在，不打桩无法走到回退分支）。

覆盖：
1. textutil 正常时不走 soffice（回归）
2. textutil 缺席 + soffice 转换成功有文本 → channel="doc"，信号正常扫出
3. textutil 缺席 + soffice 也不可用 → 维持 unsupported，error 保留原文案
4. textutil 缺席 + soffice 转出但无文本层 → channel="doc"，非候选不报错

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/scan_doc_soffice_fallback_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

import app.extraction.pipeline as pipeline_mod  # noqa: E402
import app.extraction.target_identifier as ti  # noqa: E402
from app.extraction.excel_channel import UnsupportedFileError  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_scan_doc_fallback_test_")

DOC = TMP / "箱单.doc"
DOC.write_bytes(b"fake doc")  # 内容不经手真实解析（全部打桩），只要文件存在

TEXT_WITH_SIGNALS = "PACKING LIST 4901234567890 净重 NET WEIGHT 毛重 GROSS WEIGHT 箱数 CTNS"


def _patch_textutil_missing(monkeypatch):
    def _raise(_path, max_chars=None):
        raise UnsupportedFileError("textutil 不可用（仅 macOS 自带）: 安装 LibreOffice")
    monkeypatch.setattr(ti, "doc_to_html", _raise)


def test_textutil_ok_never_touches_soffice(monkeypatch):
    """回归：textutil 可用时完全走原路径，soffice 不被调用。"""
    monkeypatch.setattr(ti, "doc_to_html",
                        lambda _p, max_chars=None: f"<body>{TEXT_WITH_SIGNALS}</body>")

    def _forbidden(*_a, **_k):
        raise AssertionError("textutil 可用时不应调用 soffice 转换")
    monkeypatch.setattr(pipeline_mod, "_convert_doc_to_pdf", _forbidden)

    prof = ti.scan_file(str(DOC))
    assert prof.channel == "doc"
    assert prof.barcodes == {"4901234567890"}
    assert prof.has_net and prof.has_gross and prof.has_qty
    assert prof.is_candidate


def test_soffice_fallback_scans_pdf_text(monkeypatch):
    """textutil 缺席 + soffice 成功：用转出 PDF 的文本层做信号扫描。"""
    _patch_textutil_missing(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "_convert_doc_to_pdf",
                        lambda _p, _tmp: "/tmp/fake.pdf")
    monkeypatch.setattr(ti, "pdf_to_text",
                        lambda _p: (TEXT_WITH_SIGNALS, True))

    prof = ti.scan_file(str(DOC))
    assert prof.channel == "doc"
    assert prof.barcodes == {"4901234567890"}
    assert prof.has_net and prof.has_gross and prof.has_qty
    assert prof.is_candidate


def test_both_unavailable_keeps_unsupported(monkeypatch):
    """textutil/soffice 都不可用：维持原 unsupported 判定，error 文案保留安装指引。"""
    _patch_textutil_missing(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "_convert_doc_to_pdf", lambda _p, _tmp: None)

    prof = ti.scan_file(str(DOC))
    assert prof.channel == "unsupported"
    assert "textutil 不可用" in prof.error
    assert not prof.is_candidate


def test_soffice_pdf_without_text_layer(monkeypatch):
    """soffice 转出但无文本层：channel=doc、无信号、非候选，不抛异常。"""
    _patch_textutil_missing(monkeypatch)
    monkeypatch.setattr(pipeline_mod, "_convert_doc_to_pdf",
                        lambda _p, _tmp: "/tmp/fake.pdf")
    monkeypatch.setattr(ti, "pdf_to_text", lambda _p: ("", False))

    prof = ti.scan_file(str(DOC))
    assert prof.channel == "doc"
    assert not prof.barcodes
    assert not prof.is_candidate
