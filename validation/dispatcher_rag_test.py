# -*- coding: utf-8 -*-
"""RAG 知识库检索测试（RAG_MOCK=1，内存假索引 + bigram 哈希向量，无需 API key）。

覆盖：
1. V1 行为不变：backend 关闭时 _search_kb 纯关键词匹配（命中/兜底）；
2. 冷启动同步：sync_kb 灌库 guide/issue 两 namespace；
3. V2 语义命中：guide 自由文本问题命中正确条目；
4. V2 未知 issue type：向量检索命中最近邻条目（V1 只能给通用模板）；
5. 已知 issue type：仍走精确查表，不被向量路径影响；
6. 降级：embed 失败时 _search_kb 自动回落关键词匹配；
7. 幂等：sync 重跑两次，namespace 条目数不变；
8. 待策展队列：未命中问题写入 + 读写 + 移除 rag_curation_queue.jsonl；
9. 扩展文件合并同步：kb_extension.json 条目灌库后出现在索引中；
10. 未知 issue type 采集：回落通用模板时记入策展队列；
11. curate_kb preview：聚类 + 去重预览。

注意：mock 向量是字符 bigram 哈希（非真语义），可分性差，测试内把
rag_min_score 降到 0.1；真实 Qwen3-Embedding 用默认 0.5。

用法：
  python3 validation/dispatcher_rag_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

os.environ["RAG_MOCK"] = "1"  # 须在 import rag 之前设置

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from app.config import get_settings  # noqa: E402
from app.dispatcher import rag  # noqa: E402
from app.dispatcher import explain, guide  # noqa: E402

_QUEUE_PATH = APP_ROOT / "app" / "data" / "rag_curation_queue.jsonl"
_EXT_PATH = APP_ROOT / "app" / "data" / "kb_extension.json"


def _load_sync_script():
    """以模块方式加载 scripts/sync_kb.py（scripts 非包，走文件加载）。"""
    spec = importlib.util.spec_from_file_location(
        "sync_kb", APP_ROOT / "scripts" / "sync_kb.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case_1_v1_keyword_mode() -> None:
    """V1 行为不变：backend 关闭时纯关键词匹配。"""
    real = rag.backend_enabled
    rag.backend_enabled = lambda: False
    try:
        hits = guide._search_kb("怎么重跑批次")
        keys = [k for k, _ in hits]
        assert "rerun_batch" in keys, f"关键词命中失败: {keys}"
        print("  ✓ 关键词命中 rerun_batch")

        hits = guide._search_kb("完全不相关的量子力学问题xyz")
        assert hits[0][0] == "_generic", f"兜底失败: {hits[0][0]}"
        print("  ✓ 未命中走通用模板")
    finally:
        rag.backend_enabled = real


def case_2_sync_and_guide_hit() -> None:
    """冷启动同步 + V2 语义命中 guide 条目。"""
    get_settings().rag_min_score = 0.1  # mock 向量可分性差，见模块 docstring
    sync = _load_sync_script()
    rc = sync.main()
    assert rc == 0, "sync_kb 灌库失败"
    assert len(rag._MOCK_INDEX.namespaces["guide"]) == len(guide.GUIDE_KB)
    assert len(rag._MOCK_INDEX.namespaces["issue"]) == len(explain.ISSUE_KB)
    print(f"  ✓ 灌库 guide={len(guide.GUIDE_KB)} issue={len(explain.ISSUE_KB)}")

    hits = guide._search_kb("重跑批次怎么操作")
    keys = [k for k, _ in hits]
    assert "rerun_batch" in keys, f"向量检索未命中 rerun_batch: {keys}"
    entry = dict(hits)[ "rerun_batch"]
    assert entry["title"] == guide.GUIDE_KB["rerun_batch"]["title"]
    print("  ✓ 语义命中 rerun_batch，metadata 正确还原")


def case_3_unknown_issue_type() -> None:
    """未知 issue type：RAG 命中最近邻条目（V1 只能给通用模板）。"""
    kb_map = explain._search_issue_kb(["缺少装箱单"])  # 未收录 type
    entry = kb_map["缺少装箱单"]
    assert entry is not explain._GENERIC_KB, "未知 type 未走向量检索"
    assert entry["title"] == explain.ISSUE_KB["NO_PACKING_LIST"]["title"]
    assert isinstance(entry["suggest"], list) and entry["suggest"], \
        "suggest_json 还原失败"
    print("  ✓ 未知 type 命中 NO_PACKING_LIST，suggest_json 还原正确")


def case_4_known_issue_type_exact() -> None:
    """已知 issue type：仍走精确查表，不受向量路径影响。"""
    kb_map = explain._search_issue_kb(["CHANNEL_ERROR"])
    assert kb_map["CHANNEL_ERROR"] is explain.ISSUE_KB["CHANNEL_ERROR"]
    print("  ✓ 已知 type 精确查表（同一对象）")


def case_5_fallback_on_embed_failure() -> None:
    """降级：embed 失败时自动回落关键词匹配。"""
    rag._cached_query_embed.cache_clear()
    real = rag.embed_texts
    rag.embed_texts = lambda texts: None  # 模拟 API 故障
    try:
        hits = guide._search_kb("怎么重跑批次")
        keys = [k for k, _ in hits]
        assert "rerun_batch" in keys, f"embed 故障后未回落关键词: {keys}"
        print("  ✓ embed 故障 → 回落关键词命中 rerun_batch")
    finally:
        rag.embed_texts = real
        rag._cached_query_embed.cache_clear()


def case_6_sync_idempotent() -> None:
    """幂等：sync 重跑，条目数不变。"""
    sync = _load_sync_script()
    before = len(rag._MOCK_INDEX.namespaces["guide"])
    rc = sync.main()
    after = len(rag._MOCK_INDEX.namespaces["guide"])
    assert rc == 0 and before == after, f"幂等失败: {before} → {after}"
    print(f"  ✓ 重跑后 guide 条目数不变（{after}）")


def case_7_curation_queue() -> None:
    """待策展队列：未命中问题写入 jsonl。"""
    # 用 ASCII 乱码问题：与中文条目无 bigram 重叠，任何阈值下都不会命中
    question = "zzqqxxnoresult777"
    hits = guide._search_kb(question)
    assert hits[0][0] == "_generic", f"乱码问题应走通用模板，实际: {hits[0][0]}"
    assert _QUEUE_PATH.exists(), "策展队列文件未生成"
    lines = _QUEUE_PATH.read_text(encoding="utf-8").splitlines()
    matched = [l for l in lines
               if json.loads(l)["question"] == question]
    assert matched, "问题未写入策展队列"
    print("  ✓ 未命中问题已写入 rag_curation_queue.jsonl")


def case_8_curation_read_remove() -> None:
    """策展队列读写 + 移除：read_curation_queue / remove_from_queue。"""
    # 清理旧数据，写入两条测试问题
    if _QUEUE_PATH.exists():
        _QUEUE_PATH.unlink()
    rag.log_curation("curation-read-test-qa", source="guide")
    rag.log_curation("curation-read-test-qb", source="issue")

    items = rag.read_curation_queue(10)
    assert len(items) >= 2, f"read_curation_queue 返回不足: {len(items)}"
    questions = {it["question"] for it in items}
    assert "curation-read-test-qa" in questions
    assert "curation-read-test-qb" in questions
    print("  ✓ 写入并读取策展队列")

    rag.remove_from_queue({"curation-read-test-qa"})
    items2 = rag.read_curation_queue(10)
    q2 = {it["question"] for it in items2}
    assert "curation-read-test-qa" not in q2, "移除失败"
    assert "curation-read-test-qb" in q2, "误删了不该删的"
    print("  ✓ 按 question 移除（另一条保留）")


def case_9_extension_merge_sync() -> None:
    """kb_extension.json 合并同步：扩展条目出现在灌库后。"""
    # 写入一条测试扩展
    rag.save_extension({
        "guide": {"test_ext_guide": {
            "keywords": ["测试扩展"], "title": "测试扩展条目",
            "content": "这是一条测试扩展内容", "priority": 5,
        }},
        "issue": {},
    })

    sync = _load_sync_script()
    rc = sync.main()
    assert rc == 0, "合并同步失败"

    # 扩展条目应出现在索引中
    assert "guide.test_ext_guide" in rag._MOCK_INDEX.namespaces["guide"], \
        "扩展条目未灌入索引"
    # 原有条目不丢
    assert "guide.beginner_flow" in rag._MOCK_INDEX.namespaces["guide"], \
        "原有条目丢失"
    print("  ✓ 扩展条目合并灌库，原有条目不受影响")


def case_10_issue_curation_logging() -> None:
    """未知 issue type 回落通用模板时记入策展队列。"""
    backup_min_score = get_settings().rag_min_score
    get_settings().rag_min_score = 0.99  # mock 下确保不误命中
    q = "zzzz-issue-collect-test-unknown-type-xyz"
    try:
        hits = rag.read_curation_queue(200)
        before = len([it for it in hits if it["question"] == q])

        explain._search_issue_kb([q])  # 未收录 type，应触发 log_curation

        hits2 = rag.read_curation_queue(200)
        after = len([it for it in hits2 if it["question"] == q])
        assert after > before, f"未知 issue type 未记入策展队列: {before} → {after}"
        print(f"  ✓ 未知 issue type 记入策展队列（{before} → {after}）")
    finally:
        get_settings().rag_min_score = backup_min_score


def case_11_curate_preview() -> None:
    """curate_kb preview：聚类 + 去重预览。"""
    from app.dispatcher.tools import _preview_curate_kb

    # 写入几条相似问题（共享前缀 bigram 确保 mock 向量聚类）
    for q in ["curate-test-AAAAA-如何查看SKU提取结果",
              "curate-test-AAAAA-怎么看到SKU数据结果",
              "curate-test-ZZZZZ-完全不相关的乱码xyz"]:
        rag.log_curation(q, source="guide")

    pv = _preview_curate_kb({"max_items": 50})
    assert "lines" in pv and "summary" in pv
    assert pv["summary"], "preview summary 为空"
    assert len(pv["lines"]) > 0, "preview lines 为空"
    # 相似问题应聚为一簇
    combined = "\n".join(pv["lines"])
    assert "出现 2" in combined, \
        f"相似问题未聚类: {combined}"
    print("  ✓ curate_kb preview 聚类正常")


def main() -> None:
    # 快照策展队列 + 扩展文件，测试结束后原样恢复
    queue_backup = (_QUEUE_PATH.read_text(encoding="utf-8")
                    if _QUEUE_PATH.exists() else None)
    ext_backup = (_EXT_PATH.read_text(encoding="utf-8")
                  if _EXT_PATH.exists() else None)
    cases = [
        ("V1 关键词模式不变", case_1_v1_keyword_mode),
        ("冷启动同步 + guide 语义命中", case_2_sync_and_guide_hit),
        ("未知 issue type 向量命中", case_3_unknown_issue_type),
        ("已知 issue type 精确查表", case_4_known_issue_type_exact),
        ("embed 故障降级回落", case_5_fallback_on_embed_failure),
        ("同步幂等", case_6_sync_idempotent),
        ("待策展队列写入", case_7_curation_queue),
        ("策展队列读写+移除", case_8_curation_read_remove),
        ("扩展文件合并同步", case_9_extension_merge_sync),
        ("未知 issue type 采集", case_10_issue_curation_logging),
        ("curate_kb preview 聚类", case_11_curate_preview),
    ]
    passed = 0
    try:
        for name, fn in cases:
            print(f"[{passed + 1}/{len(cases)}] {name}")
            fn()
            passed += 1
        print(f"\n{passed}/{len(cases)} 通过")
    finally:
        if queue_backup is None:
            _QUEUE_PATH.unlink(missing_ok=True)
        else:
            _QUEUE_PATH.write_text(queue_backup, encoding="utf-8")
        if ext_backup is None:
            _EXT_PATH.unlink(missing_ok=True)
        else:
            _EXT_PATH.write_text(ext_backup, encoding="utf-8")


if __name__ == "__main__":
    main()
