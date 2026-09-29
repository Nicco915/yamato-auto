# -*- coding: utf-8 -*-
"""调度 Agent 导出工具测试：export_pivot / export_split_stats。

覆盖：
- TOOLS 注册：risk="write"、preview/execute 成对、phase=1 不可见、
  phase=2 可见；validate_args 缺 batch_id 报错、幻觉参数剔除；
- preview 只出说明不执行（不创建 pivot/stats 目录、不生成文件）；
  未知批次/无装箱单/无分票提案 → 中文 warning 引导；
- exec 成功路径：真实调 app.export 服务（隔离 output 下造 filled Excel +
  Declaration），返回中文自然语言摘要（含 pivot/stats 目录相对说法、
  警告逐条列出），摘要不含绝对路径；
- exec 错误路径：批次不存在 / 无装箱单 / 无分票提案 → 中文引导 error，
  而非 traceback。

运行方式（在 app/ 目录下）：
    PYTHONPATH=. python3 -m pytest tests/dispatcher_export_tools_test.py -v

隔离（血泪红线 2026-08-11）：import app 模块前设 YAMATO_DOTENV_PATH 指向
临时空 .env；import 全部 app 模块后 isolate_to_tmp，output/DB 全部落在
临时目录，绝不碰真实 app/output 与 master.db。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ---- env 前置（需在 import app 之前，血泪红线）----
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"
_TMP_ENV = Path(tempfile.mkdtemp(prefix="yamato_disp_export_env_")) / ".env"
_TMP_ENV.write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP_ENV)

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

import pytest  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.db.models import Declaration  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.dispatcher import tools as dispatcher_tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_dispatcher_export_tools_")

PIVOT_BATCH = "EXP-PIVOT-1"
STATS_BATCH = "EXP-STATS-1"

# filled Excel 全列（stats 服务按表头取数，需与 split.loader 口径一致）
COLUMNS = [
    "KANRI_NO", "MINATO_MEI_KJ", "CONTAINER_MEI", "MAKER_MEI_KJ",
    "SHOHIN_CD", "净重", "毛重", "SOTOBAKO_D_HACCHU_SU",
    "中文品名", "D_HACCHU_SU", "KAKAKUKEI", "TSUKA_MEI", "M3",
    "PURCHASE_ORDER", "SOTOBAKO_HABA", "SOTOBAKO_OKUYUKI", "SOTOBAKO_TAKASA",
]


def _row(kanri: str, port: str, *, pcs: int = 10, m3: float = 1.0,
         ctype: str = "40HQ") -> dict:
    return {
        "KANRI_NO": kanri, "MINATO_MEI_KJ": port, "CONTAINER_MEI": ctype,
        "MAKER_MEI_KJ": "测试厂", "SHOHIN_CD": "4900000000001",
        "净重": 5.5, "毛重": 6.6, "SOTOBAKO_D_HACCHU_SU": pcs,
        "中文品名": "测试品", "D_HACCHU_SU": 100, "KAKAKUKEI": 100.0,
        "TSUKA_MEI": "USD", "M3": m3, "PURCHASE_ORDER": "PO-001",
        "SOTOBAKO_HABA": 50, "SOTOBAKO_OKUYUKI": 40, "SOTOBAKO_TAKASA": 30,
    }


def _make_filled(batch_id: str, rows: list[dict]) -> Path:
    """在隔离 output 下造 containers/{batch}_filled.xlsx。"""
    containers_dir = get_settings().batch_containers_dir(batch_id)
    containers_dir.mkdir(parents=True, exist_ok=True)
    path = containers_dir / f"{batch_id}_filled.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(COLUMNS)
    for r in rows:
        ws.append([r.get(c) for c in COLUMNS])
    wb.save(path)
    return path


def _add_decl(split_id: str, ticket_no: str, port: str, kanri: str,
              *, status: str = "pending", version: int = 1) -> None:
    item = {"kanri_no": kanri, "factory_filter": None,
            "factory_exclude": None, "is_partial": False,
            "inspection_filter": None}
    with get_session() as s:
        s.add(Declaration(
            split_thread_id=split_id, ticket_no=ticket_no, port=port,
            container_type="40HQ", items=[item], sj_factories=[],
            status=status, version=version,
        ))
        s.commit()


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def test_registry_write_tools_pair():
    """两个导出工具：risk=write、preview/execute 成对注册。"""
    for name in ("export_pivot", "export_split_stats"):
        t = dispatcher_tools.TOOLS[name]
        assert t.risk == "write"
        assert t.preview is not None and t.execute is not None
        assert t.func is None
        assert t.parameters["required"] == ["batch_id"]
        assert t.parameters["properties"]["batch_id"]["type"] == "string"


def test_registry_phase_visibility():
    """phase=1 不可见（写工具确认门），phase=2 可见。"""
    phase1 = {t.name for t in dispatcher_tools.visible_tools(phase=1)}
    phase2 = {t.name for t in dispatcher_tools.visible_tools(phase=2)}
    assert "export_pivot" not in phase1
    assert "export_split_stats" not in phase1
    assert "export_pivot" in phase2
    assert "export_split_stats" in phase2


def test_validate_args():
    """缺 batch_id 报错；幻觉参数被剔除。"""
    schema = dispatcher_tools.TOOLS["export_pivot"].parameters
    _, err = dispatcher_tools.validate_args({}, schema)
    assert err and "batch_id" in err
    cleaned, err2 = dispatcher_tools.validate_args(
        {"batch_id": "B-1", "hallucinated": 1}, schema)
    assert err2 is None
    assert cleaned == {"batch_id": "B-1"}


# ---------------------------------------------------------------------------
# export_pivot
# ---------------------------------------------------------------------------

def test_export_pivot_preview_no_side_effect():
    """preview 只出中文说明：不创建 pivot 目录、不生成文件。"""
    _make_filled(PIVOT_BATCH, [_row("K-001", "东京")])
    p = dispatcher_tools._preview_export_pivot({"batch_id": PIVOT_BATCH})
    assert p["warnings"] == []
    assert PIVOT_BATCH in p["summary"]
    assert "透视" in p["summary"]
    assert any("pivot 目录" in line for line in p["lines"])
    assert any("覆盖" in line for line in p["lines"])
    assert not get_settings().batch_pivot_dir(PIVOT_BATCH).exists()


def test_export_pivot_preview_guides():
    """preview 引导：未知批次 → 批次不存在；登记批次无装箱单 → 提示先提取。"""
    p1 = dispatcher_tools._preview_export_pivot({"batch_id": "不存在批次-X"})
    assert any("批次不存在" in w for w in p1["warnings"])

    batch_store.upsert_batch("EXP-PIVOT-EMPTY", status="running")
    p2 = dispatcher_tools._preview_export_pivot({"batch_id": "EXP-PIVOT-EMPTY"})
    assert any("还没有装箱单" in w and "请先完成提取" in w
               for w in p2["warnings"])


def test_export_pivot_exec_success():
    """exec 成功：返回中文摘要（pivot 目录相对说法 + 警告逐条），文件落盘。"""
    # 同一管理号两个不同 M3 → 触发一条中文警告
    _make_filled(PIVOT_BATCH, [
        _row("K-001", "东京", pcs=10, m3=1.0),
        _row("K-001", "东京", pcs=5, m3=2.0),
        _row("K-002", "名古屋", pcs=3, m3=0.5),
    ])
    r = dispatcher_tools._exec_export_pivot({"batch_id": PIVOT_BATCH})
    assert r.get("status") == "exported", r
    msg = r["message"]
    assert PIVOT_BATCH in msg and "透视表已导出" in msg
    assert "pivot 目录" in msg
    # 警告逐条列出
    assert r["warnings"], "应携带 M3 多值警告"
    assert "1 条警告" in msg
    assert any(w in msg for w in r["warnings"])
    # 铁律：摘要不得含内部绝对路径
    assert str(get_settings().output_dir_abs) not in msg
    # 文件确实生成
    out_dir = get_settings().batch_pivot_dir(PIVOT_BATCH)
    assert list(out_dir.glob("XD透视_*.xlsx"))


def test_export_pivot_exec_errors_chinese():
    """exec 错误路径：批次不存在 / 无装箱单 → 中文引导 error，非 traceback。"""
    r1 = dispatcher_tools._exec_export_pivot({"batch_id": "不存在批次-Y"})
    assert "error" in r1
    assert "批次不存在" in r1["error"]
    assert "Traceback" not in r1["error"]

    r2 = dispatcher_tools._exec_export_pivot({"batch_id": "EXP-PIVOT-EMPTY"})
    assert "error" in r2
    assert "还没有装箱单" in r2["error"]
    assert "请先完成提取" in r2["error"]

    r3 = dispatcher_tools._exec_export_pivot({"batch_id": ""})
    assert "error" in r3 and "batch_id" in r3["error"]


# ---------------------------------------------------------------------------
# export_split_stats
# ---------------------------------------------------------------------------

def test_export_split_stats_preview_no_side_effect():
    """preview 只出说明：不创建 stats 目录；有提案时展示票数。"""
    _add_decl(f"split-{STATS_BATCH}", "T-001", "东京", "K-101")
    p = dispatcher_tools._preview_export_split_stats({"batch_id": STATS_BATCH})
    assert p["warnings"] == []
    assert STATS_BATCH in p["summary"]
    assert "统计" in p["summary"]
    assert any("stats 目录" in line for line in p["lines"])
    assert any("提案票数: 1" in line for line in p["lines"])
    assert not get_settings().batch_stats_dir(STATS_BATCH).exists()


def test_export_split_stats_preview_guides_no_proposal():
    """preview 引导：无分票提案 → 提示先生成提案。"""
    p = dispatcher_tools._preview_export_split_stats(
        {"batch_id": "无提案批次-Z"})
    assert any("还没有分票提案" in w and "请先生成分票提案" in w
               for w in p["warnings"])


def test_export_split_stats_exec_success():
    """exec 成功：内部拼 split- 前缀调服务，中文摘要 + 文件落 stats 目录。"""
    _make_filled(STATS_BATCH, [_row("K-101", "东京", pcs=10, m3=1.0)])
    r = dispatcher_tools._exec_export_split_stats({"batch_id": STATS_BATCH})
    assert r.get("status") == "exported", r
    msg = r["message"]
    assert STATS_BATCH in msg and "统计表已导出" in msg
    assert "stats 目录" in msg
    assert str(get_settings().output_dir_abs) not in msg
    out_dir = get_settings().batch_stats_dir(STATS_BATCH)
    assert list(out_dir.glob("截单信息-分体积_*.xlsx"))


def test_export_split_stats_exec_errors_chinese():
    """exec 错误路径：无提案 / 无装箱单 → 中文引导 error，非 traceback。"""
    r1 = dispatcher_tools._exec_export_split_stats({"batch_id": "无提案批次-Z"})
    assert "error" in r1
    assert "还没有分票提案" in r1["error"]
    assert "请先生成分票提案" in r1["error"]
    assert "Traceback" not in r1["error"]

    # 有提案但无装箱单
    batch_store.upsert_batch("EXP-STATS-NOXLSX", status="running")
    _add_decl("split-EXP-STATS-NOXLSX", "T-001", "东京", "K-201")
    r2 = dispatcher_tools._exec_export_split_stats(
        {"batch_id": "EXP-STATS-NOXLSX"})
    assert "error" in r2
    assert "还没有装箱单" in r2["error"]
    assert "请先完成提取" in r2["error"]
