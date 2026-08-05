# -*- coding: utf-8 -*-
"""会话缓存新鲜度校验测试（方案C兜底，2026-08-05）。

背景：会话缓存落在 data/sessions/{工厂名}.json，只按工厂名命名、无批次维度。
第一批次残留的 JSON 里 targets[].path / items[].source_file 全是上一批次
路径，第二批次（upstream_root 可能已换目录）启动时 Node3 直接命中旧缓存，
导致全厂误用上一批次数据。方案C 在缓存命中前校验路径证据新鲜度。

覆盖（全程临时目录构造假 session JSON / 假上游目录，绝不碰真实
app/data/sessions；_SESSIONS_DIR 为模块级常量，逐用例 patch 到临时目录）：
1. 缓存路径在新 root 下且文件存在 → 命中；
2. 缓存路径在旧 root（不在新 root 下）→ 返回 None；
3. 无任何路径证据 → 返回 None；
4. upstream_root=None → 老行为（不校验，直接命中）；
5. source_file 为 "mock"/"no_folder_matched" 等非路径标记 → 不误判；
6. 缓存路径在 root 下但文件已删 → 返回 None（宁可重提）；
7. 证据优先级：targets[].path 优先于 source_file（targets 有可用路径时
   不看 source_file）；targets 为空时回落 source_file。

用法（在 app/ 目录下）：
  python3 validation/session_staleness_test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 提取走 mock：避免 import 提取线（llm_client 的 load_dotenv 会 override
# 环境变量，血泪红线见 validation/_test_isolation.py）；本测试只直接调用
# _try_load_cached_session，不碰 db / graph，无需 isolate_to_tmp
os.environ["EXTRACTION_MOCK"] = "1"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))

from app.nodes import extraction_node  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="yamato_session_staleness_test_"))
REAL_SESSIONS_DIR = (APP_ROOT / "data" / "sessions").resolve()

# 假上游目录：旧批次 root（第一批次的工厂目录）与新批次 root
OLD_ROOT = TMP / "旧批次上游"
NEW_ROOT = TMP / "新批次上游"
for root in (OLD_ROOT, NEW_ROOT):
    (root / "测试厂").mkdir(parents=True)

# 旧批次遗留的真实文件与新批次同名文件
OLD_FILE = OLD_ROOT / "测试厂" / "箱单.xlsx"
OLD_FILE.write_bytes(b"old")
NEW_FILE = NEW_ROOT / "测试厂" / "箱单.xlsx"
NEW_FILE.write_bytes(b"new")

SESS = TMP / "sessions"
SESS.mkdir()


def setup_module_dir() -> None:
    """把 extraction_node._SESSIONS_DIR patch 到临时目录并加守卫断言。"""
    extraction_node._SESSIONS_DIR = SESS
    assert SESS.resolve() != REAL_SESSIONS_DIR, "sessions 目录隔离失败，中止"


def _write_cache(factory: str, data: dict) -> Path:
    path = SESS / f"{factory}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _item(source_file: str) -> dict:
    return {"sku_code": "4900000000001", "source_file": source_file}


def case_1_fresh_hit() -> None:
    """缓存路径在新 root 下且文件存在 → 命中。"""
    _write_cache("厂1", {
        "items": {"S1": _item(str(NEW_FILE))},
        "no_code_items": [],
        "targets": [{"path": str(NEW_FILE), "barcodes": ["4900000000001"]}],
    })
    got = extraction_node._try_load_cached_session("厂1", str(NEW_ROOT))
    assert got is not None, "新 root 下的缓存应命中"
    assert got["items"]["S1"]["source_file"] == str(NEW_FILE)
    print("  ✓ 缓存路径在新 root 下且文件存在 → 命中")


def case_2_stale_old_root() -> None:
    """缓存路径在旧 root（不在新 root 下）→ 返回 None。"""
    _write_cache("厂2", {
        "items": {"S1": _item(str(OLD_FILE))},
        "no_code_items": [],
        "targets": [{"path": str(OLD_FILE), "barcodes": ["4900000000001"]}],
    })
    got = extraction_node._try_load_cached_session("厂2", str(NEW_ROOT))
    assert got is None, "旧 root 的缓存应判陈旧返回 None"
    print("  ✓ 缓存路径在旧 root → 返回 None（弃缓存重提）")


def case_3_no_evidence() -> None:
    """无任何路径证据（items 有数据但 source_file 全为标记、无 targets）→ None。"""
    _write_cache("厂3", {
        "items": {"S1": _item("extraction_error: boom")},
        "no_code_items": [],
        "targets": [],
    })
    got = extraction_node._try_load_cached_session("厂3", str(NEW_ROOT))
    assert got is None, "无路径证据应判陈旧返回 None"
    print("  ✓ 无任何路径证据 → 返回 None")


def case_4_none_upstream_legacy() -> None:
    """upstream_root=None → 老行为：不校验，即使路径在旧 root 也命中。"""
    _write_cache("厂4", {
        "items": {"S1": _item(str(OLD_FILE))},
        "no_code_items": [],
        "targets": [{"path": str(OLD_FILE), "barcodes": []}],
    })
    got = extraction_node._try_load_cached_session("厂4")
    assert got is not None, "upstream_root=None 应保持老行为直接命中"
    got2 = extraction_node._try_load_cached_session("厂4", None)
    assert got2 is not None, "显式 None 同样不校验"
    print("  ✓ upstream_root=None → 老行为（不校验，直接命中）")


def case_5_non_path_markers() -> None:
    """source_file 为 mock/no_folder_matched 等标记时不误判为路径证据。

    标记不是路径 → 不构成"在 root 下"的证据；此时缓存无可用证据 → None，
    绝不能因标记字符串凑巧命中而放行。
    """
    for marker in ("mock", "no_folder_matched", "no_items_extracted"):
        _write_cache("厂5", {
            "items": {"S1": _item(marker)},
            "no_code_items": [{"sku_code": "", "source_file": marker}],
            "targets": [],
        })
        got = extraction_node._try_load_cached_session("厂5", str(NEW_ROOT))
        assert got is None, f"标记 {marker!r} 不应被当作路径证据"
        # upstream_root=None 时老行为仍命中（兼容性）
        assert extraction_node._try_load_cached_session("厂5") is not None
    print("  ✓ mock/no_folder_matched/no_items_extracted 标记不误判为路径")


def case_6_under_root_but_deleted() -> None:
    """缓存路径在 root 下但文件已删 → 返回 None（存在性检查不通过）。"""
    ghost = NEW_ROOT / "测试厂" / "已删除的箱单.xlsx"
    _write_cache("厂6", {
        "items": {"S1": _item(str(ghost))},
        "no_code_items": [],
        "targets": [{"path": str(ghost), "barcodes": []}],
    })
    got = extraction_node._try_load_cached_session("厂6", str(NEW_ROOT))
    assert got is None, "root 下但文件已删应判陈旧返回 None"
    print("  ✓ 路径在 root 下但文件已删 → 返回 None")


def case_7_evidence_priority() -> None:
    """证据优先级：targets[].path 优先；targets 无可用路径时回落 source_file。"""
    # 7a. targets 指向新 root（新鲜）但 source_file 指向旧 root → 仍命中
    #     （targets 有可用路径时不看 source_file）
    _write_cache("厂7a", {
        "items": {"S1": _item(str(OLD_FILE))},
        "no_code_items": [],
        "targets": [{"path": str(NEW_FILE), "barcodes": []}],
    })
    got = extraction_node._try_load_cached_session("厂7a", str(NEW_ROOT))
    assert got is not None, "targets[].path 新鲜即命中，不看 source_file"

    # 7b. targets 为空，source_file 在新 root 下且存在 → 回落 source_file 命中
    _write_cache("厂7b", {
        "items": {"S1": _item(str(NEW_FILE))},
        "no_code_items": [],
        "targets": [],
    })
    got = extraction_node._try_load_cached_session("厂7b", str(NEW_ROOT))
    assert got is not None, "targets 为空应回落 source_file 判定"

    # 7c. targets 为空，no_code_items 的 source_file 在新 root 下 → 也作证据
    _write_cache("厂7c", {
        "items": {},
        "no_code_items": [{"sku_code": "", "source_file": str(NEW_FILE)}],
        "targets": [],
    })
    got = extraction_node._try_load_cached_session("厂7c", str(NEW_ROOT))
    assert got is not None, "no_code_items[*].source_file 也应作为回落证据"
    print("  ✓ 证据优先级：targets[].path 优先，空时回落 items/no_code_items 的 "
          "source_file")


CASES = [
    ("1. 缓存路径在新 root 下且文件存在 → 命中", case_1_fresh_hit),
    ("2. 缓存路径在旧 root → 返回 None", case_2_stale_old_root),
    ("3. 无任何路径证据 → 返回 None", case_3_no_evidence),
    ("4. upstream_root=None → 老行为直接命中", case_4_none_upstream_legacy),
    ("5. 非路径标记不误判", case_5_non_path_markers),
    ("6. root 下但文件已删 → 返回 None", case_6_under_root_but_deleted),
    ("7. 证据优先级 targets[].path > source_file", case_7_evidence_priority),
]


def main() -> int:
    setup_module_dir()
    results: list[tuple[str, bool, str]] = []
    for name, fn in CASES:
        print(f"===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
            results.append((name, False, f"{type(e).__name__}: {e}"))
        else:
            print(f"[PASS] {name}")
            results.append((name, True, ""))
        print()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"===== 总结：{passed}/{len(results)} 通过 =====")
    for name, ok, err in results:
        if not ok:
            print(f"  [FAIL] {name}: {err}")
    if passed == len(results):
        print("🎉 会话缓存新鲜度校验全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
