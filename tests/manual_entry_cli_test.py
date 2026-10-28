# -*- coding: utf-8 -*-
"""run_cli 手动录入勾选逻辑测试（第七阶段 任务 B，§1.1 控制台入口）。

覆盖 scripts/run_cli.py 的 collect_manual_skips / _parse_skip_factories：
- --skip-factories 显式清单（解析、保序去重、不再提问）
- --skip-all-manual 全部跳过（预检全集 / 退化 hint 全集）
- 逐厂 y/N 交互：默认采纳预检建议（complete→Y / 否则→N），可改
- 预检失败降级为无建议；stdin EOF 按默认值走

precheck_fn/input_fn/print_fn 全部注入，不碰真服务/真终端/真 DB。

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/manual_entry_cli_test.py -q
    或直接：python3 tests/manual_entry_cli_test.py

隔离（血泪红线 2026-08-11）：run_cli import 链会触发 llm_client 的
load_dotenv(override=True)——先 import 再 isolate_to_tmp 复位环境；
跑 pytest 前另设 YAMATO_DOTENV_PATH 指向临时 .env。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# ---- env 前置（需在 import app 链之前）----
os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))


def _load_run_cli():
    """按路径加载 scripts/run_cli.py（非包模块，走 importlib）。"""
    spec = importlib.util.spec_from_file_location(
        "yamato_run_cli", APP_ROOT / "scripts" / "run_cli.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rc = _load_run_cli()

from _test_isolation import isolate_to_tmp  # noqa: E402

# import 完成后复位环境（load_dotenv override 已在 import 时执行完毕）
TMP = isolate_to_tmp("yamato_manual_entry_cli_test_")


def _precheck_report(*, a_complete: bool = True):
    """预检假数据：厂A 按参数完整、厂B 不完整。"""
    return {"factories": [
        {"name": "厂A", "complete": a_complete, "filled_rows": 3, "total_rows": 3},
        {"name": "厂B", "complete": False, "filled_rows": 1, "total_rows": 4},
    ]}


def _input_feeder(answers: list[str]):
    """按序喂回答的 input 替身；耗尽后抛 AssertionError（不该再问）。"""
    it = iter(answers)

    def _input(prompt: str = "") -> str:
        return next(it)

    return _input


def _boom_input(prompt: str = "") -> str:
    raise AssertionError("非交互路径不应提问")


# ---------- _parse_skip_factories ----------

def test_parse_skip_factories():
    assert rc._parse_skip_factories(" a, b ,,c ,a ") == ["a", "b", "c"]
    assert rc._parse_skip_factories(None) == []
    assert rc._parse_skip_factories("") == []
    print("  ✓ test_parse_skip_factories")


# ---------- 非交互路径 ----------

def test_explicit_skip_factories_wins():
    """--skip-factories 显式清单优先，不调预检、不提问。"""
    def _boom_precheck(folder_name):
        raise AssertionError("显式清单路径不应调预检")

    skips = rc.collect_manual_skips(
        folder_name="ETD0901",
        skip_factories="厂A, 厂C",
        precheck_fn=_boom_precheck,
        input_fn=_boom_input,
        print_fn=lambda *a: None,
    )
    assert skips == ["厂A", "厂C"]
    print("  ✓ test_explicit_skip_factories_wins")


def test_skip_all_with_precheck():
    """--skip-all-manual：工厂全集取自预检结果，不提问。"""
    skips = rc.collect_manual_skips(
        folder_name="ETD0901",
        skip_all=True,
        precheck_fn=lambda folder_name: _precheck_report(),
        input_fn=_boom_input,
        print_fn=lambda *a: None,
    )
    assert skips == ["厂A", "厂B"]
    print("  ✓ test_skip_all_with_precheck")


def test_skip_all_fallback_to_hint():
    """--skip-all-manual 无 folder_name：全集退化为 factories_hint。"""
    skips = rc.collect_manual_skips(
        skip_all=True,
        factories_hint=["山東中地"],
        input_fn=_boom_input,
        print_fn=lambda *a: None,
    )
    assert skips == ["山東中地"]
    print("  ✓ test_skip_all_fallback_to_hint")


# ---------- 交互路径（默认采纳预检建议） ----------

def test_interactive_defaults_follow_suggestion():
    """回车采纳建议：complete 的厂默认跳过，其余默认不跳。"""
    prompts = []

    def _input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""   # 两厂都回车

    skips = rc.collect_manual_skips(
        folder_name="ETD0901",
        precheck_fn=lambda folder_name: _precheck_report(),
        input_fn=_input,
        print_fn=lambda *a: None,
    )
    assert skips == ["厂A"]
    assert len(prompts) == 2
    assert "Y/n" in prompts[0] and "已填完整" in prompts[0]   # 建议跳过
    assert "y/N" in prompts[1]                                 # 建议不跳
    print("  ✓ test_interactive_defaults_follow_suggestion")


def test_interactive_override_suggestion():
    """用户可改：对建议跳过的厂答 n、对建议不跳的厂答 y。"""
    skips = rc.collect_manual_skips(
        folder_name="ETD0901",
        precheck_fn=lambda folder_name: _precheck_report(),
        input_fn=_input_feeder(["n", "y"]),
        print_fn=lambda *a: None,
    )
    assert skips == ["厂B"]
    print("  ✓ test_interactive_override_suggestion")


def test_interactive_eof_uses_default():
    """stdin EOF（管道/重定向）：按默认建议走，不炸。"""
    def _eof_input(prompt: str = "") -> str:
        raise EOFError

    skips = rc.collect_manual_skips(
        folder_name="ETD0901",
        precheck_fn=lambda folder_name: _precheck_report(),
        input_fn=_eof_input,
        print_fn=lambda *a: None,
    )
    assert skips == ["厂A"]
    print("  ✓ test_interactive_eof_uses_default")


def test_precheck_failure_degrades():
    """预检失败降级为无建议（默认全 N），工厂全集退化为 hint。"""
    def _bad_precheck(folder_name):
        raise ValueError("子文件夹不存在")

    skips = rc.collect_manual_skips(
        folder_name="nope",
        factories_hint=["山東中地"],
        precheck_fn=_bad_precheck,
        input_fn=_input_feeder(["y"]),
        print_fn=lambda *a: None,
    )
    assert skips == ["山東中地"]
    print("  ✓ test_precheck_failure_degrades")


def main():
    test_parse_skip_factories()
    test_explicit_skip_factories_wins()
    test_skip_all_with_precheck()
    test_skip_all_fallback_to_hint()
    test_interactive_defaults_follow_suggestion()
    test_interactive_override_suggestion()
    test_interactive_eof_uses_default()
    test_precheck_failure_degrades()
    print("\nmanual_entry_cli_test: PASS")


if __name__ == "__main__":
    main()


def test_all_manual_main_completes_without_review(monkeypatch, tmp_path, capsys):
    """全手动已完成：不调用识别审核、不要求主数据新增或 Approved。"""
    output = tmp_path / "filled.xlsx"
    output.touch()
    seen = {}

    def run(thread_id, **kwargs):
        seen.update(kwargs)
        return {"status": "completed", "final_output_path": str(output)}

    def unexpected(*args, **kwargs):
        raise AssertionError("全手动不应进入审核/主数据校验")

    monkeypatch.setattr(sys, "argv", ["run_cli.py", "--skip-all-manual"])
    monkeypatch.setattr(rc.service, "run_until_interrupt", run)
    monkeypatch.setattr(rc.service, "get_order_state", lambda tid: {"values": {
        "pending_factories": [], "validation_status": "Pending"}})
    monkeypatch.setattr(rc.service, "resume_order", unexpected)
    monkeypatch.setattr(rc, "simulate_human", unexpected)
    monkeypatch.setattr(rc, "get_session", unexpected)
    rc.main()
    assert seen["manual_skip_factories"] == [rc.TARGET_FACTORY]
    assert "全部手动录入，跳过识别审核环节" in capsys.readouterr().out
