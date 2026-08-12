# -*- coding: utf-8 -*-
"""批次2 Step2.3 端到端集成测试（mock LLM）。

覆盖：
  1. 达安验证（A2a + 13 位 SKU 进 items）：
     - 用 fixture DA26107.xls + mock _route_extract 模拟「修复后 LLM 行为」
     - 验证 session.items["4936695359672"] 存在 + coverage.extracted == 1
  2. 兆丰验证（A4 + C2 高分替换）：
     - 1.28 partial (4 SKU) → 1.30 partial (4 SKU, 同集合) → 1.26 full (14 SKU)
     - 验证：1.28 后长度 4；1.30 后仍 4（ignored_duplicate）；
            1.26 后 14（C2 高分替换全量版）；coverage.extracted == 14
  3. A4 非法 SKU 验证：
     - mock 返回 sku_code="ABC123" → 进 no_code_items，不进 items
     - needs_human_review == True + issues 含 SKU_NON_13_DIGIT
  4. A2a 端到端验证（DataFrame 触发形态 + 真实 fixture 双重验证）：
     - 单射形态 (1 主行 + 1 下方 BARCODE 行) 触发 _bind_orphan_barcode
     - 真实达安 fixture 经 legacy_xls_to_markdown 后含 BARCODE 行，
       且 A2a 已在 baseline 快照里实证（详见 tests/snapshots/baseline/daan/）

隔离门（2026-08-11 血泪红线）：
  - YAMATO_TEST_MODE=1 守卫（测试顶部）
  - YAMATO_DOTENV_PATH 指临时空 .env（防止子链 load_dotenv override 打回真实路径）
  - YAMATO_SESSIONS_DIR 指临时目录（不污染真实 data/sessions/）

运行：
  PYTHONPATH=. YAMATO_TEST_MODE=1 python3 -m pytest tests/test_extraction_e2e_daan_zhaofeng.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import pandas as pd
import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# ---------------------------------------------------------------------------
# 隔离门（2026-08-11 事故教训：任何子链 load_dotenv 都不能打回真实路径）
# ---------------------------------------------------------------------------

assert os.environ.get("YAMATO_TEST_MODE") == "1", (
    "test_extraction_e2e_daan_zhaofeng 仅用于测试，必须 YAMATO_TEST_MODE=1 才允许跑"
)


def _install_isolated_env() -> None:
    """写入临时 .env + 临时 SESSIONS_DIR（防止子链 load_dotenv 副作用）。"""
    tmp = Path(tempfile.mkdtemp(prefix="yamato_e2e_"))
    empty_env = tmp / ".env"
    empty_env.write_text("# isolated .env for test_extraction_e2e_daan_zhaofeng\n", encoding="utf-8")
    sessions_dir = tmp / "sessions"
    sessions_dir.mkdir()
    os.environ["YAMATO_TEST_MODE"] = "1"
    os.environ["YAMATO_DOTENV_PATH"] = str(empty_env)
    os.environ["YAMATO_SESSIONS_DIR"] = str(sessions_dir)


_install_isolated_env()

# import 必须在隔离门之后
from app.extraction import session as session_mod  # noqa: E402
from app.extraction.excel_channel import (  # noqa: E402
    ChannelResult,
    _bind_orphan_barcode,
    legacy_xls_to_markdown,
)
from app.extraction.schemas import ExtractedItem  # noqa: E402
from app.extraction.target_identifier import FileProfile  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures：fixture 文件 + 通用 mock 帮手
# ---------------------------------------------------------------------------

DAAN_FIXTURE = APP_ROOT / "tests" / "fixtures" / "daan" / "DA26107.xls"
ZHAOFENG_DIR = APP_ROOT / "tests" / "fixtures" / "zhaofeng"
ZHAOFENG_128 = ZHAOFENG_DIR / "Packing XD-261830-001-1.28.xlsx"
ZHAOFENG_130 = ZHAOFENG_DIR / "Packing XD-261830-001-1.30.xlsx"
ZHAOFENG_126 = ZHAOFENG_DIR / "Packing XD-261830-001-1.26.xlsx"


def _make_item(sku_code: str, qty: float = 30.0,
               net: float = 708.48, gross: float = 758.88,
               name: str = "SILICA GEL CAT LITTER 3L",
               basis: str = "total", unit: str = "KG",
               source: str = "src.xlsx") -> ExtractedItem:
    """构造一个标准的 ExtractedItem（默认值匹配达安 fixture 的真实数值）。"""
    return ExtractedItem(
        sku_name=name, sku_code=sku_code,
        total_quantity=qty, total_net_weight=net,
        total_gross_weight=gross, weight_basis=basis,
        weight_unit=unit, source_file=source,
    )


def _mock_route(items: Iterable[ExtractedItem], notes: list[str] | None = None):
    """构造一个 _route_extract mock：返回 ChannelResult(items=items)。
    notes 默认空；传值时模拟 verify_weight_basis 输出的备注（如「总」名等）。
    """
    def _route(fpath: str, **kwargs) -> ChannelResult:
        res = ChannelResult()
        res.items = list(items)
        if notes:
            res.notes = list(notes)
        return res
    return _route


@pytest.fixture
def isolated_session(monkeypatch):
    """每个测试一个新的 FactorySession，并把 _route_extract / scan_file patch 为 mock。

    默认 _route_extract 返回空 ChannelResult；测试按需覆盖。
    """
    monkeypatch.setattr(session_mod, "_route_extract",
                        _mock_route(items=[]))
    monkeypatch.setattr(session_mod, "scan_file",
                        lambda path: FileProfile(path, "xlsx", set(), True, True, True))
    return session_mod.FactorySession("test_factory")


# ---------------------------------------------------------------------------
# 1. 达安验证（A2a + 13 位 SKU 进 items）
# ---------------------------------------------------------------------------


def test_daan_13_digit_sku_lands_in_items(isolated_session, monkeypatch):
    """达安 fixture：mock LLM 反映修复后行为 → 4936695359672 进 items + coverage.extracted == 1。

    关键：sku_code 是 13 位数字（"4936695359672"）。
    """
    session = isolated_session
    fixture_path = str(DAAN_FIXTURE)

    # mock LLM 反映修复后行为：能识别 BARCODE 行 → sku_code="4936695359672"
    monkeypatch.setattr(
        session_mod, "_route_extract",
        _mock_route(items=[_make_item(sku_code="4936695359672", source=fixture_path)]),
    )
    # scan_file 必须返回带 13 位 barcode 的候选画像，否则会被路由到 non_target 分支
    monkeypatch.setattr(
        session_mod, "scan_file",
        lambda path: FileProfile(
            path, "excel", {"4936695359672", "8799999999999"},
            True, True, True,
        ),
    )

    result = session_mod.process_file(session, fixture_path)

    # SKU 进了 items
    assert "4936695359672" in session.items, (
        f"达安 13 位 SKU 应进 items，实际 keys={list(session.items)}"
    )
    # coverage.extracted == 1
    cov = session.coverage()
    assert cov["extracted"] == 1, f"coverage.extracted 应 == 1，实际 {cov}"
    # action 应是 extracted（不是 ignored_subset / deferred_negative / etc）
    assert result.action in ("extracted", "replaced_target"), (
        f"达安 fixture 应走 extracted 分支，实际 {result.action}"
    )
    # 数值正确
    item = session.items["4936695359672"]
    assert item["total_quantity"] == 30.0
    assert item["total_net_weight"] == 708.48
    assert item["total_gross_weight"] == 758.88


# ---------------------------------------------------------------------------
# 2. 兆丰验证（A4 + C2 高分替换：partial → ignored → full supersedes）
# ---------------------------------------------------------------------------


# 兆丰真实 14 SKU（与 baseline 快照一致）
ZHAOFENG_14_SKUS = [
    "4549509623861", "4549509623878", "4549509623885", "4549509623892",
    "4549509623908", "4549509623915", "4549509623922", "4549509623939",
    "4549509729358", "4549509729365", "4549509769378", "4549509769385",
    "4550596103495", "4550596103501",
]


def _mock_route_with_items_by_path(items_map: dict[str, list[ExtractedItem]]):
    """按文件路径返回不同 items 的 _route_extract mock。

    items_map: {fixture_path_str: [ExtractedItem, ...]}
    """
    def _route(fpath: str, **kwargs) -> ChannelResult:
        res = ChannelResult()
        res.items = list(items_map.get(str(fpath), items_map.get(fpath, [])))
        return res
    return _route


def _zhaofeng_profile_for(path: str, barcodes_subset_or_full: set[str]) -> FileProfile:
    """构造 scan_file 返回值：兆丰文件都是合法候选（含 13 位 barcode + 三信号）。"""
    return FileProfile(path, "xlsx", barcodes_subset_or_full, True, True, True)


def test_zhaofeng_partial_then_partial_then_full_replaces_to_14(monkeypatch):
    """兆丰端到端：1.28(4) → 1.30(4 ignored_dup) → 1.26(14 replaces) → coverage.extracted == 14。

    模拟场景：
      - 1.28 partial 返回 4 SKU，注册为 target（name_score=2 之类）；
      - 1.30 partial 同样 4 SKU（同 barcode 集合），name_score 较低 → ignored_duplicate；
      - 1.26 full 返回 14 SKU（barcode 集合超集），name_score 更高 → replaced_target，
        session.items 替换为 14 SKU（C2 高分替换全量版）。

    注意：name_score 来自路径信号。本测试用 monkeypatch 钉死 _name_score
    让结果可重现（不依赖 fixture 文件名运气）。
    """
    session = session_mod.FactorySession("zhaofeng")
    # 三个文件都视为合法候选（profile.barcodes 非空）
    monkeypatch.setattr(
        session_mod, "scan_file",
        lambda path: _zhaofeng_profile_for(path, set(ZHAOFENG_14_SKUS)),
    )
    # C2：name_score 由文件路径片段决定
    #   1.28 → 2；1.30 → 2；1.26 → 8（更高分）
    name_scores = {
        str(ZHAOFENG_128): 2,
        str(ZHAOFENG_130): 2,
        str(ZHAOFENG_126): 8,
    }
    monkeypatch.setattr(
        session_mod, "_name_score",
        lambda path: name_scores.get(path, 0),
    )
    # 三个文件的 mock items：1.28/1.30 只返 4 SKU；1.26 返 14 SKU
    partial_4 = ZHAOFENG_14_SKUS[:4]
    partial_items = [_make_item(sku_code=c, qty=10.0, net=50.0, gross=55.0,
                                name=f"PARTIAL-{c}", source="partial") for c in partial_4]
    full_items = [
        _make_item(sku_code=c, qty=20.0, net=100.0, gross=110.0,
                   name=f"FULL-{c}", source="full")
        for c in ZHAOFENG_14_SKUS
    ]
    items_map = {
        str(ZHAOFENG_128): partial_items,
        str(ZHAOFENG_130): partial_items,
        str(ZHAOFENG_126): full_items,
    }
    monkeypatch.setattr(session_mod, "_route_extract",
                        _mock_route_with_items_by_path(items_map))

    # 顺序：1.28 → 1.30 → 1.26
    r1 = session_mod.process_file(session, str(ZHAOFENG_128))
    assert r1.action in ("extracted", "replaced_target"), (
        f"1.28 应成功提取，实际 {r1.action}"
    )
    assert len(session.items) == 4, (
        f"1.28 后应有 4 SKU，实际 {len(session.items)}"
    )

    r2 = session_mod.process_file(session, str(ZHAOFENG_130))
    # 同 barcode 集合 + name_score 不更高 → ignored_duplicate
    assert r2.action == "ignored_duplicate", (
        f"1.30 应被忽略（同集合、低分），实际 {r2.action}"
    )
    assert len(session.items) == 4, (
        f"1.30 后应仍为 4 SKU（不替换），实际 {len(session.items)}"
    )

    r3 = session_mod.process_file(session, str(ZHAOFENG_126))
    # barcode 集合超集 + name_score 更高 → replaced_target（被忽略的 partial 也从 targets 移除）
    assert r3.action in ("extracted", "replaced_target"), (
        f"1.26 应取代旧目标，实际 {r3.action}"
    )
    assert len(session.items) == 14, (
        f"1.26 后应有 14 SKU（C2 高分替换），实际 {len(session.items)}"
    )

    # 校验 14 SKU 全部就位
    assert all(c in session.items for c in ZHAOFENG_14_SKUS), (
        f"14 SKU 应全在 session.items，实际 keys={list(session.items)}"
    )
    # coverage.extracted == 14
    cov = session.coverage()
    assert cov["extracted"] == 14, f"coverage.extracted 应 == 14，实际 {cov}"
    # 至少含关键 SKU（任务断言要求）
    assert "4549509623861" in session.items
    assert "4550596103495" in session.items


# ---------------------------------------------------------------------------
# 3. A4 非法 SKU 验证（非 13 位 → no_code_items + needs_human_review + issues）
# ---------------------------------------------------------------------------


def test_a4_non_13_digit_sku_routes_to_review_queue():
    """A4：mock 返回 sku_code='ABC123'（非 13 位）→ 应进 no_code_items 且 needs_human_review。

    验证：
      - session.items 不含 'ABC123'
      - session.no_code_items 含该项
      - needs_human_review == True
      - session.issues 含 SKU_NON_13_DIGIT 类型
    """
    session = session_mod.FactorySession("a4_test")
    invalid_item = _make_item(sku_code="ABC123", source="bad.xlsx")

    new_skus, updated = session_mod._merge(session, [invalid_item], "bad.xlsx")

    # 不进 items
    assert session.items == {}, (
        f"非 13 位 SKU 不应进 items，实际 {session.items}"
    )
    # new_skus/updated 都为空（未触发提取/改单分支）
    assert new_skus == [] and updated == [], (
        f"非 13 位 SKU 应被丢弃不计数，实际 new={new_skus} updated={updated}"
    )
    # no_code_items 含该项
    assert len(session.no_code_items) == 1, (
        f"非 13 位 SKU 应进 no_code_items，实际 {session.no_code_items}"
    )
    dumped = session.no_code_items[0]
    assert dumped["sku_code"] == "ABC123"
    # needs_human_review 标记
    assert dumped["needs_human_review"] is True, (
        f"非 13 位 SKU 应标 needs_human_review，实际 {dumped}"
    )
    # issues 含 SKU_NON_13_DIGIT 类型
    issue_types = [i["type"] for i in session.issues]
    assert "SKU_NON_13_DIGIT" in issue_types, (
        f"issues 应含 SKU_NON_13_DIGIT，实际 types={issue_types}"
    )


def test_a4_short_digit_sku_also_rejected():
    """A4：12 位数字 SKU 也应被拒（边界值）。"""
    session = session_mod.FactorySession("a4_short")
    short_item = _make_item(sku_code="123456789012", source="short.xlsx")
    session_mod._merge(session, [short_item], "short.xlsx")
    assert session.items == {}
    assert len(session.no_code_items) == 1
    assert session.no_code_items[0]["needs_human_review"] is True
    assert "SKU_NON_13_DIGIT" in [i["type"] for i in session.issues]


def test_a4_valid_13_digit_sku_passes():
    """A4 对照：合法 13 位数字 SKU 应正常进 items，不触发 review。"""
    session = session_mod.FactorySession("a4_valid")
    valid_item = _make_item(sku_code="1234567890123", source="ok.xlsx")
    new_skus, updated = session_mod._merge(session, [valid_item], "ok.xlsx")
    assert "1234567890123" in session.items
    assert new_skus == ["1234567890123"]
    assert updated == []
    assert session.no_code_items == []
    assert all(i["type"] != "SKU_NON_13_DIGIT" for i in session.issues)


# ---------------------------------------------------------------------------
# 4. A2a 端到端验证（DataFrame 触发形态 + 真实 fixture 实证）
# ---------------------------------------------------------------------------


def test_a2a_trigger_form_one_main_one_below_barcode_row_appends():
    """A2a 触发形态：1 主行 + 1 下方条码行 → 主行末尾追加条码数字。"""
    df = pd.DataFrame(
        [
            ["WIDGET-001", "DESCRIPTION-A", 100, 50.5, 60.0],
            ["BARCODE:4936695359672", "", "", "", ""],
        ]
    )
    out = _bind_orphan_barcode(df)
    last_col = out.shape[1] - 1
    assert out.shape == (2, 6), f"触发后应扩为 6 列，实际 {out.shape}"
    assert out.iloc[0, last_col] == "4936695359672"
    # 条码行保留不删
    assert out.iloc[1, 0] == "BARCODE:4936695359672"


def test_a2a_real_daan_fixture_contains_barcode_row():
    """A2a 真实 fixture 验证：达安 DA26107.xls 经 legacy_xls_to_markdown 后含 BARCODE 行。

    印证 A2a 在真实单据上有触发条件——证明生产链路上 BARCODE 行能进 markdown，
    后续 LLM 也能读取并关联主行。
    """
    assert DAAN_FIXTURE.exists(), f"fixture 缺失: {DAAN_FIXTURE}"
    md = legacy_xls_to_markdown(str(DAAN_FIXTURE))
    # 含 BARCODE 标签行
    assert "BARCODE:4936695359672" in md, (
        f"达安 markdown 应含 BARCODE 行（A2a 触发前提），实际前 800 字：\n{md[:800]}"
    )
    # 含纯 13 位数字（追加到主行末尾后）
    assert md.count("4936695359672") >= 1


def test_a2a_real_daan_fixture_barcode_row_is_below_main_sku_row():
    """A2a 实证：达安 markdown 中 BARCODE 行位于主 SKU 行下方（触发形态）。"""
    assert DAAN_FIXTURE.exists(), f"fixture 缺失: {DAAN_FIXTURE}"
    md = legacy_xls_to_markdown(str(DAAN_FIXTURE))
    lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    main_row_idx = None
    barcode_row_idx = None
    for i, ln in enumerate(lines):
        if "SILICA GEL CAT LITTER 3L" in ln and main_row_idx is None:
            main_row_idx = i
        if "BARCODE:4936695359672" in ln and barcode_row_idx is None:
            barcode_row_idx = i
    assert main_row_idx is not None, "达安 markdown 应含 SILICA GEL 主行"
    assert barcode_row_idx is not None, "达安 markdown 应含 BARCODE 行"
    assert barcode_row_idx > main_row_idx, (
        f"BARCODE 行应在主行下方（A2a 触发形态），实际 main={main_row_idx} barcode={barcode_row_idx}"
    )


# ---------------------------------------------------------------------------
# 主入口（直接 python3 跑也能用）
# ---------------------------------------------------------------------------


def _run_all() -> int:
    """直接 python3 跑时的兜底执行。"""
    import inspect
    funcs = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = skipped = 0
    for name, fn in funcs:
        sig = inspect.signature(fn)
        has_required_param = any(
            p.default is inspect.Parameter.empty
            and p.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
            for p in sig.parameters.values()
        )
        if has_required_param:
            print(f"  ~ {name} (requires pytest fixture)")
            skipped += 1
            continue
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    if "-v" in sys.argv or "--verbose" in sys.argv:
        sys.exit(pytest.main([__file__, "-v"]))
    sys.exit(_run_all())