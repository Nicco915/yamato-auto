# -*- coding: utf-8 -*-
"""提取失败告警闭环测试（llm_client/session/service/writer/board）。

对应设计文档《提取失败告警闭环实施计划_20260930.md》§5：
1. FatalLLMError 分类：403 PermissionDeniedError / 401 AuthenticationError
   → 不重试直接抛 FatalLLMError（调用 1 次）；429 RateLimitError 仍走原重试；
2. usage 分桶：fatal/rate_limit/other 计数 + last_error；
3. session.process_file / force_extract 遇 FatalLLMError 上抛不吞，
   后续文件不再处理；普通单文件异常仍按文件吞掉（channel_error）；
4. 预提取：空结果标 failed 且删空 session 缓存；FatalLLMError 整批终止，
   剩余厂标 failed；
5. writer._upsert_db：全 None 重量 SKU 跳过主库（FactorySKU 无记录）；
6. board.folder_plan / prepare_folders：existing/missing 分类、别名简称命名、
   幂等、多装箱单未指定 → GET 返回 need_choice / POST 422；
7. get_batch_detail role=failed：requirements 三厂、factory_outputs 一厂、
   一厂审计 approved 无快照（done + can_reopen=False）、其余 failed。

运行：
    cd app && PYTHONPATH=. python3 -m pytest tests/test_extraction_failure_feedback.py -v

隔离红线（2026-08-11 事故教训）：
- import app 模块前先设 YAMATO_TEST_MODE=1 + YAMATO_DOTENV_PATH=临时空 .env
  （llm_client 模块级 load_dotenv(override=True) 只可能读到空文件）；
- import 后再调 validation/_test_isolation.isolate_to_tmp 把
  checkpoint/master/output/sessions 全部指向临时目录（守卫断言不碰生产库）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

# ---- 隔离门①：import 前设 YAMATO_DOTENV_PATH（血泪红线）----
_TMP = Path(tempfile.mkdtemp(prefix="yamato_extract_fail_test_"))
(_TMP / ".env").write_text("# isolated .env\n", encoding="utf-8")
os.environ["YAMATO_TEST_MODE"] = "1"
os.environ["YAMATO_DOTENV_PATH"] = str(_TMP / ".env")
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"
# chat_completion 的 get_settings() 要求 key 存在（OpenAI client 已被 mock，不发真请求）
os.environ.setdefault("SILICONFLOW_API_KEY", "sk-test-dummy")

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402,F401  确保全部 app 模块先于隔离 import
from app.config import get_settings  # noqa: E402
from app.db import batch_store  # noqa: E402,F401
from app.extraction import llm_client  # noqa: E402
from app.extraction import session as session_mod  # noqa: E402
from app.extraction.llm_client import FatalLLMError  # noqa: E402
from app.nodes import extraction_node  # noqa: E402
from app.nodes import writer as writer_mod  # noqa: E402
from app.orchestrator import board  # noqa: E402

# ---- 隔离门②：import 后把 db/output/sessions 指向临时目录 ----
from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_extract_fail_test_", alias_map_copy=True)

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402

client = TestClient(app)


# ---------------------------------------------------------------------------
# 公共 helpers
# ---------------------------------------------------------------------------

def _api_error(exc_cls, status: int, message: str):
    """构造 openai SDK 的 APIStatusError 子类实例（需 httpx Response）。"""
    import httpx
    req = httpx.Request("POST", "https://api.test/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return exc_cls(message, response=resp, body=None)


def _success_resp(content: str = '{"ok": true}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1,
                              total_tokens=4),
    )


def _fake_client(create_fn):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_fn)),
        base_url="https://api.test/v1",
    )


@pytest.fixture(autouse=True)
def _reset_usage():
    """每个用例前后清空全局用量追踪器，避免跨用例污染。"""
    llm_client.usage_tracker.reset()
    yield
    llm_client.usage_tracker.reset()


@pytest.fixture()
def watch(tmp_path):
    """每个用例独立监控目录；结束后恢复 settings.watch_dir。"""
    w = tmp_path / "watch"
    w.mkdir()
    original = get_settings().watch_dir
    get_settings().watch_dir = str(w)
    try:
        yield w
    finally:
        get_settings().watch_dir = original


def _make_downstream(path: Path, factories: list[str]):
    """造最小可解析的 ContentsOfTheContainer 装箱单（工厂列 + SKU 列）。"""
    wb = Workbook()
    ws = wb.active
    ws.append([get_settings().col_factory, get_settings().col_sku])
    for factory in factories:
        ws.append([factory, "4901234567890"])
    wb.save(path)


def _seed_factory(name: str, short_name: str | None = None):
    from app.db.models import Factory
    from app.db.session import get_session
    with get_session() as s:
        f = Factory(factory_name=name, short_name=short_name)
        s.add(f)
        s.commit()
        return f.factory_id


# ---------------------------------------------------------------------------
# 1. FatalLLMError 分类：403/401 不重试直接抛
# ---------------------------------------------------------------------------

def test_permission_denied_raises_fatal_without_retry(monkeypatch):
    """403 PermissionDeniedError → FatalLLMError，且 chat.completions.create
    只调用 1 次（不重试）；401 AuthenticationError 同理。"""
    from openai import AuthenticationError, PermissionDeniedError

    for exc_cls, status in ((PermissionDeniedError, 403),
                            (AuthenticationError, 401)):
        calls = []

        def boom(**kwargs):
            calls.append(kwargs)
            raise _api_error(exc_cls, status,
                             "AccessDenied.Unpurchased: 模型无访问权限")

        monkeypatch.setattr(llm_client, "_get_client",
                            lambda: _fake_client(boom))
        with pytest.raises(FatalLLMError) as ei:
            llm_client.chat_completion(
                [{"role": "user", "content": "hi"}], source_file="t1")
        assert type(ei.value).__name__ == "FatalLLMError"
        assert "LLM 调用被拒绝" in str(ei.value)
        assert len(calls) == 1, f"{exc_cls.__name__} 不应重试"

    summary = llm_client.usage_tracker.summary()
    assert summary["failed_by_kind"]["fatal"] == 2
    assert summary["failed_calls"] == 2


def test_rate_limit_still_retries_with_backoff(monkeypatch):
    """429 RateLimitError 不属于致命错误：仍走原指数退避重试路径，
    第三次成功则正常返回（调用 3 次）。"""
    from openai import RateLimitError

    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise _api_error(RateLimitError, 429, "rate limit exceeded")
        return _success_resp()

    monkeypatch.setattr(llm_client, "_get_client", lambda: _fake_client(flaky))
    # 退避 sleep 打桩为无操作，避免测试拖慢（monkeypatch 结束后自动恢复）
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_a, **_k: None)

    out = llm_client.chat_completion([{"role": "user", "content": "hi"}],
                                     source_file="t2")
    assert out == '{"ok": true}'
    assert len(calls) == 3

    summary = llm_client.usage_tracker.summary()
    assert summary["failed_by_kind"]["rate_limit"] == 2
    assert summary["failed_by_kind"]["fatal"] == 0
    assert summary["calls"] == 3


# ---------------------------------------------------------------------------
# 2. usage 分桶 + last_error
# ---------------------------------------------------------------------------

def test_usage_summary_failed_by_kind_and_last_error(monkeypatch):
    """fatal/rate_limit/other 三类失败分别计数；last_error 为最近一条失败摘要。"""
    from openai import BadRequestError, PermissionDeniedError, RateLimitError

    script = [
        _api_error(PermissionDeniedError, 403, "AccessDenied.Unpurchased"),
        _api_error(RateLimitError, 429, "rate limited"),
        _api_error(RateLimitError, 429, "rate limited"),
        _api_error(RateLimitError, 429, "rate limited"),
        _api_error(RateLimitError, 429, "rate limited"),  # 重试耗尽共 4 次
        _api_error(BadRequestError, 400, "bad request: invalid param"),
    ]

    def scripted(**kwargs):
        exc = script.pop(0)
        raise exc

    monkeypatch.setattr(llm_client, "_get_client",
                        lambda: _fake_client(scripted))
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_a, **_k: None)

    # fatal：1 次调用直接抛
    with pytest.raises(FatalLLMError):
        llm_client.chat_completion([{"role": "user", "content": "x"}])
    # rate_limit：重试耗尽（1 + MAX_API_RETRIES 次）后原样抛 RateLimitError
    with pytest.raises(RateLimitError):
        llm_client.chat_completion([{"role": "user", "content": "x"}])
    # other：400 BadRequest 不可重试，1 次即抛
    with pytest.raises(BadRequestError):
        llm_client.chat_completion([{"role": "user", "content": "x"}])

    summary = llm_client.usage_tracker.summary()
    assert summary["failed_by_kind"] == {"fatal": 1, "rate_limit": 4, "other": 1}
    assert summary["failed_calls"] == 6
    assert summary["last_error"] is not None
    assert "BadRequestError" in summary["last_error"]


# ---------------------------------------------------------------------------
# 3. session.process_file / force_extract 遇 FatalLLMError 上抛不吞
# ---------------------------------------------------------------------------

def _patch_candidate_scan(monkeypatch):
    """把 scan_file/_name_score 打桩为「是候选箱单」，聚焦测试 LLM 错误路径。"""
    profile = SimpleNamespace(channel="excel", is_candidate=True,
                              barcodes={"4901234567890"})
    monkeypatch.setattr(session_mod, "scan_file", lambda _p: profile)
    monkeypatch.setattr(session_mod, "_name_score", lambda _p: 0)


def test_process_file_reraises_fatal_and_stops_following_files(
        tmp_path, monkeypatch):
    """process_file 遇 FatalLLMError 直接上抛（不吞为 channel_error），
    调用方循环随之终止——后续文件不再送 LLM（_route_extract 只被调 1 次）。"""
    _patch_candidate_scan(monkeypatch)
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise FatalLLMError("LLM 调用被拒绝（PermissionDeniedError）：403")

    monkeypatch.setattr(session_mod, "_route_extract", boom)

    f1 = tmp_path / "packing1.xlsx"
    f2 = tmp_path / "packing2.xlsx"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    sess = session_mod.FactorySession(factory="T3工厂甲")
    done = []
    with pytest.raises(FatalLLMError):
        for p in (f1, f2):
            session_mod.process_file(sess, str(p))
            done.append(p.name)
    assert calls == [1], "致命错误后后续文件不应再调 LLM"
    assert done == [], "第一个文件即上抛，无文件完成"


def test_process_file_non_fatal_error_still_swallowed(tmp_path, monkeypatch):
    """对照组：普通单文件异常维持现状按文件吞掉（返回 channel_error），
    后续文件照常处理。"""
    _patch_candidate_scan(monkeypatch)
    calls = []

    def flaky(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("单文件解析失败")
        return SimpleNamespace(items=[], notes=[], error=None)

    monkeypatch.setattr(session_mod, "_route_extract", flaky)

    f1 = tmp_path / "a.xlsx"
    f2 = tmp_path / "b.xlsx"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")

    sess = session_mod.FactorySession(factory="T3工厂乙")
    r1 = session_mod.process_file(sess, str(f1))
    assert r1.action == "channel_error"  # 普通异常吞掉，不上抛
    r2 = session_mod.process_file(sess, str(f2))
    assert r2.action != "channel_error"
    assert len(calls) == 2


def test_force_extract_reraises_fatal(tmp_path, monkeypatch):
    """force_extract 遇 FatalLLMError 同样上抛不吞。"""
    _patch_candidate_scan(monkeypatch)

    def boom(*_a, **_k):
        raise FatalLLMError("LLM 调用被拒绝（AuthenticationError）：401")

    monkeypatch.setattr(session_mod, "_route_extract", boom)
    f = tmp_path / "forced.xlsx"
    f.write_bytes(b"x")
    sess = session_mod.FactorySession(factory="T3工厂丙")
    with pytest.raises(FatalLLMError):
        session_mod.force_extract(sess, str(f))


# ---------------------------------------------------------------------------
# 4. 预提取：空结果标 failed 且删空缓存；FatalLLMError 整批终止
# ---------------------------------------------------------------------------

def _setup_upstream(tmp_path, factories: list[str]) -> Path:
    root = tmp_path / "upstream"
    root.mkdir()
    for f in factories:
        (root / f).mkdir()
    return root


def test_preextract_empty_result_marks_failed_and_drops_cache(
        tmp_path, monkeypatch):
    """提取完成但 0 条 → 进度 failed（原因「提取结果为空」），且刚落盘的
    空 session 缓存被删除（防图内命中空缓存直接出占位）；其余厂正常 done。"""
    tid = "PRE-EMPTY-T1"
    factories = ["T4空厂", "T4好厂"]
    root = _setup_upstream(tmp_path, factories)

    def fake_run(batch_id, folder_path, factory_name, expected_skus):
        sess = session_mod.FactorySession(factory=factory_name)
        if factory_name == "T4空厂":
            # 模拟真实 _run_factory_session 的落盘行为：空 session 也先写缓存
            d = session_mod.batch_session_dir(batch_id)
            d.mkdir(parents=True, exist_ok=True)
            session_mod.batch_session_path(batch_id, factory_name).write_text(
                json.dumps(sess.to_dict(), ensure_ascii=False), encoding="utf-8")
            return sess  # items 空
        sess.items = {"4901234567890": {"sku_code": "4901234567890"}}
        return sess

    monkeypatch.setattr(extraction_node, "_run_factory_session", fake_run)

    service._pre_extract_factories(
        factories, str(root), {f: [] for f in factories}, thread_id=tid)

    progress = service.load_pre_extraction_progress(tid)
    assert progress is not None
    by_name = {f["factory"]: f for f in progress["factories"]}
    assert by_name["T4空厂"]["status"] == "failed"
    assert "提取结果为空" in (by_name["T4空厂"]["error"] or "")
    assert by_name["T4好厂"]["status"] == "done"
    # 空 session 缓存已被删除
    assert not session_mod.batch_session_path(tid, "T4空厂").exists()


def test_preextract_fatal_aborts_whole_batch(tmp_path, monkeypatch):
    """FatalLLMError 熔断：整批预提取终止，出错厂标 failed（含 FatalLLMError
    原因），剩余厂全部标 failed（「整批预提取已终止」），不再烧 token。"""
    tid = "PRE-FATAL-T1"
    factories = ["T4F甲", "T4F乙", "T4F丙"]
    root = _setup_upstream(tmp_path, factories)
    calls = []

    def boom(batch_id, folder_path, factory_name, expected_skus):
        calls.append(factory_name)
        raise FatalLLMError(
            "LLM 调用被拒绝（PermissionDeniedError）：403 AccessDenied.Unpurchased")

    monkeypatch.setattr(extraction_node, "_run_factory_session", boom)

    service._pre_extract_factories(
        factories, str(root), {f: [] for f in factories}, thread_id=tid)

    assert calls == ["T4F甲"], "致命错误后不再处理后续工厂"
    progress = service.load_pre_extraction_progress(tid)
    by_name = {f["factory"]: f for f in progress["factories"]}
    assert by_name["T4F甲"]["status"] == "failed"
    assert "FatalLLMError" in (by_name["T4F甲"]["error"] or "")
    for rest in ("T4F乙", "T4F丙"):
        assert by_name[rest]["status"] == "failed"
        assert "整批预提取已终止" in (by_name[rest]["error"] or "")


# ---------------------------------------------------------------------------
# 5. writer._upsert_db：全 None 重量 SKU 不入主库
# ---------------------------------------------------------------------------

def test_upsert_db_skips_all_none_weight_rows():
    """净重/毛重全 None 的占位 SKU 不 INSERT/UPDATE 主库（FactorySKU 无记录）；
    有重量的正常 SKU 对照落库成功。"""
    from app.db.models import Factory, FactorySKU
    from app.db.session import get_session

    state = {"current_factory_data": {
        "factory_name": "T5占位工厂",
        "calculated_items": [
            {"sku": "5901234567890",
             "calculation": {"calculated_unit_net": None,
                             "calculated_unit_gross": None}},
        ],
    }}
    inserted, updated = writer_mod._upsert_db(state)
    assert (inserted, updated) == (0, 0)

    with get_session() as s:
        factory = s.query(Factory).filter(
            Factory.factory_name == "T5占位工厂").one()
        rows = s.query(FactorySKU).filter(
            FactorySKU.factory_id == factory.factory_id).all()
        assert rows == [], "全 None 重量 SKU 不得写入主库"

    # 对照：有重量的正常 SKU 落库成功
    state_ok = {"current_factory_data": {
        "factory_name": "T5占位工厂",
        "calculated_items": [
            {"sku": "5901234567891",
             "calculation": {"calculated_unit_net": 1.5,
                             "calculated_unit_gross": 1.8}},
        ],
    }}
    inserted, updated = writer_mod._upsert_db(state_ok)
    assert (inserted, updated) == (1, 0)
    with get_session() as s:
        factory = s.query(Factory).filter(
            Factory.factory_name == "T5占位工厂").one()
        rec = s.query(FactorySKU).filter(
            FactorySKU.factory_id == factory.factory_id,
            FactorySKU.sku_code == "5901234567891").one()
        assert float(rec.unit_net_weight) == 1.5
        assert float(rec.unit_gross_weight) == 1.8


# ---------------------------------------------------------------------------
# 6. board.folder_plan / prepare_folders：分类、别名简称命名、幂等、多装箱单
# ---------------------------------------------------------------------------

def test_folder_plan_existing_vs_missing_and_alias_short_name(watch):
    """装箱单两厂：一厂文件夹已存在 → existing；另一厂主数据有 short_name
    → missing 且待建文件夹名按别名简称（方案 B 命名规则）。"""
    folder = watch / "T6批次"
    upstream = folder / "工厂"
    upstream.mkdir(parents=True)
    (upstream / "T6工場甲").mkdir()  # 已存在（exact 命中）
    _make_downstream(folder / "ContentsOfTheContainer_t6.xlsx",
                     ["T6工場甲", "T6工場乙"])
    _seed_factory("T6工場乙", short_name="乙T6")

    plan = board.folder_plan("T6批次")
    assert plan["need_choice"] is False
    assert plan["need_upstream"] is False
    existing = {e["factory"]: e for e in plan["existing"]}
    assert "T6工場甲" in existing
    assert existing["T6工場甲"]["folder_name"] == "T6工場甲"
    assert plan["missing"] == [{"factory": "T6工場乙", "folder_name": "乙T6"}]

    # 端点契约（GET 只读预览）
    r = client.get("/api/v1/watch/folder-plan",
                   params={"folder_name": "T6批次"})
    assert r.status_code == 200
    body = r.json()
    assert body["need_choice"] is False
    assert {m["folder_name"] for m in body["missing"]} == {"乙T6"}


def test_prepare_folders_creates_and_is_idempotent(watch):
    """POST 确认创建：missing 厂批量 mkdir（按别名简称）；重复调用幂等
    （created 为空，第二次 folder_plan 中该厂落入 existing）。"""
    folder = watch / "T6批次2"
    upstream = folder / "工厂"
    upstream.mkdir(parents=True)
    _make_downstream(folder / "ContentsOfTheContainer_t6b.xlsx", ["T6工場丙"])
    _seed_factory("T6工場丙", short_name="丙T6")

    r = client.post("/api/v1/watch/prepare-folders",
                    json={"folder_name": "T6批次2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["created"] == ["丙T6"]
    assert (upstream / "丙T6").is_dir()

    # 幂等：再调一次，不再创建；计划里该厂已被确定性档（alias_folder）命中
    r2 = client.post("/api/v1/watch/prepare-folders",
                     json={"folder_name": "T6批次2"})
    assert r2.status_code == 200
    assert r2.json()["created"] == []
    plan = board.folder_plan("T6批次2")
    assert plan["missing"] == []
    assert {e["folder_name"] for e in plan["existing"]} == {"丙T6"}


def test_folder_plan_multiple_downstream_need_choice_and_post_422(watch):
    """多装箱单候选且未指定：GET 返回 need_choice=true + 候选清单；
    POST（写路径）不返回选择信号，直接 422。"""
    folder = watch / "T6批次3"
    folder.mkdir()
    _make_downstream(folder / "ContentsOfTheContainer_a.xlsx", ["T6工場丁"])
    _make_downstream(folder / "ContentsOfTheContainer_b.xlsx", ["T6工場丁"])

    r = client.get("/api/v1/watch/folder-plan",
                   params={"folder_name": "T6批次3"})
    assert r.status_code == 200
    body = r.json()
    assert body["need_choice"] is True
    assert len(body["downstream_candidates"]) == 2
    assert body["missing"] == []

    r2 = client.post("/api/v1/watch/prepare-folders",
                     json={"folder_name": "T6批次3"})
    assert r2.status_code == 422
    assert "多个下游装箱单" in r2.json()["detail"]


def test_folder_plan_missing_folder_404(watch):
    """文件夹不存在 → GET 404（FileNotFoundError 契约）。"""
    r = client.get("/api/v1/watch/folder-plan",
                   params={"folder_name": "不存在的批次"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 7. get_batch_detail：role=failed 推导 + 无快照 done 厂 can_reopen=False
# ---------------------------------------------------------------------------

class _FakeGraph:
    """可控假 graph：get_state 返回构造的完成态 state（无 next、无 interrupt）。"""

    def __init__(self, values):
        self._values = values

    def get_state(self, _config):
        return SimpleNamespace(values=self._values, next=(), tasks=[],
                               created_at="2026-09-30T00:00:00+00:00")


def test_batch_detail_failed_role_and_no_snapshot_no_reopen(monkeypatch):
    """requirements 三厂：快照内厂 → done（可 reopen）；审计 approved 但无快照
    → done 且 can_reopen=False；既无快照又无 approved 审计 → failed。"""
    from app.db.models import ReviewAudit
    from app.db.session import get_session

    tid = "FAIL-DETAIL-T1"
    values = {
        "batch_id": tid,
        "downstream_requirements": {
            "T7厂A": ["5901234567890"],
            "T7厂B": ["5901234567890"],
            "T7厂C": ["5901234567890"],
        },
        "factory_outputs": {"T7厂A": {"factory_name": "T7厂A",
                                      "calculated_items": []}},
        "pending_factories": [],
        "deferred_factories": [],
        "current_factory_data": {},
    }
    monkeypatch.setattr(service, "get_graph", lambda: _FakeGraph(values))

    with get_session() as s:
        s.add(ReviewAudit(thread_id=tid, factory_name="T7厂C", approved=True,
                          edited_count=0, changes_json="[]", new_skus_json="[]",
                          result_status="approved"))
        s.commit()

    detail = service.get_batch_detail(tid)
    roles = {f["factory"]: f for f in detail["factories"]}

    assert roles["T7厂A"]["role"] == "done"
    assert roles["T7厂A"].get("can_reopen") is not False  # 有快照，可重新打开
    assert roles["T7厂B"]["role"] == "failed"  # 无快照且无 approved 审计
    assert roles["T7厂C"]["role"] == "done"
    assert roles["T7厂C"]["can_reopen"] is False  # 无快照，堵 reopen 404
    assert detail["status"] == "completed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
