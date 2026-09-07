# -*- coding: utf-8 -*-
"""调度 Agent 监控目录扫描建批工具测试：
scan_new_batches / start_scanned_batch / mark_batch_done + 快路径句式。

覆盖：
1. scan：空监控目录提示；候选列出（folder_name/has_content/下游文件名）；
   已建批文件夹被跳过；
2. start preview：正常候选出预览；文件夹不存在/已占用/多下游表 → blocked；
3. start execute：monkeypatch service 断言参数透传 + ValueError 转 error；
   真实路径走通（mock 提取跑图到挂起，批次记录落库，links 齐备）；
4. mark_done preview/execute：标记后 batches 表 status=completed 且
   completed_at 非空，扫描不再列出；重复标记 → blocked/error；
5. 快路径：「扫描新批次」命中；「启动新批次」不落快路径（交给 LLM）。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_scan_tools_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）；
监控目录经 extra_env={"WATCH_DIR": ...} 随隔离一并设置。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

from openpyxl import Workbook  # noqa: E402

from app.api import service  # noqa: E402
from app.db import batch_store  # noqa: E402
from app.dispatcher import fastpath  # noqa: E402
# 血泪红线：dispatcher.tools 的 import 链（service→…→llm_client）会执行
# load_dotenv(override=True)，必须在 isolate_to_tmp 之前完成全部 app 模块
# import，否则隔离 env 会被打回真实路径
from app.dispatcher import tools as dispatcher_tools  # noqa: E402
from app.nodes import extraction_node as en  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 监控目录先建好（隔离只设环境变量，不管建目录）
_WATCH = Path(tempfile.mkdtemp(prefix="yamato_scan_tools_watch_"))

TMP = isolate_to_tmp("yamato_dispatcher_scan_tools_",
                     extra_env={"WATCH_DIR": str(_WATCH)})

from app.config import get_settings  # noqa: E402
assert get_settings().watch_dir == str(_WATCH)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _watch_dir_guard():
    """混跑保险：settings 是全局单例，同进程后导入的测试文件（cancel/autopin
    等）各自的 isolate_to_tmp 会覆盖 watch_dir——每个用例前钉回本文件的
    监控目录，结束还原。"""
    original = get_settings().watch_dir
    get_settings().watch_dir = str(_WATCH)
    try:
        yield
    finally:
        get_settings().watch_dir = original

HEADER = ["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"]


def _make_xlsx(path: Path, rows: list[tuple[str, str, str, int]] | None = None):
    """写 xlsx：rows 为 None 时空表（仅供文件名探测）；否则带表头的最小装箱单。"""
    wb = Workbook()
    ws = wb.active
    if rows is not None:
        ws.append(HEADER)
        for factory, sku, name, qty in rows:
            ws.append([factory, sku, name, qty])
    wb.save(path)


def _new_folder(name: str, *, downstream: bool = True,
                downstream_count: int = 1) -> Path:
    """在监控目录下建一个候选子文件夹，可选放 1~N 份下游装箱单。"""
    sub = _WATCH / name
    sub.mkdir(parents=True, exist_ok=True)
    for i in range(downstream_count):
        if downstream:
            suffix = f"_{i}" if i else ""
            _make_xlsx(sub / f"ContentsOfTheContainer{suffix}.xlsx")
    return sub


def _force_mock_extraction(monkeypatch) -> None:
    """全量 pytest 下 extraction_node 可能已被绑定真实提取线，强制回 mock
    （与 add_factories_test 同模式）。"""
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")


# ---------------------------------------------------------------------------
# 1. scan_new_batches
# ---------------------------------------------------------------------------

def test_scan_empty_watch_dir():
    """空监控目录 → 空候选 + 明确提示（本用例必须先于任何建文件夹的用例）。"""
    r = dispatcher_tools._fn_scan_new_batches({})
    assert r["count"] == 0 and r["candidates"] == []
    assert "没有新批次候选" in r["message"]


def test_scan_lists_candidate():
    sub = _new_folder("SCAN_NEW1")
    r = dispatcher_tools._fn_scan_new_batches({})
    names = {c["folder_name"] for c in r["candidates"]}
    assert "SCAN_NEW1" in names
    cand = next(c for c in r["candidates"] if c["folder_name"] == "SCAN_NEW1")
    assert cand["has_content"] is True
    assert any("ContentsOfTheContainer" in n for n in cand["downstream_candidates"])
    assert sub.name == "SCAN_NEW1"


def test_scan_skips_existing_batch():
    _new_folder("SCAN_DONE1")
    batch_store.upsert_batch("SCAN_DONE1", watch_dir=str(_WATCH),
                             folder_name="SCAN_DONE1", status="completed")
    r = dispatcher_tools._fn_scan_new_batches({})
    names = {c["folder_name"] for c in r["candidates"]}
    assert "SCAN_DONE1" not in names
    assert "SCAN_NEW1" in names  # 上一个用例的候选仍在


# ---------------------------------------------------------------------------
# 2. start_scanned_batch preview
# ---------------------------------------------------------------------------

def test_start_preview_ok():
    _new_folder("START_OK1")
    p = dispatcher_tools._preview_start_scanned_batch({"folder_name": "START_OK1"})
    assert not p.get("blocked"), f"不应 blocked: {p}"
    text = "\n".join(p["lines"])
    assert "START_OK1" in text
    assert "自动匹配" in text           # 唯一下游表自动命中
    assert "默认=装箱单所在目录" in text  # 平铺结构：装箱单父目录=子文件夹本身


def test_start_preview_nested():
    """嵌套结构（批次文件夹/中间层/装箱单）：向下一层钻取命中，
    上游默认=中间层目录；中间层下有「工厂」子目录时默认取它。"""
    sub = _WATCH / "START_NEST1"
    mid = sub / "84"
    mid.mkdir(parents=True)
    _make_xlsx(mid / "ContentsOfTheContainer.xlsx")
    p = dispatcher_tools._preview_start_scanned_batch(
        {"folder_name": "START_NEST1"})
    assert not p.get("blocked"), f"不应 blocked: {p}"
    text = "\n".join(p["lines"])
    assert "自动匹配" in text
    assert f"上游工厂文件夹: {mid}（默认=装箱单所在目录）" in text

    # 有「工厂」子目录 → 默认取它（生产真实结构约定）
    sub2 = _WATCH / "START_NEST2"
    mid2 = sub2 / "93"
    (mid2 / "工厂").mkdir(parents=True)
    _make_xlsx(mid2 / "ContentsOfTheContainer.xlsx")
    p2 = dispatcher_tools._preview_start_scanned_batch(
        {"folder_name": "START_NEST2"})
    assert not p2.get("blocked"), f"不应 blocked: {p2}"
    assert f"上游工厂文件夹: {mid2 / '工厂'}（默认=「工厂」子目录）" \
        in "\n".join(p2["lines"])


def test_start_preview_missing_folder():
    p = dispatcher_tools._preview_start_scanned_batch(
        {"folder_name": "NO_SUCH_FOLDER"})
    assert p.get("blocked") is True


def test_start_preview_occupied():
    _new_folder("START_BUSY1")
    batch_store.upsert_batch("START_BUSY1", watch_dir=str(_WATCH),
                             folder_name="START_BUSY1", status="running")
    p = dispatcher_tools._preview_start_scanned_batch({"folder_name": "START_BUSY1"})
    assert p.get("blocked") is True
    assert "已建过批次" in p["summary"]


def test_start_preview_multiple_downstream():
    _new_folder("START_MULTI1", downstream_count=2)
    p = dispatcher_tools._preview_start_scanned_batch(
        {"folder_name": "START_MULTI1"})
    assert p.get("blocked") is True
    assert "多个下游装箱单" in p["summary"]


def test_start_preview_no_downstream():
    _new_folder("START_EMPTY1", downstream=False)
    p = dispatcher_tools._preview_start_scanned_batch(
        {"folder_name": "START_EMPTY1"})
    assert p.get("blocked") is True
    assert "未找到下游装箱单" in p["summary"]


# ---------------------------------------------------------------------------
# 3. start_scanned_batch execute
# ---------------------------------------------------------------------------

def test_start_exec_param_passthrough(monkeypatch):
    """参数原样透传 service.start_batch_from_scan。"""
    _new_folder("START_MOCK1")
    captured = {}

    def fake_start(folder_name, thread_id=None, downstream_file_path=None,
                   upstream_root=None, on_progress=None):
        captured.update(folder_name=folder_name, thread_id=thread_id,
                        downstream_file_path=downstream_file_path,
                        upstream_root=upstream_root)
        return {"status": "pending_human_review", "thread_id": thread_id or folder_name}

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)
    r = dispatcher_tools._exec_start_scanned_batch(
        {"folder_name": "START_MOCK1", "thread_id": "TID-X",
         "downstream_file_path": "/tmp/d.xlsx", "upstream_root": "/tmp/u"})
    assert captured == {"folder_name": "START_MOCK1", "thread_id": "TID-X",
                        "downstream_file_path": "/tmp/d.xlsx",
                        "upstream_root": "/tmp/u"}
    assert r["thread_id"] == "TID-X"
    hrefs = [l["href"] for l in r["links"]]
    assert "/review?thread_id=TID-X" in hrefs   # 待审核 → 去审核链接
    assert "/batch/TID-X" in hrefs


def test_start_exec_value_error(monkeypatch):
    """service 抛 ValueError → {"error": ...}，绝不抛出。"""
    def fake_start(folder_name, **kwargs):
        raise ValueError("子文件夹不存在: xxx")

    monkeypatch.setattr(service, "start_batch_from_scan", fake_start)
    r = dispatcher_tools._exec_start_scanned_batch({"folder_name": "WHATEVER"})
    assert "error" in r and "子文件夹不存在" in r["error"]


def test_start_exec_real_run(monkeypatch):
    """真实路径走通（生产结构）：监控目录/批次文件夹/93/装箱单+工厂/工厂A →
    mock 提取跑图到挂起，批次记录落库（upstream=93/工厂），扫描跳过。"""
    _force_mock_extraction(monkeypatch)
    sub = _WATCH / "START_REAL1"
    mid = sub / "93"
    (mid / "工厂" / "工厂A").mkdir(parents=True)
    _make_xlsx(mid / "ContentsOfTheContainer.xlsx",
               [("工厂A", "SKU-A1", "测试品A", 10)])

    r = dispatcher_tools._exec_start_scanned_batch({"folder_name": "START_REAL1"})
    assert "error" not in r, f"执行失败: {r}"
    assert r["status"] == "pending_human_review"
    assert r["thread_id"] == "START_REAL1"

    rec = batch_store.get_batch("START_REAL1")
    assert rec is not None and rec["folder_name"] == "START_REAL1"
    assert rec["watch_dir"] == str(_WATCH)
    assert rec["upstream_root"] == str(mid / "工厂")  # 「工厂」子目录约定

    names = {c["folder_name"]
             for c in dispatcher_tools._fn_scan_new_batches({})["candidates"]}
    assert "START_REAL1" not in names  # 已建批，扫描跳过


# ---------------------------------------------------------------------------
# 4. mark_batch_done
# ---------------------------------------------------------------------------

def test_mark_done_preview_ok():
    _new_folder("MARK_OK1")
    p = dispatcher_tools._preview_mark_batch_done({"folder_name": "MARK_OK1"})
    assert not p.get("blocked"), f"不应 blocked: {p}"
    assert "1 个" in p["summary"]
    assert any("MARK_OK1" in l for l in p["lines"])


def test_mark_done_preview_missing_folder():
    p = dispatcher_tools._preview_mark_batch_done({"folder_name": "NO_SUCH"})
    assert p.get("blocked") is True


def test_mark_done_exec_and_scan_skip():
    _new_folder("MARK_EXEC1")
    # 标记前扫描可见
    names = {c["folder_name"]
             for c in dispatcher_tools._fn_scan_new_batches({})["candidates"]}
    assert "MARK_EXEC1" in names

    r = dispatcher_tools._exec_mark_batch_done({"folder_name": "MARK_EXEC1"})
    assert "error" not in r, f"执行失败: {r}"
    assert r["status"] == "completed"
    assert r["marked"] == ["MARK_EXEC1"]

    rec = batch_store.get_batch("MARK_EXEC1")
    assert rec is not None
    assert rec["status"] == "completed"
    assert rec["completed_at"] is not None

    # 标记后扫描跳过；重复标记 → preview blocked / execute 幂等跳过
    names = {c["folder_name"]
             for c in dispatcher_tools._fn_scan_new_batches({})["candidates"]}
    assert "MARK_EXEC1" not in names
    p = dispatcher_tools._preview_mark_batch_done({"folder_name": "MARK_EXEC1"})
    assert p.get("blocked") is True
    r2 = dispatcher_tools._exec_mark_batch_done({"folder_name": "MARK_EXEC1"})
    assert "error" not in r2 and r2["skipped"] == ["MARK_EXEC1"]


def test_mark_done_batch():
    """批量标记：folder_names 一次多个；已有记录的自动跳过；
    不存在的文件夹 preview blocked。"""
    _new_folder("BATCH_M1")
    _new_folder("BATCH_M2")
    _new_folder("BATCH_M3")
    batch_store.upsert_batch("BATCH_M3", watch_dir=str(_WATCH),
                             folder_name="BATCH_M3", status="completed")

    p = dispatcher_tools._preview_mark_batch_done(
        {"folder_names": ["BATCH_M1", "BATCH_M2", "BATCH_M3"]})
    assert not p.get("blocked"), f"不应 blocked: {p}"
    assert "2 个" in p["summary"]           # M3 已有记录被跳过
    assert any("BATCH_M3" in l and "跳过" in l for l in p["lines"])

    r = dispatcher_tools._exec_mark_batch_done(
        {"folder_names": ["BATCH_M1", "BATCH_M2", "BATCH_M3"]})
    assert "error" not in r, f"执行失败: {r}"
    assert sorted(r["marked"]) == ["BATCH_M1", "BATCH_M2"]
    assert r["skipped"] == ["BATCH_M3"]
    for n in ("BATCH_M1", "BATCH_M2"):
        rec = batch_store.get_batch(n)
        assert rec is not None and rec["status"] == "completed"

    # 单个与批量合并传参 + 去重
    r2 = dispatcher_tools._exec_mark_batch_done(
        {"folder_name": "BATCH_M1", "folder_names": ["BATCH_M1", "BATCH_M2"]})
    assert r2["marked"] == [] and sorted(r2["skipped"]) == ["BATCH_M1", "BATCH_M2"]


def test_mark_done_batch_missing_blocked():
    """批量里有不存在的文件夹 → 整批 blocked 不出确认卡。"""
    _new_folder("BATCH_X1")
    p = dispatcher_tools._preview_mark_batch_done(
        {"folder_names": ["BATCH_X1", "BATCH_NOPE"]})
    assert p.get("blocked") is True
    assert "BATCH_NOPE" in p["lines"][0]


# ---------------------------------------------------------------------------
# 4b. watch_overview（监控目录总览三档）
# ---------------------------------------------------------------------------

def test_watch_overview():
    _new_folder("OV_NEW1")
    _new_folder("OV_DONE1")
    _new_folder("OV_PROG1")
    batch_store.upsert_batch("OV_DONE1", watch_dir=str(_WATCH),
                             folder_name="OV_DONE1", status="completed")
    batch_store.upsert_batch("OV_PROG1", watch_dir=str(_WATCH),
                             folder_name="OV_PROG1", status="running")

    r = dispatcher_tools._fn_watch_overview({})
    assert "error" not in r
    done_names = {d["folder_name"] for d in r["done"]}
    prog_names = {d["folder_name"] for d in r["in_progress"]}
    cand_names = {c["folder_name"] for c in r["candidates"]}
    assert "OV_DONE1" in done_names
    assert "OV_PROG1" in prog_names
    assert "OV_NEW1" in cand_names
    assert "OV_DONE1" not in cand_names
    # 候选档附装箱单探测
    cand = next(c for c in r["candidates"] if c["folder_name"] == "OV_NEW1")
    assert cand["has_content"] is True


# ---------------------------------------------------------------------------
# 5. unmark_batch_done（mark_batch_done 逆操作）
# ---------------------------------------------------------------------------

def test_unmark_roundtrip():
    """标记 → 取消标记：记录删除，扫描重新列出该文件夹。"""
    _new_folder("UNMARK1")
    dispatcher_tools._exec_mark_batch_done({"folder_name": "UNMARK1"})
    assert batch_store.get_batch("UNMARK1") is not None

    p = dispatcher_tools._preview_unmark_batch_done({"folder_name": "UNMARK1"})
    assert not p.get("blocked"), f"不应 blocked: {p}"
    assert "UNMARK1" in p["summary"]

    r = dispatcher_tools._exec_unmark_batch_done({"folder_name": "UNMARK1"})
    assert "error" not in r, f"执行失败: {r}"
    assert batch_store.get_batch("UNMARK1") is None
    names = {c["folder_name"]
             for c in dispatcher_tools._fn_scan_new_batches({})["candidates"]}
    assert "UNMARK1" in names


def test_unmark_no_record():
    """无记录 → preview blocked / execute error。"""
    _new_folder("UNMARK_NONE")
    p = dispatcher_tools._preview_unmark_batch_done(
        {"folder_name": "UNMARK_NONE"})
    assert p.get("blocked") is True
    r = dispatcher_tools._exec_unmark_batch_done({"folder_name": "UNMARK_NONE"})
    assert "error" in r


def test_unmark_real_batch_refused():
    """真实跑过的批次（有 checkpoint，见 test_start_exec_real_run 的
    START_REAL1）→ 拒绝取消，提示用 rerun。"""
    assert batch_store.get_batch("START_REAL1") is not None
    p = dispatcher_tools._preview_unmark_batch_done(
        {"folder_name": "START_REAL1"})
    assert p.get("blocked") is True
    assert "真实跑过的批次" in p["summary"]
    r = dispatcher_tools._exec_unmark_batch_done(
        {"folder_name": "START_REAL1"})
    assert "error" in r and "rerun" in r["error"]
    assert batch_store.get_batch("START_REAL1") is not None  # 记录未被误删


# ---------------------------------------------------------------------------
# 6. 快路径
# ---------------------------------------------------------------------------

def test_fastpath_scan_hit():
    _new_folder("FAST_NEW1")
    r = fastpath.try_fastpath("扫描新批次")
    assert r is not None and r["tool"] == "scan_new_batches"
    assert "FAST_NEW1" in r["message"]

    r2 = fastpath.try_fastpath("有没有新批次")
    assert r2 is not None and r2["tool"] == "scan_new_batches"


def test_fastpath_start_not_hit():
    """「启动新批次」是写意图，不落快路径，交给 LLM 走确认门。"""
    assert fastpath.try_fastpath("启动新批次") is None
    assert fastpath.try_fastpath("把 XD430 标记为已完成") is None


def test_fastpath_watch_overview():
    r = fastpath.try_fastpath("监控目录下全部批次情况")
    assert r is not None and r["tool"] == "watch_overview"
    assert "已完成" in r["message"] and "未执行" in r["message"]
    # 监控目录语境外的「全部批次」仍走批次列表（checkpoint 视角）
    r2 = fastpath.try_fastpath("看看全部批次")
    assert r2 is None or r2["tool"] == "list_batches"


def test_folder_names_json_string_coerced():
    """LLM 把数组序列化成 JSON 字符串传参（qwen 系生产实测）→
    args_schema 层自动还原为 list，不再校验报错。"""
    from app.dispatcher.lc_tools import _json_schema_to_model
    model = _json_schema_to_model(
        "mark_batch_done",
        dispatcher_tools.TOOLS["mark_batch_done"].parameters)
    m = model.model_validate(
        {"folder_names": '["XD427-ETD0117", "XD428-ETD0207"]'})
    assert m.folder_names == ["XD427-ETD0117", "XD428-ETD0207"]
    # 真数组不受影响；非法字符串仍按原样报错
    m2 = model.model_validate({"folder_names": ["A"]})
    assert m2.folder_names == ["A"]
    import pydantic
    try:
        model.model_validate({"folder_names": "不是JSON"})
        raise AssertionError("非法字符串应校验失败")
    except pydantic.ValidationError:
        pass


# ---------------------------------------------------------------------------
# 7. 监控目录 env 键名回归（2026-09-07 事故：写入 YAMATO_WATCH_DIR 死配置，
#    Settings 无 env_prefix 只认 WATCH_DIR，set_paths 确认后扫描仍报未配置）
# ---------------------------------------------------------------------------

def test_allowed_paths_env_names_match_settings():
    """ALLOWED_PATHS 的 env 键名必须与 Settings 字段名一致（无 env_prefix，
    pydantic-settings 按字段名大小写不敏感匹配）。"""
    from app import agent_chat
    from app.config import Settings
    fields = set(Settings.model_fields)
    for key, (env_name, _kind, _label) in agent_chat.ALLOWED_PATHS.items():
        if key not in fields:
            continue  # gt_source 由 validation/ground_truth.py 直接读 env，不走 Settings
        assert env_name.lower() == key, \
            f"{key} 的 env 键 {env_name} 与 Settings 字段名不一致（读不到）"


def test_apply_watch_dir_effective_and_cleans_legacy(tmp_path):
    """apply_paths 改监控目录后：get_settings().watch_dir 立即生效；
    .env 里残留的 YAMATO_WATCH_DIR 死配置被清除。"""
    import os as _os
    from app import agent_chat
    from app.config import get_settings as _gs

    env_file = tmp_path / ".env"
    env_file.write_text("SILICONFLOW_API_KEY=sk-x\n"
                        "YAMATO_WATCH_DIR=D:/legacy/dead\n", encoding="utf-8")
    old_env = _os.environ.get("WATCH_DIR")
    try:
        r = agent_chat.apply_paths({"watch_dir": str(_WATCH)}, env_path=env_file)
        assert r["applied"] == {"WATCH_DIR": str(_WATCH)}
        # 运行时立即生效
        assert _gs().watch_dir == str(_WATCH)
        # .env 持久化 + 旧键清除
        text = env_file.read_text(encoding="utf-8")
        assert f"WATCH_DIR={_WATCH}" in text
        assert "YAMATO_WATCH_DIR" not in text
        assert "SILICONFLOW_API_KEY=sk-x" in text  # 无关行不动
    finally:
        if old_env is None:
            _os.environ.pop("WATCH_DIR", None)
        else:
            _os.environ["WATCH_DIR"] = old_env
        _gs.cache_clear()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
