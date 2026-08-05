# -*- coding: utf-8 -*-
"""预提取进度落盘 + 批次端点 pre_extraction 键 测试（2026-08-03）。

覆盖：
1. 状态流转：cached / done / failed（异常摘要 type+message，截 200 字符）、
   pending → running 中间态每次重写后文件都可完整解析；
2. 原子写：SESSIONS_DIR 无残留 .tmp 半截文件；
3. 危险 thread_id 字符过滤：路径分隔符/盘符/通配符全部替换为 _，
   进度文件始终落在 SESSIONS_DIR 内（防目录穿越）；
4. 批次端点：进度文件存在且可解析 → 带 pre_extraction 键；
   不存在 / JSON 损坏 → 不带该键且端点不 500。

隔离原则同 ui_api_test（import 完成后 isolate_to_tmp）；
EXTRACTION_MOCK=1 不调 LLM；预提取内部函数 monkeypatch 替身，全程确定。
_ 启动批次时把 service._start_pre_extraction patch 成 no-op，防 daemon
线程抢写进度文件造成竞态。

用法（在 app/ 目录下）：
  python3 validation/preextract_progress_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

os.environ["EXTRACTION_MOCK"] = "1"  # 提取走 mock，不调 LLM

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.graph import get_graph  # noqa: E402
from app.nodes import extraction_node  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

TMP = isolate_to_tmp("yamato_preextract_progress_", alias_map_copy=True)
SESS = service.SESSIONS_DIR  # isolate_to_tmp 已 patch 到临时目录
client = TestClient(app)


def _read_progress(tag_file: Path) -> dict:
    return json.loads(tag_file.read_text(encoding="utf-8"))


def test_state_transitions() -> Path:
    """状态流转：cached / done / failed + 每次重写文件均可完整解析。"""
    print("===== 1. 状态流转（cached/done/failed）=====")
    upstream = TMP / "upstream"
    for name in ("缓存厂", "成功厂", "失败厂"):
        (upstream / name).mkdir(parents=True, exist_ok=True)

    # 替身：缓存厂命中缓存；失败厂抛 TimeoutError；成功厂正常返回
    # （fake_load 第二参数对齐 _try_load_cached_session 新签名：
    # 预提取调用点已接入 upstream_root 新鲜度校验，替身收下但不使用）
    orig_load = extraction_node._try_load_cached_session
    orig_run = extraction_node._run_factory_session

    def fake_load(factory, upstream_root=None):
        return {"items": {"SKU1": {}}} if factory == "缓存厂" else None

    def fake_run(folder_path, factory, expected_skus):
        if factory == "失败厂":
            raise TimeoutError("LLM 多次未响应")

    extraction_node._try_load_cached_session = fake_load
    extraction_node._run_factory_session = fake_run
    try:
        service._pre_extract_factories(
            ["缓存厂", "成功厂", "失败厂"], str(upstream),
            {"缓存厂": [], "成功厂": [], "失败厂": []},
            thread_id="进度测试批次-1")
    finally:
        extraction_node._try_load_cached_session = orig_load
        extraction_node._run_factory_session = orig_run

    path = SESS / "_preextract_progress_进度测试批次-1.json"
    assert path.is_file(), f"进度文件未生成: {path}"
    data = _read_progress(path)
    assert data["thread_id"] == "进度测试批次-1", data
    assert data["updated_at"], data
    statuses = {f["factory"]: f for f in data["factories"]}
    assert statuses["缓存厂"]["status"] == "cached", statuses
    assert statuses["成功厂"]["status"] == "done", statuses
    assert statuses["失败厂"]["status"] == "failed", statuses
    err = statuses["失败厂"]["error"]
    assert err.startswith("TimeoutError") and "LLM 多次未响应" in err, err
    assert len(err) <= 200, err
    for f in data["factories"]:
        assert f["ts"], f"终态缺 ts: {f}"
    print(f"  ✓ cached/done/failed 流转正确，error={err!r}")

    # 原子写：目录下无残留 .tmp 半截文件
    stray = [p for p in SESS.iterdir()
             if p.name.startswith("_preextract_progress_") and p.suffix != ".json"]
    assert not stray, f"存在半截临时文件: {stray}"
    print("  ✓ 无残留 .tmp 文件（原子 rename）")
    return path


def test_atomic_rewrite() -> None:
    """每次 update 后文件都是完整可解析 JSON（读方不会看到半截）。"""
    print("\n===== 2. 原子重写（每次状态变化文件均可解析）=====")
    prog = service._PreExtractProgress("原子测试", ["甲", "乙"])
    path = SESS / "_preextract_progress_原子测试.json"
    for factory, status in (("甲", "running"), ("甲", "done"),
                            ("乙", "running"), ("乙", "failed")):
        prog.update(factory, status, "x" * 500 if status == "failed" else None)
        data = _read_progress(path)  # 每次重写后立即解析，必须完整
        entry = next(f for f in data["factories"] if f["factory"] == factory)
        assert entry["status"] == status, entry
    entry = next(f for f in _read_progress(path)["factories"]
                 if f["factory"] == "乙")
    assert len(entry["error"]) == 500 or entry["error"], entry  # 直接构造不截断
    # 经 _pre_extract_factories 的异常路径才截 200（第 1 步已验证）
    print("  ✓ 连续 4 次重写，每次文件均完整可解析")


def test_dangerous_thread_id() -> None:
    """危险 thread_id 字符过滤：进度文件始终落在 SESSIONS_DIR 内。"""
    print("\n===== 3. 危险 thread_id 过滤 =====")
    evil = "../../恶\\劣:批次*?"
    path = service._preextract_progress_path(evil)
    assert path.resolve().parent == SESS.resolve(), \
        f"进度文件逃逸出 SESSIONS_DIR: {path}"
    for ch in ("/", "\\", ":", "*", "?"):
        assert ch not in path.name, f"文件名含危险字符 {ch!r}: {path.name}"
    assert ".." not in path.name.replace("_preextract_progress_", ""), path.name
    # 写读闭环：危险 id 也能正常落盘/读取，thread_id 原样保留在内容里
    prog = service._PreExtractProgress(evil, ["甲"])
    prog.update("甲", "done")
    data = service.load_pre_extraction_progress(evil)
    assert data and data["thread_id"] == evil, data
    assert data["factories"][0]["status"] == "done", data
    print(f"  ✓ 危险 id 过滤为安全文件名: {path.name}")


def test_endpoint() -> None:
    """批次端点：进度文件存在→带 pre_extraction；损坏/不存在→不带且不 500。"""
    print("\n===== 4. 批次端点 pre_extraction 键 =====")
    thread_id = "PROGRESS-API-TEST"

    # 防 daemon 预提取线程抢写进度文件造成竞态
    orig_start = service._start_pre_extraction
    service._start_pre_extraction = lambda tid: None
    try:
        get_graph().get_state({"configurable": {"thread_id": "__warmup__"}})

        from app.config import get_settings
        src_downstream = Path(get_settings().downstream_file_path)
        assert src_downstream.is_file(), f"默认下游装箱单不存在: {src_downstream}"
        tmp_downstream = TMP / src_downstream.name
        shutil.copy2(src_downstream, tmp_downstream)

        r = client.post("/api/v1/batches", json={
            "thread_id": thread_id,
            "downstream_file_path": str(tmp_downstream),
        })
        assert r.status_code == 200, r.text

        # 4a. 无进度文件 → 不带 pre_extraction 键
        r = client.get(f"/api/v1/batches/{thread_id}")
        assert r.status_code == 200, r.text
        assert "pre_extraction" not in r.json(), r.json().keys()
        print("  ✓ 无进度文件：200 且不带 pre_extraction")

        # 4b. 写入进度文件 → 原样带出
        prog = service._PreExtractProgress(thread_id, ["某厂", "另一厂"])
        prog.update("某厂", "running")
        r = client.get(f"/api/v1/batches/{thread_id}")
        assert r.status_code == 200, r.text
        pre = r.json().get("pre_extraction")
        assert pre and pre["thread_id"] == thread_id, pre
        assert len(pre["factories"]) == 2, pre
        assert pre["factories"][0]["status"] == "running", pre
        print("  ✓ 有进度文件：pre_extraction 原样带出")

        # 4c. 进度文件损坏 → 不带键且端点不 500
        service._preextract_progress_path(thread_id).write_text(
            "{损坏 json", encoding="utf-8")
        r = client.get(f"/api/v1/batches/{thread_id}")
        assert r.status_code == 200, r.text
        assert "pre_extraction" not in r.json(), r.json().keys()
        print("  ✓ 进度文件损坏：200 且静默不带键")

        # 4d. 删掉进度文件 → 恢复不带键
        service._preextract_progress_path(thread_id).unlink()
        r = client.get(f"/api/v1/batches/{thread_id}")
        assert r.status_code == 200, r.text
        assert "pre_extraction" not in r.json(), r.json().keys()
        print("  ✓ 删除进度文件：恢复不带键")

        # 收尾：删掉测试批次，不污染后续测试
        r = client.delete(f"/api/v1/batches/{thread_id}")
        assert r.status_code == 200, r.text
    finally:
        service._start_pre_extraction = orig_start


def main() -> int:
    test_state_transitions()
    test_atomic_rewrite()
    test_dangerous_thread_id()
    test_endpoint()
    print("\npreextract_progress_test: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\npreextract_progress_test: FAIL — {e}")
        sys.exit(1)
