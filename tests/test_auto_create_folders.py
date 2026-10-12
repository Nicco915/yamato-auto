# -*- coding: utf-8 -*-
"""建批/补厂自动创建缺资料工厂文件夹测试（2026-10-12 用户二次确认，调整 B，
实施计划「七、补充设计」）。

覆盖：
1. create_batch：prescan 未匹配工厂按标准别名口径自动建夹
   （主数据 short_name 优先，无别名回退装箱单原名；与看板预建同一名称）；
2. 幂等：文件夹已存在重复建批/重复建夹不报错；
3. mkdir 失败（目标路径被同名文件占用）只 warning 不阻断建批；
4. add_factories_to_batch：新增入队的未匹配工厂一并自动建夹
   （真实跑图到 awaiting 后补厂，参照 add_factories_test.py 模式）；
5. canonical_folder_name 命名单测：非法字符安全化、short_name 优先、
   无别名回退原名。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_auto_create_folders.py -v

隔离红线（2026-08-11 事故教训，骨架照抄 test_board_error_recovery.py）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_autocreate_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.db import batch_store  # noqa: E402
from app.db.models import Factory  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.nodes import extraction_node as en  # noqa: E402
from app.orchestrator import factory_setup  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_autocreate_test_")

HEADER = ["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"]

# 厂名刻意取零相似度（rapidfuzz ratio≈0），防未匹配厂被 fuzzy 捞成 candidates；
# 且全量混跑时与其他测试文件（add_factories_test 的 东海制果/西陆金属工业）
# 不重名——e2e 跑图会自动建 Factory 主数据行，重名会撞唯一约束
F_OK = "北岭重工集团"
F_GAP = "南岛食品工业"
F_GAP_SHORT = "南岛食品"  # 主数据 short_name → 标准文件夹名


def _write_manifest(path: Path, rows: list[tuple[str, str, str, int]]) -> str:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for factory, sku, name, qty in rows:
        ws.append([factory, sku, name, qty])
    wb.save(path)
    return str(path)


@pytest.fixture(autouse=True)
def _cleanup():
    """共享临时库：每个用例后清掉本文件造的 Factory 行与批次行，
    防串味（short_name 查询按 factory_name 唯一命中）。"""
    yield
    with get_session() as s:
        for name in (F_GAP, "单测厂甲", "单测厂乙"):
            row = s.query(Factory).filter(Factory.factory_name == name).first()
            if row:
                s.delete(row)
        s.commit()
    for tid in ("AC-BATCH-1", "AC-BATCH-2", "AC-BATCH-3", "AC-BATCH-4",
                "AC-ADDF-1"):
        batch_store.delete_batch(tid)


def _add_factory_master(factory_name: str, short_name: str | None) -> None:
    """插入/更新 Factory 主数据行（upsert 容错：全量混跑时 e2e 跑图可能
    已自动建过同名行，撞唯一约束则改更新 short_name）。"""
    with get_session() as s:
        row = s.query(Factory).filter(Factory.factory_name == factory_name).first()
        if row:
            row.short_name = short_name
        else:
            s.add(Factory(factory_name=factory_name, short_name=short_name))
        s.commit()


# ---------------------------------------------------------------------------
# 1. create_batch：prescan 未匹配工厂自动建夹（标准别名口径）
# ---------------------------------------------------------------------------

def test_create_batch_auto_creates_unmatched_folders(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    (upstream / F_OK).mkdir(parents=True)
    (upstream / F_OK / "pl.pdf").write_bytes(b"%PDF-1.4 fake")
    manifest = _write_manifest(tmp_path / "m1.xlsx", [
        (F_OK, "4900000000701", "ITEM-A", 10),
        (F_GAP, "4900000000702", "ITEM-B", 20),
    ])
    # 主数据 short_name：自动建夹必须用短名（与看板预建同口径）
    _add_factory_master(F_GAP, F_GAP_SHORT)

    # 不真跑图：建夹逻辑在 run_until_interrupt 之前，mock 掉跑图即可
    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: {"status": "pending_human_review"})
    r = service.create_batch("AC-BATCH-1", manifest, str(upstream))
    assert r["status"] == "pending_human_review"

    # 未匹配厂按 short_name 建夹；已匹配厂文件夹原样不动
    assert (upstream / F_GAP_SHORT).is_dir()
    assert (upstream / F_OK).is_dir()
    assert not (upstream / F_GAP).exists()


def test_create_batch_auto_create_fallback_plain_name(tmp_path, monkeypatch):
    """无主数据别名时回退装箱单原名（安全化后）建夹。"""
    upstream = tmp_path / "upstream"
    (upstream / F_OK).mkdir(parents=True)
    manifest = _write_manifest(tmp_path / "m2.xlsx", [
        (F_OK, "4900000000701", "ITEM-A", 10),
        (F_GAP, "4900000000702", "ITEM-B", 20),
    ])
    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: {"status": "pending_human_review"})
    service.create_batch("AC-BATCH-2", manifest, str(upstream))
    assert (upstream / F_GAP).is_dir()


# ---------------------------------------------------------------------------
# 2. 幂等：重复建批/重复建夹不报错
# ---------------------------------------------------------------------------

def test_auto_create_idempotent(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    (upstream / F_OK).mkdir(parents=True)
    manifest = _write_manifest(tmp_path / "m3.xlsx", [
        (F_OK, "4900000000701", "ITEM-A", 10),
        (F_GAP, "4900000000702", "ITEM-B", 20),
    ])
    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: {"status": "pending_human_review"})
    # 文件夹已被预建（如看板手动预建）：prescan 直接 resolved，不重复建
    (upstream / F_GAP).mkdir()
    service.create_batch("AC-BATCH-3", manifest, str(upstream))
    assert (upstream / F_GAP).is_dir()

    # helper 本身幂等：重复调用不报错
    p1 = factory_setup.ensure_factory_folder(upstream, F_GAP)
    p2 = factory_setup.ensure_factory_folder(upstream, F_GAP)
    assert p1 == p2 == upstream / F_GAP


# ---------------------------------------------------------------------------
# 3. mkdir 失败只 warning 不阻断建批
# ---------------------------------------------------------------------------

def test_mkdir_failure_never_blocks_create(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    (upstream / F_OK).mkdir(parents=True)
    manifest = _write_manifest(tmp_path / "m4.xlsx", [
        (F_OK, "4900000000701", "ITEM-A", 10),
        (F_GAP, "4900000000702", "ITEM-B", 20),
    ])
    # 目标路径被同名「文件」占用 → mkdir(exist_ok=True) 抛 FileExistsError
    (upstream / F_GAP).write_text("占位文件", encoding="utf-8")

    monkeypatch.setattr(service, "run_until_interrupt",
                        lambda *a, **kw: {"status": "pending_human_review"})
    r = service.create_batch("AC-BATCH-4", manifest, str(upstream))  # 不抛
    assert r["status"] == "pending_human_review"
    assert batch_store.get_batch("AC-BATCH-4") is not None
    assert (upstream / F_GAP).is_file()  # 占位文件原样，未被覆盖


# ---------------------------------------------------------------------------
# 4. add_factories_to_batch：新增入队的未匹配工厂一并自动建夹（真实跑图）
# ---------------------------------------------------------------------------

def _force_mock_extraction(monkeypatch) -> None:
    """全量 pytest 下强制回 mock 提取线（与 add_factories_test 同模式）。"""
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")


def _write_batch_config(thread: str, xlsx: str, upstream: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    service._write_batch_config(thread, {
        "thread_id": thread,
        "downstream_file_path": xlsx,
        "upstream_root": upstream,
        "created_at": now,
        "last_run_at": now,
        "run_count": 1,
    })


def test_add_factories_auto_creates_folder(monkeypatch, tmp_path):
    _force_mock_extraction(monkeypatch)
    THREAD = "AC-ADDF-1"
    root = tmp_path / "upstream_addf"
    (root / F_OK).mkdir(parents=True)
    (root / F_OK / "pl.pdf").write_bytes(b"%PDF-1.4 fake")

    xlsx = _write_manifest(tmp_path / "addf_ac.xlsx", [
        (F_OK, "4900000000701", "ITEM-A", 10),
        (F_GAP, "4900000000702", "ITEM-B", 20),
    ])
    _write_batch_config(THREAD, xlsx, str(root))

    # 首遍：A 挂审核，approve 后 B 缺资料自动跳过 → completed/awaiting
    r = service.run_until_interrupt(THREAD, downstream_file_path=xlsx,
                                    upstream_root=str(root))
    assert r["review_data"]["factory_name"] == F_OK
    r = service.resume_order(THREAD, {"approved": True, "items": []})
    assert r["status"] == "success", r
    assert not (root / F_GAP).exists()  # 建批走的是 run_until_interrupt，不建夹

    # 补充工厂：B 入队且未匹配文件夹 → 自动建空夹；二遍空夹 no_documents
    # 仍自动跳过（不挂人工），批次回 awaiting
    r = service.add_factories_to_batch(THREAD)
    assert r["added"] == 1 and r["factories"] == [F_GAP], r
    assert r["status"] == "completed", r
    assert (root / F_GAP).is_dir()  # 自动建夹生效（标准名=原名，无别名）

    # 幂等：再次补充（B 仍未写入）不报错；文件夹已存在 → 命中不重建
    r = service.add_factories_to_batch(THREAD)
    assert r["added"] == 1 and r["factories"] == [F_GAP], r
    assert (root / F_GAP).is_dir()


# ---------------------------------------------------------------------------
# 5. canonical_folder_name 命名单测（共享 helper 口径）
# ---------------------------------------------------------------------------

def test_canonical_folder_name_rules():
    # 非法字符安全化
    assert factory_setup.canonical_folder_name('甲/乙:厂') == "甲_乙_厂"
    # 无别名回退原名
    assert factory_setup.canonical_folder_name("单测厂甲") == "单测厂甲"
    # 主数据 short_name 优先
    _add_factory_master("单测厂乙", "乙短名")
    assert factory_setup.canonical_folder_name("单测厂乙") == "乙短名"


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
