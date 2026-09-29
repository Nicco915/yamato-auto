# -*- coding: utf-8 -*-
"""F1 XD 透视导出（app.export.pivot.generate_pivot）单元测试。

覆盖：
- containers 目录最新 *_filled.xlsx 数据源；
- 港口→管理号分组（首现顺序）、件数求和、港口小计、总计；
- KANRI_NO 为空的行被忽略；
- 同一管理号多个不同 M3 → 取第一个 + 中文警告；
- 港口名只在每组第一行显示；
- 批次登记原件回退、批次不存在（ValueError）、装箱单不存在（FileNotFoundError）。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_export_pivot.py -v

隔离（血泪红线）：import app 模块前设 YAMATO_DOTENV_PATH 指临时空 .env；
autouse fixture 把 OUTPUT_DIR 指向 tmp_path 并清 settings 缓存，
绝不写真实 app/output。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP_ENV = Path(tempfile.mkdtemp(prefix="yamato_pivot_test_env_")) / ".env"
_TMP_ENV.write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP_ENV)

from openpyxl import Workbook, load_workbook  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.export import pivot  # noqa: E402

BATCH = "测试批次A"


# ---- 隔离门②：autouse 把 OUTPUT_DIR 钉到 tmp_path（settings 单例需清缓存）----
@pytest.fixture(autouse=True)
def _isolate_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    get_settings.cache_clear()
    s = get_settings()
    # 守卫：宁可 FAIL 也不碰真实 app/output
    assert str(s.output_dir_abs.resolve()).startswith(str(tmp_path.resolve()))
    yield tmp_path
    get_settings.cache_clear()


def _containers_dir() -> Path:
    d = get_settings().batch_containers_dir(BATCH)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_source_xlsx(path: Path) -> None:
    """构造临时装箱单：2 港口、3 管理号；XC001 两行（M3 不一致触发警告）、
    一行 KANRI_NO 为空被忽略。表头前置一行说明，验证按表头文本定位列。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["装箱单（测试）", None, None, None, None, None])
    ws.append(["NO", "MINATO_MEI_KJ", "KANRI_NO", "CONTAINER_MEI", "M3",
               "SOTOBAKO_D_HACCHU_SU"])
    ws.append([1, "博多港", "XC001", "20F", 10.0, 100])
    ws.append([2, "博多港", "XC001", "20F", 10.5, 50])   # M3 不一致 → 警告
    ws.append([3, "東京港", "XC100", "40F", 50.0, 200])
    ws.append([4, "東京港", None, "40F", 55.0, 999])     # 空管理号 → 忽略
    ws.append([5, "東京港", "XC101", "40F H/C", 56.0, 300])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _read_rows(path: Path) -> list[list]:
    wb = load_workbook(path)
    try:
        ws = wb.worksheets[0]
        return [[ws.cell(r, c).value for c in range(1, 6)]
                for r in range(1, ws.max_row + 1)]
    finally:
        wb.close()


def test_generate_pivot_from_filled_xlsx(_isolate_output):
    src = _containers_dir() / "ContentsOfTheContainer_filled.xlsx"
    _write_source_xlsx(src)

    result = pivot.generate_pivot(BATCH)

    out_path = Path(result["file_path"])
    assert out_path.is_file()
    assert out_path.name == f"XD透视_{BATCH}.xlsx"
    assert out_path.parent == get_settings().batch_pivot_dir(BATCH)

    # M3 不一致 → 中文警告，且取第一个值
    assert len(result["warnings"]) == 1
    assert "XC001" in result["warnings"][0]
    assert "M3" in result["warnings"][0]

    rows = _read_rows(out_path)
    assert rows[0] == ["港口", "管理号", "柜型", "M3", "汇总"]
    assert rows[1] == ["博多港", "XC001", "20F", 10.0, 150]   # 件数求和，M3 取第一个
    assert rows[2] == ["博多港 汇总", None, None, None, 150]
    assert rows[3] == ["東京港", "XC100", "40F", 50.0, 200]
    assert rows[4] == [None, "XC101", "40F H/C", 56.0, 300]  # 港口名后续行留空
    assert rows[5] == ["東京港 汇总", None, None, None, 500]  # 空管理号行的 999 被忽略
    assert rows[6] == ["总计", None, None, None, 650]
    assert len(rows) == 7


def test_overwrite_same_name_on_repeat(_isolate_output):
    src = _containers_dir() / "a_filled.xlsx"
    _write_source_xlsx(src)
    first = pivot.generate_pivot(BATCH)
    second = pivot.generate_pivot(BATCH)
    assert first["file_path"] == second["file_path"]
    assert Path(second["file_path"]).is_file()


def test_fallback_to_batch_downstream_file(_isolate_output, tmp_path, monkeypatch):
    # containers 目录为空 → 回退批次登记的下游原件
    src = tmp_path / "original.xlsx"
    _write_source_xlsx(src)
    monkeypatch.setattr(
        pivot, "get_batch",
        lambda bid: {"thread_id": bid, "downstream_file_path": str(src)},
    )
    result = pivot.generate_pivot(BATCH)
    rows = _read_rows(Path(result["file_path"]))
    assert rows[6] == ["总计", None, None, None, 650]


def test_batch_not_exists_raises(_isolate_output, monkeypatch):
    monkeypatch.setattr(pivot, "get_batch", lambda bid: None)
    with pytest.raises(ValueError, match="批次不存在"):
        pivot.generate_pivot(BATCH)


def test_no_source_raises_file_not_found(_isolate_output, monkeypatch):
    monkeypatch.setattr(
        pivot, "get_batch",
        lambda bid: {"thread_id": bid,
                     "downstream_file_path": "/nonexistent/xxx.xlsx"},
    )
    with pytest.raises(FileNotFoundError, match="装箱单不存在"):
        pivot.generate_pivot(BATCH)


def test_picks_latest_filled_by_mtime(_isolate_output, monkeypatch):
    # 两个 filled 文件：旧文件数据全为空管理号，新文件为正常数据；应取 mtime 最新者
    older = _containers_dir() / "old_filled.xlsx"
    _write_source_xlsx(older)
    newer = _containers_dir() / "new_filled.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["MINATO_MEI_KJ", "KANRI_NO", "CONTAINER_MEI", "M3",
               "SOTOBAKO_D_HACCHU_SU"])
    ws.append(["名古屋港", "XC999", "20F", 20.0, 7])
    wb.save(newer)
    os.utime(older, (1000000000, 1000000000))
    os.utime(newer, (1000000100, 1000000100))

    result = pivot.generate_pivot(BATCH)
    rows = _read_rows(Path(result["file_path"]))
    assert rows[1] == ["名古屋港", "XC999", "20F", 20.0, 7]
    assert rows[2] == ["名古屋港 汇总", None, None, None, 7]
    assert rows[3] == ["总计", None, None, None, 7]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
