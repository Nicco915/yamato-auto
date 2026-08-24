# -*- coding: utf-8 -*-
"""港口主数据化测试（/mappings 港口 Tab + DB 权威源解析）。

覆盖：
- 种子：ports 表空时硬编码 5 港自动入库，幂等（再调不重复）
- get_port_info：DB 新港命中（大阪港 O/OSAKA）；硬编码港在 DB 被改名后以 DB 为准；
  未知港口报错且消息含「主数据维护」引导
- API：新增/编辑/删除/列表；inv_letter 冲突 409（提示占用方）；
  name_en 小写输入被归一化大写；缺字段 422；空串字段 400；非法字母 400
- 删除已登记港口后 get_port_info 回退硬编码 / 报错正确
- declare 侧：service.generate_declarations 走 get_port_info（不再直接引用 PORT_MAP），
  发票号字母取自 DB 港口

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/port_master_data_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import app  # noqa: E402
from app.db.models import Port  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.declare import service as declare_service  # noqa: E402
from app.declare.naming import (  # noqa: E402
    PORT_MAP,
    declaration_filename,
    ensure_ports_seeded,
    get_port_info,
    ticket_title,
)

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_port_test_")

client = TestClient(app)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _clear_ports() -> None:
    with get_session() as s:
        s.query(Port).delete()
        s.commit()


def _reset_ports() -> None:
    """清空 + 重新种子 5 港，回到干净的初始状态。"""
    _clear_ports()
    ensure_ports_seeded()


def _all_ports() -> list[dict]:
    with get_session() as s:
        return [
            {
                "id": p.id,
                "port_jp": p.port_jp,
                "name_cn": p.name_cn,
                "name_en": p.name_en,
                "inv_letter": p.inv_letter,
            }
            for p in s.query(Port).order_by(Port.id).all()
        ]


def _insert_port(port_jp: str, name_cn: str, name_en: str, inv_letter: str) -> None:
    with get_session() as s:
        s.add(Port(port_jp=port_jp, name_cn=name_cn,
                   name_en=name_en, inv_letter=inv_letter))
        s.commit()


# ---------------------------------------------------------------------------
# 种子
# ---------------------------------------------------------------------------

def test_seed_on_empty_table():
    """表空时硬编码 5 港自动入库，内容与 PORT_MAP 一致。"""
    _clear_ports()
    ensure_ports_seeded()
    rows = _all_ports()
    assert len(rows) == len(PORT_MAP) == 5
    by_jp = {r["port_jp"]: r for r in rows}
    assert set(by_jp) == set(PORT_MAP)
    assert by_jp["博多港"]["name_cn"] == "博多"
    assert by_jp["博多港"]["name_en"] == "HAKATA"
    assert by_jp["博多港"]["inv_letter"] == "H"


def test_seed_idempotent():
    """表非空时不再重复种子。"""
    _reset_ports()
    ensure_ports_seeded()
    ensure_ports_seeded()
    assert len(_all_ports()) == 5


# ---------------------------------------------------------------------------
# get_port_info 解析（DB 权威源 + 硬编码兜底）
# ---------------------------------------------------------------------------

def test_get_port_info_db_new_port():
    """DB 新登记的港口（硬编码没有的 大阪港）命中 DB。"""
    _reset_ports()
    _insert_port("大阪港", "大阪", "OSAKA", "O")
    info = get_port_info("大阪港")
    assert info == {"cn": "大阪", "en": "OSAKA", "inv": "O"}
    # 票名/文件名同样走 DB
    assert ticket_title("大阪港", 0) == "大阪A票"
    assert declaration_filename("大阪港", 1) == "报关大阪B.xlsx"


def test_get_port_info_db_overrides_hardcoded():
    """硬编码港在 DB 被改名后以 DB 为准。"""
    _reset_ports()
    with get_session() as s:
        p = s.query(Port).filter(Port.port_jp == "東京港").first()
        p.name_cn = "东京都"
        p.inv_letter = "J"
        s.commit()
    info = get_port_info("東京港")
    assert info["cn"] == "东京都"
    assert info["inv"] == "J"


def test_get_port_info_unknown_port_guides_to_master_data():
    """未知港口报错，消息含「主数据维护」登记引导。"""
    _reset_ports()
    with pytest.raises(ValueError) as ei:
        get_port_info("不存在的港xyz")
    msg = str(ei.value)
    assert "未知港口" in msg
    assert "主数据维护" in msg
    with pytest.raises(ValueError) as ei2:
        ticket_title("不存在的港xyz", 0)
    assert "主数据维护" in str(ei2.value)


def test_delete_registered_port_falls_back_to_hardcoded():
    """删除已登记的种子港后 get_port_info 回退硬编码（既有五港不致崩）。"""
    _reset_ports()
    with get_session() as s:
        s.query(Port).filter(Port.port_jp == "東京港").delete()
        s.commit()
    # 表非空 → 不再种子；DB 查不到 → 硬编码兜底
    assert get_port_info("東京港") == {"cn": "东京", "en": "TOKYO", "inv": "T"}


def test_delete_db_only_port_then_unknown():
    """删除仅在 DB 登记的港口（硬编码也没有）→ 报未知港口 + 引导。"""
    _reset_ports()
    _insert_port("大阪港", "大阪", "OSAKA", "O")
    with get_session() as s:
        s.query(Port).filter(Port.port_jp == "大阪港").delete()
        s.commit()
    with pytest.raises(ValueError) as ei:
        get_port_info("大阪港")
    assert "主数据维护" in str(ei.value)


# ---------------------------------------------------------------------------
# API：/api/v1/mappings/ports CRUD
# ---------------------------------------------------------------------------

def test_api_list_seeds_five_ports():
    """首次 GET 列表自动种子 5 港，重复调用幂等。"""
    _clear_ports()
    r = client.get("/api/v1/mappings/ports")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 5
    assert {p["port_jp"] for p in rows} == set(PORT_MAP)
    r2 = client.get("/api/v1/mappings/ports")
    assert len(r2.json()) == 5


def test_api_create_port_normalizes_uppercase():
    """新增：name_en/inv_letter 小写输入被归一化大写，返回 201。"""
    _reset_ports()
    r = client.post("/api/v1/mappings/ports", json={
        "port_jp": "大阪港", "name_cn": "大阪",
        "name_en": "osaka", "inv_letter": "o",
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["name_en"] == "OSAKA"
    assert data["inv_letter"] == "O"
    # 保存后立即生效（解析无缓存）
    assert get_port_info("大阪港")["inv"] == "O"


def test_api_inv_letter_conflict_409():
    """inv_letter 撞字母 → 409，中文提示说明被哪个港口占用。"""
    _reset_ports()
    r = client.post("/api/v1/mappings/ports", json={
        "port_jp": "大阪港", "name_cn": "大阪",
        "name_en": "OSAKA", "inv_letter": "H",  # 博多港已占用 H
    })
    assert r.status_code == 409, r.text
    assert "博多港" in r.json()["detail"]


def test_api_update_port():
    """编辑：全量字段提交；改成未被占用的字母成功；撞字母 409。"""
    _reset_ports()
    _insert_port("大阪港", "大阪", "OSAKA", "O")
    pid = next(p["id"] for p in _all_ports() if p["port_jp"] == "大阪港")
    r = client.put(f"/api/v1/mappings/ports/{pid}", json={
        "port_jp": "大阪港", "name_cn": "大阪",
        "name_en": "osaka", "inv_letter": "p",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["name_en"] == "OSAKA"
    assert data["inv_letter"] == "P"
    assert get_port_info("大阪港")["inv"] == "P"
    # 编辑撞他人字母 → 409（排除自身）
    r2 = client.put(f"/api/v1/mappings/ports/{pid}", json={
        "port_jp": "大阪港", "name_cn": "大阪",
        "name_en": "OSAKA", "inv_letter": "H",
    })
    assert r2.status_code == 409, r2.text
    assert "博多港" in r2.json()["detail"]


def test_api_duplicate_port_jp_400():
    """port_jp 重复 → 400。"""
    _reset_ports()
    r = client.post("/api/v1/mappings/ports", json={
        "port_jp": "博多港", "name_cn": "博多",
        "name_en": "HAKATA", "inv_letter": "X",
    })
    assert r.status_code == 400, r.text
    assert "已存在" in r.json()["detail"]


def test_api_missing_field_422():
    """缺必填字段 → 422（pydantic 校验）。"""
    _reset_ports()
    r = client.post("/api/v1/mappings/ports", json={
        "port_jp": "大阪港", "name_cn": "大阪", "inv_letter": "O",
    })
    assert r.status_code == 422, r.text


def test_api_blank_field_400():
    """空串必填字段 → 400 中文提示。"""
    _reset_ports()
    r = client.post("/api/v1/mappings/ports", json={
        "port_jp": "大阪港", "name_cn": "  ",
        "name_en": "OSAKA", "inv_letter": "O",
    })
    assert r.status_code == 400, r.text
    assert "中文名" in r.json()["detail"]


def test_api_bad_inv_letter_400():
    """发票字母非 A-Z 单字符 → 400。"""
    _reset_ports()
    for bad in ("AB", "1", ""):
        r = client.post("/api/v1/mappings/ports", json={
            "port_jp": "大阪港", "name_cn": "大阪",
            "name_en": "OSAKA", "inv_letter": bad,
        })
        assert r.status_code == 400, f"{bad!r}: {r.text}"


def test_api_delete_port():
    """删除：204 系列之外走 200 + {"deleted": id}；再删 404。"""
    _reset_ports()
    _insert_port("大阪港", "大阪", "OSAKA", "O")
    pid = next(p["id"] for p in _all_ports() if p["port_jp"] == "大阪港")
    r = client.delete(f"/api/v1/mappings/ports/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == pid
    assert all(p["port_jp"] != "大阪港" for p in client.get("/api/v1/mappings/ports").json())
    r2 = client.delete(f"/api/v1/mappings/ports/{pid}")
    assert r2.status_code == 404, r2.text


# ---------------------------------------------------------------------------
# declare 侧：发票号字母走 DB 港口解析
# ---------------------------------------------------------------------------

def test_declare_service_uses_port_resolver():
    """service.generate_declarations 走 get_port_info（不再直接引用 PORT_MAP）。"""
    src = inspect.getsource(declare_service.generate_declarations)
    assert "get_port_info" in src
    assert "PORT_MAP" not in src


def test_invoice_letter_from_db_port():
    """发票号字母取自 DB 港口：YIL{inv_letter}{号码段}，新港口登记即可用。"""
    _reset_ports()
    _insert_port("大阪港", "大阪", "OSAKA", "O")
    info = get_port_info("大阪港")
    assert f"YIL{info['inv']}656" == "YILO656"
    # 种子的博多港（2026-08-25 踩坑港）同样经 DB 解析
    assert f"YIL{get_port_info('博多港')['inv']}656" == "YILH656"


def teardown_module():
    """还原 tmp 库为纯 5 港种子状态——本文件登记过 大阪港 等测试港口，
    不还原会污染同进程后续测试文件（如 app/tests/test_declare.py 的
    test_unknown_port 预期 大阪港 报未知港口）。"""
    _reset_ports()
