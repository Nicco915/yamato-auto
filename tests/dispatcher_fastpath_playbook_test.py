# -*- coding: utf-8 -*-
"""剧本宏 process_skipped_factory + 快路径 fastpath 测试。

覆盖：
- 剧本宏 execute：两步成功合并回报 / 第一步失败不执行第二步 /
  第二步失败明确回报系统状态（对照已保存 + 补充失败原因，partial 标记）
- 剧本宏 preview：folder 非法 / 批次不存在 / 工厂已写入 三种警告提前亮出
- 快路径：批次列表 / 批次状态（含批次号提取）/ 用量 三意图命中；
  动作词（重跑/发起/删除）fall through；长句 fall through；
  批次号提取不到 fall through；list_batches 工具异常 fall through；
  get_batch_status 批次不存在 → 友好文案（不回喂 LLM）

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/dispatcher_fastpath_playbook_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线：必须先完成全部
app 模块 import，再 isolate_to_tmp——llm_client 的 load_dotenv(override=True)
会把隔离 env 打回真实路径）。
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
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

# 血泪红线：先完成 app 模块 import（import 链触发 load_dotenv override），再隔离
from app.dispatcher import fastpath  # noqa: E402
from app.dispatcher import tools as dispatcher_tools  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 假 upstream_root：供 validate_subfolder 校验（preview 用例）
_UPSTREAM_BASE = Path(tempfile.mkdtemp(prefix="yamato_fastpath_upstream_"))
(_UPSTREAM_BASE / "中地36").mkdir(parents=True)
(_UPSTREAM_BASE / "正达").mkdir(parents=True)

TMP = isolate_to_tmp("yamato_dispatcher_fastpath_",
                     extra_env={"UPSTREAM_ROOT": str(_UPSTREAM_BASE)})


# ---------------------------------------------------------------------------
# 剧本宏 process_skipped_factory
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_execs(monkeypatch):
    """替换两个执行体为记录调用的假实现。"""
    calls = {"alias": [], "add": []}

    def fake_alias(args, on_progress=None):
        calls["alias"].append(args)
        return {"ok": True, "message": "已永久保存对照：工厂「山東中地」→ 文件夹「中地36」"}

    def fake_add(args, on_progress=None):
        calls["add"].append(args)
        return {"ok": True, "message": "已补入 1 家工厂：山東中地。批次已完成重新提取并挂起。",
                "factories": ["山東中地"], "status": "pending_human_review",
                "thread_id": args.get("thread_id")}

    monkeypatch.setattr(dispatcher_tools, "_exec_create_factory_alias", fake_alias)
    monkeypatch.setattr(dispatcher_tools, "_exec_add_factories", fake_add)
    return calls


def test_playbook_two_steps_success(mock_execs):
    args = {"thread_id": "89", "factory": "山東中地", "folder": "中地36"}
    r = dispatcher_tools._exec_process_skipped_factory(args)
    assert r.get("ok"), f"应成功: {r}"
    assert len(mock_execs["alias"]) == 1 and len(mock_execs["add"]) == 1
    # 合并回报：两步结果都在 message 里
    assert "已永久保存对照" in r["message"] and "已补入" in r["message"]
    assert r["factories"] == ["山東中地"]


def test_playbook_step1_fails_step2_not_run(monkeypatch):
    calls = []

    def fail_alias(args, on_progress=None):
        return {"error": "文件夹 不存在目录 不存在"}

    def fake_add(args, on_progress=None):
        calls.append(args)
        return {"ok": True}

    monkeypatch.setattr(dispatcher_tools, "_exec_create_factory_alias", fail_alias)
    monkeypatch.setattr(dispatcher_tools, "_exec_add_factories", fake_add)
    r = dispatcher_tools._exec_process_skipped_factory(
        {"thread_id": "89", "factory": "X", "folder": "不存在目录"})
    assert "error" in r and "第 1 步" in r["error"]
    assert calls == [], "第一步失败时绝不执行第二步"


def test_playbook_step2_fails_reports_partial(monkeypatch):
    def ok_alias(args, on_progress=None):
        return {"ok": True, "message": "已永久保存对照"}

    def fail_add(args, on_progress=None):
        return {"error": "批次正在运行中"}

    monkeypatch.setattr(dispatcher_tools, "_exec_create_factory_alias", ok_alias)
    monkeypatch.setattr(dispatcher_tools, "_exec_add_factories", fail_add)
    r = dispatcher_tools._exec_process_skipped_factory(
        {"thread_id": "89", "factory": "山東中地", "folder": "中地36"})
    assert "error" in r
    # 明确回报系统状态：第一步已成功 + 第二步失败原因
    assert "已永久保存对照" in r["error"] and "批次正在运行中" in r["error"]
    assert r.get("partial") is True


def test_playbook_preview_warnings(monkeypatch):
    """preview：folder 非法 + 批次不存在 → 两条警告都进 warnings，不抛异常。"""
    monkeypatch.setattr(dispatcher_tools.service, "get_order_state",
                        lambda tid: {"exists": False})
    p = dispatcher_tools._preview_process_skipped_factory(
        {"thread_id": "不存在批次", "factory": "X", "folder": "不存在目录"})
    joined = " ".join(p["warnings"])
    assert "不存在目录" in joined and "不存在批次" in joined


def test_playbook_preview_warns_already_processed(monkeypatch):
    """工厂已审核写入 → preview 警告「不会重复处理」。"""
    monkeypatch.setattr(
        dispatcher_tools.service, "get_order_state",
        lambda tid: {"exists": True,
                     "values": {"factory_outputs": {"山東中地": {}}}})
    p = dispatcher_tools._preview_process_skipped_factory(
        {"thread_id": "89", "factory": "山東中地", "folder": "中地36"})
    assert any("不会重复处理" in w for w in p["warnings"])
    assert "第 1 步" in p["lines"][0] and "第 2 步" in p["lines"][1]


# ---------------------------------------------------------------------------
# 快路径 fastpath
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_tools(monkeypatch):
    """替换三个只读工具的 func 为可控假实现。"""
    calls = {"list_batches": [], "get_batch_status": [], "get_usage": []}

    def fake_list(args):
        calls["list_batches"].append(args)
        return {"batches": [
            {"thread_id": "88", "status": "completed",
             "progress": {"done": 10, "total": 10, "current_factory": None}},
            {"thread_id": "89", "status": "pending_review",
             "progress": {"done": 3, "total": 5, "current_factory": "山東中地"}},
        ]}

    def fake_status(args):
        calls["get_batch_status"].append(args)
        if args["thread_id"] == "89":
            return {"thread_id": "89", "status": "pending_review",
                    "progress": {"done": 3, "total": 5,
                                 "current_factory": "山東中地"},
                    "unprocessed_factories": ["正达"]}
        return {"error": f"批次不存在: {args['thread_id']}"}

    def fake_usage(args):
        calls["get_usage"].append(args)
        return {"calls": 42, "failed_calls": 1, "prompt_tokens": 10000,
                "completion_tokens": 2000, "total_tokens": 12000}

    monkeypatch.setattr(dispatcher_tools.TOOLS["list_batches"], "func", fake_list)
    monkeypatch.setattr(dispatcher_tools.TOOLS["get_batch_status"], "func", fake_status)
    monkeypatch.setattr(dispatcher_tools.TOOLS["get_usage"], "func", fake_usage)
    return calls


def test_fastpath_batch_list(mock_tools):
    for text in ["有哪些批次", "批次列表", "查看所有批次", "看看批次", "批次情况呢"]:
        r = fastpath.try_fastpath(text)
        assert r is not None, f"应命中: {text}"
        assert r["tool"] == "list_batches"
        assert "88" in r["message"] and "89" in r["message"]
        assert "待审核" in r["message"] and "3/5" in r["message"]
    assert len(mock_tools["list_batches"]) == 5


def test_fastpath_batch_status(mock_tools):
    for text in ["批次89的状态", "批次89状态", "89怎么样了", "批次89的进度"]:
        r = fastpath.try_fastpath(text)
        assert r is not None, f"应命中: {text}"
        assert r["tool"] == "get_batch_status"
        assert r["args"] == {"thread_id": "89"}
        assert "待审核" in r["message"] and "正达" in r["message"]


def test_fastpath_batch_status_not_found(mock_tools):
    """批次不存在 → 友好文案（意图明确，不回喂 LLM）。"""
    r = fastpath.try_fastpath("批次999的状态")
    assert r is not None and r["tool"] == "get_batch_status"
    assert "没有找到批次" in r["message"]


def test_fastpath_usage(mock_tools):
    for text in ["用量", "token消耗", "查一下用量", "用量统计"]:
        r = fastpath.try_fastpath(text)
        assert r is not None, f"应命中: {text}"
        assert r["tool"] == "get_usage"
        assert "42" in r["message"] and "12,000" in r["message"]


def test_fastpath_action_words_fall_through(mock_tools):
    """带动作词的句子一律 fall through（写操作/复杂意图不吃）。"""
    for text in ["把批次89重跑一下", "发起新批次", "删除批次89",
                 "批次89的审核", "看看批次然后重跑"]:
        assert fastpath.try_fastpath(text) is None, f"应放行: {text}"
    assert mock_tools["list_batches"] == []


def test_fastpath_long_sentence_fall_through(mock_tools):
    assert fastpath.try_fastpath("帮我看看目前有哪些批次是已经完成的状态") is None


def test_fastpath_no_batch_id_fall_through(mock_tools):
    for text in ["状态怎么样", "进度如何", "批次的状态"]:
        assert fastpath.try_fastpath(text) is None, f"应放行: {text}"


def test_fastpath_tool_error_falls_through(monkeypatch, mock_tools):
    """list_batches 执行异常 → 返回 None 交给 LLM（快路径绝不抛错）。"""
    monkeypatch.setattr(dispatcher_tools.TOOLS["list_batches"], "func",
                        lambda args: {"error": "boom"})
    assert fastpath.try_fastpath("有哪些批次") is None


def test_fastpath_empty_and_blank(mock_tools):
    assert fastpath.try_fastpath("") is None
    assert fastpath.try_fastpath("   ") is None
