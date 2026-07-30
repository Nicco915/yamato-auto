# -*- coding: utf-8 -*-
"""知识库冷启动同步：代码 KB + 扩展文件（kb_extension.json）→ Pinecone。

幂等：ID 为稳定主键（guide.<key> / issue.<TYPE>），重跑只覆盖不重复。
两个来源自动合并：代码 KB 优先（extension 不覆盖同名 key），KB 变更后
直接重跑本脚本即可（rag设计.md 写入时机铁律①）。

用法：
  # 真实灌库（需 .env 配好 PINECONE_API_KEY / EMBEDDING_API_KEY）
  python3 scripts/sync_kb.py

  # 无 key 冒烟（内存假索引，验证链路）
  RAG_MOCK=1 python3 scripts/sync_kb.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from app.dispatcher import rag  # noqa: E402
from app.dispatcher.explain import ISSUE_KB  # noqa: E402
from app.dispatcher.guide import GUIDE_KB  # noqa: E402


def build_guide_entries() -> list[dict]:
    """GUIDE_KB + kb_extension.json → upsert 条目（代码 KB 优先）。"""
    ext = rag.load_extension()
    merged: dict = {}
    # 扩展文件先放（低优先级），代码 KB 后覆盖（高优先级）
    for key, e in ext.get("guide", {}).items():
        merged[key] = e
    for key, e in GUIDE_KB.items():
        merged[key] = e

    entries = []
    for key, e in merged.items():
        keywords = e.get("keywords", [])
        entries.append({
            "id": f"guide.{key}",
            "text": f"{e['title']}\n{'、'.join(keywords)}\n{e['content']}",
            "metadata": {
                "title": e["title"],
                "content": e["content"],
                "priority": e.get("priority", 99),
                "keywords": keywords,
            },
        })
    return entries


def build_issue_entries() -> list[dict]:
    """ISSUE_KB + kb_extension.json → upsert 条目（代码 KB 优先）。

    suggest 是结构化 list，Pinecone metadata 只支持标量/字符串列表，
    序列化为 suggest_json，检索侧由 _issue_entry_from_metadata 还原。
    """
    ext = rag.load_extension()
    merged: dict = {}
    for key, e in ext.get("issue", {}).items():
        merged[key] = e
    for key, e in ISSUE_KB.items():
        merged[key] = e

    entries = []
    for type_, e in merged.items():
        entries.append({
            "id": f"issue.{type_}",
            "text": f"{e['title']}\n{e['explain']}",
            "metadata": {
                "title": e["title"],
                "explain": e["explain"],
                "severity": e.get("severity", "mid"),
                "suggest_json": json.dumps(
                    e.get("suggest", []), ensure_ascii=False,
                ),
            },
        })
    return entries


def main() -> int:
    guide_entries = build_guide_entries()
    issue_entries = build_issue_entries()
    ext = rag.load_extension()
    ext_guide = len(ext.get("guide", {}))
    ext_issue = len(ext.get("issue", {}))
    if ext_guide or ext_issue:
        print(f"扩展文件：guide +{ext_guide} 条，issue +{ext_issue} 条")
    print(f"待同步：guide {len(guide_entries)} 条，issue {len(issue_entries)} 条")

    ok_g = rag.upsert_entries("guide", guide_entries)
    print(f"  guide namespace: {'✓' if ok_g else '✗（见警告日志）'}")
    ok_i = rag.upsert_entries("issue", issue_entries)
    print(f"  issue namespace: {'✓' if ok_i else '✗（见警告日志）'}")

    if ok_g and ok_i:
        print("同步完成（幂等，可重复执行）")
        return 0
    print("同步失败：检查 PINECONE_API_KEY / EMBEDDING_API_KEY 配置")
    return 1


if __name__ == "__main__":
    sys.exit(main())
