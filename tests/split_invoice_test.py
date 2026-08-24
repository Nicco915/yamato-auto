# -*- coding: utf-8 -*-
"""confirm 携带发票号码段 → persist_split 透传 → generate_docs 生成 的链路测试。

背景：分票页此前没有号码段输入入口，confirm 永远不带 invoice_number，
generate_docs 永远跳过——"已完成"的分票一份报关单都没生成。本测试锁定
persist_split 对 invoice_number 的透传契约（前端 confirm 时把号码段塞进
proposal.invoice_number）。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/split_invoice_test.py -v

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

from sqlalchemy import select  # noqa: E402

from app.db.models import Declaration  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.split.nodes import persist_split  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_split_invoice_test_")

TID = "split-TEST-INVOICE"


def _proposal(invoice: str | None) -> dict:
    p = {
        "status": "confirmed",
        "ports": [{
            "port": "名古屋港",
            "groups": [{
                "ticket_no": "名古屋港-01",
                "port": "名古屋港",
                "container_type": "40HQ",
                "items": [{"kanri_no": "XC001", "factory_filter": None,
                           "factory_exclude": None, "is_partial": False}],
                "sj_factories": [],
                "full_containers": 1,
                "warnings": [],
            }],
        }],
    }
    if invoice is not None:
        p["invoice_number"] = invoice
    return p


def test_persist_passes_invoice_number():
    """confirm 带号码段 → persist_split 落库 + 返回值携带 invoice_number
    （generate_docs 据此自动生成报关单）。"""
    result = persist_split({
        "split_thread_id": TID,
        "proposal": _proposal("656"),
    })
    assert result["status"] == "confirmed"
    assert result["invoice_number"] == "656"
    with get_session() as s:
        decls = s.scalars(select(Declaration).where(
            Declaration.split_thread_id == TID)).all()
    assert len(decls) == 1
    assert decls[0].ticket_no == "名古屋港-01"


def test_persist_without_invoice_number():
    """confirm 不带号码段 → 返回值不含 invoice_number（generate_docs 跳过，
    走分票页补生成）。"""
    result = persist_split({
        "split_thread_id": TID + "-NOINV",
        "proposal": _proposal(None),
    })
    assert result["status"] == "confirmed"
    assert "invoice_number" not in result
