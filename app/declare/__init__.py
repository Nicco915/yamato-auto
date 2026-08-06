# -*- coding: utf-8 -*-
"""报关单生成器——纯 Python 计算，零 LLM / 零 DB / 零 FastAPI 依赖。"""

from app.declare.aggregator import (
    AggregateResult,
    DetailRow,
    aggregate_ticket,
    rows_for_ticket,
)
from app.declare.mapping import build_mapping_index, lookup
from app.declare.naming import (
    PORT_MAP,
    declaration_filename,
    format_onboard,
    ticket_letter,
    ticket_title,
)
from app.declare.template_filler import fill_declaration

__all__ = [
    "AggregateResult",
    "DetailRow",
    "PORT_MAP",
    "aggregate_ticket",
    "build_mapping_index",
    "declaration_filename",
    "fill_declaration",
    "format_onboard",
    "lookup",
    "rows_for_ticket",
    "ticket_letter",
    "ticket_title",
]
