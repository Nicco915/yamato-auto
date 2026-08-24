# -*- coding: utf-8 -*-
"""报关单命名规则——港口映射、票名、文件名、开船日期格式化。

港口映射为「DB 权威源 + 硬编码兜底」：
- ports 表（主数据维护 → 港口）优先，首次访问时若表空自动把硬编码
  PORT_MAP 五港种子入库（幂等），之后 DB 为权威源；
- DB 查询异常时回退硬编码 PORT_MAP，绝不阻塞生成主流程；
- 两者都没有 → 报「未知港口」，错误消息附主数据维护登记引导。

解析不加缓存：生成是低频操作，每票查一次 DB，
主数据维护页保存后立即生效（不留"改了不重启不生效"的坑）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 硬编码兜底 + 种子来源（2026-08-25 博多港踩坑后，新港口一律在
# 主数据维护 → 港口 登记，不再改代码）
PORT_MAP = {
    "東京港":   {"cn": "东京",   "en": "TOKYO",   "inv": "T"},
    "名古屋港": {"cn": "名古屋", "en": "NAGOYA",  "inv": "N"},
    "横浜港":   {"cn": "横滨",   "en": "YOKOHAMA", "inv": "Y"},
    "神戸港":   {"cn": "神户",   "en": "KOBE",    "inv": "K"},
    "博多港":   {"cn": "博多",   "en": "HAKATA",  "inv": "H"},
}

# 未知港口错误消息里的登记引导（naming 与 API 共用同一措辞）
UNKNOWN_PORT_HINT = "请到 主数据维护 → 港口 登记后再生成"


def ensure_ports_seeded() -> None:
    """首次访问时把硬编码 PORT_MAP 写入 ports 表（仅当表空），幂等。

    写入失败仅记日志——硬编码兜底仍能保证既有五港正常生成，
    DB 不可用绝不阻塞主流程。
    """
    from app.db.models import Port
    from app.db.session import get_session

    try:
        with get_session() as sess:
            if sess.query(Port).count() > 0:
                return
            for jp, info in PORT_MAP.items():
                sess.add(Port(
                    port_jp=jp,
                    name_cn=info["cn"],
                    name_en=info["en"],
                    inv_letter=info["inv"],
                ))
            sess.commit()
        logger.info("ports 表为空，已种子写入 %d 个硬编码港口", len(PORT_MAP))
    except Exception as e:  # DB 不可用绝不阻塞主流程
        logger.warning("ports 种子写入失败（使用硬编码兜底）: %s", e)


def get_port_info(port: str) -> dict:
    """解析港口 → {"cn", "en", "inv"}。DB 优先 → 硬编码 PORT_MAP 兜底。

    DB 有该行即以 DB 为准（硬编码港在 DB 被改名后按 DB 生成）；
    DB 查不到且硬编码也没有时报「未知港口」，消息附主数据维护登记引导。
    """
    from app.db.models import Port
    from app.db.session import get_session

    try:
        ensure_ports_seeded()
        with get_session() as sess:
            row = sess.query(Port).filter(Port.port_jp == port).first()
            if row is not None:
                return {"cn": row.name_cn, "en": row.name_en, "inv": row.inv_letter}
    except Exception as e:  # DB 查询异常回退硬编码，绝不阻塞生成
        logger.warning("ports 表查询失败,回退硬编码 PORT_MAP: %s", e)
    if port in PORT_MAP:
        return PORT_MAP[port]
    raise ValueError(f"未知港口：{port!r}——{UNKNOWN_PORT_HINT}")


def ticket_letter(index: int) -> str:
    """票序号 → 字母：0->'A', 1->'B', ... 25->'Z'，超 26 报错。"""
    if not 0 <= index < 26:
        raise ValueError(f"票序号 {index} 超出 A-Z 范围（0-25）")
    return chr(ord("A") + index)


def ticket_title(port: str, index: int) -> str:
    """票标题，如 ticket_title('東京港', 0) -> '东京A票'。"""
    return f"{get_port_info(port)['cn']}{ticket_letter(index)}票"


def declaration_filename(port: str, index: int) -> str:
    """报关单文件名，如 declaration_filename('神戸港', 1) -> '报关神户B.xlsx'。"""
    return f"报关{get_port_info(port)['cn']}{ticket_letter(index)}.xlsx"


def format_onboard(etd: str) -> str:
    """ETD '20260725' -> '2026.7.25'（月日不补零）。"""
    digits = "".join(ch for ch in str(etd) if ch.isdigit())
    if len(digits) != 8:
        raise ValueError(f"ETD 格式应为 8 位数字（yyyymmdd），实际：{etd!r}")
    year = digits[:4]
    month = int(digits[4:6])
    day = int(digits[6:8])
    return f"{year}.{month}.{day}"
