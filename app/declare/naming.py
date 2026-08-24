# -*- coding: utf-8 -*-
"""报关单命名规则——港口映射、票名、文件名、开船日期格式化。"""

from __future__ import annotations

PORT_MAP = {
    "東京港":   {"cn": "东京",   "en": "TOKYO",   "inv": "T"},
    "名古屋港": {"cn": "名古屋", "en": "NAGOYA",  "inv": "N"},
    "横浜港":   {"cn": "横滨",   "en": "YOKOHAMA", "inv": "Y"},
    "神戸港":   {"cn": "神户",   "en": "KOBE",    "inv": "K"},
    "博多港":   {"cn": "博多",   "en": "HAKATA",  "inv": "H"},
}


def _port_info(port: str) -> dict:
    if port not in PORT_MAP:
        raise ValueError(f"未知港口：{port!r}（PORT_MAP 仅支持 {sorted(PORT_MAP)}）")
    return PORT_MAP[port]


def ticket_letter(index: int) -> str:
    """票序号 → 字母：0->'A', 1->'B', ... 25->'Z'，超 26 报错。"""
    if not 0 <= index < 26:
        raise ValueError(f"票序号 {index} 超出 A-Z 范围（0-25）")
    return chr(ord("A") + index)


def ticket_title(port: str, index: int) -> str:
    """票标题，如 ticket_title('東京港', 0) -> '东京A票'。"""
    return f"{_port_info(port)['cn']}{ticket_letter(index)}票"


def declaration_filename(port: str, index: int) -> str:
    """报关单文件名，如 declaration_filename('神戸港', 1) -> '报关神户B.xlsx'。"""
    return f"报关{_port_info(port)['cn']}{ticket_letter(index)}.xlsx"


def format_onboard(etd: str) -> str:
    """ETD '20260725' -> '2026.7.25'（月日不补零）。"""
    digits = "".join(ch for ch in str(etd) if ch.isdigit())
    if len(digits) != 8:
        raise ValueError(f"ETD 格式应为 8 位数字（yyyymmdd），实际：{etd!r}")
    year = digits[:4]
    month = int(digits[4:6])
    day = int(digits[6:8])
    return f"{year}.{month}.{day}"
