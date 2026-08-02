# -*- coding: utf-8 -*-
"""W1 审核页白名单自动刷新测试（问题2：改路径后单据 403）。

覆盖：
1. 初始 root=A：A 内单据放行（200）；
2. apply_paths 改 root=B（临时 .env）后：A 403 / B 200（refresh_review_roots
   立即生效）+ .env 已写 B + .bak 备份；
3. 模拟手工改 .env（os.environ + cache_clear，不调 refresh）：下次请求
   _auto_refresh_roots 自动感知 → C 200 / B 403；
4. 批次级 root 二级兜底：全局 root=C 时，带 checkpoint state 里 root=A 的
   thread_id 放行 A 内单据；带别的 thread_id 403；
5. 负例：白名单外路径 403 / 路径穿越 403 / 白名单内不存在文件 404 /
   不支持扩展名 415。

隔离（血泪红线）：checkpoint/master db、output、sessions 全部指向临时目录
（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。UPSTREAM_ROOT 也指向临时目录 A/B/C，
全程不碰真实工厂目录（只读都不需要）。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 python3 validation/whitelist_refresh_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("EXTRACTION_MOCK", "1")

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import agent_chat  # noqa: E402
from app.api import service  # noqa: E402
from app.api.main import app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.review import router as review_router  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 夹具：三个候选 root，各放一份假单据（FileResponse 不校验内容）----
BASE = Path(tempfile.mkdtemp(prefix="yamato_wl_fixture_"))
ROOT_A = BASE / "rootA"
ROOT_B = BASE / "rootB"
ROOT_C = BASE / "rootC"
for d in (ROOT_A, ROOT_B, ROOT_C):
    d.mkdir(parents=True)
    (d / "单据.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）；
# 初始白名单 root=A（随 settings.upstream_root）----
TMP = isolate_to_tmp("yamato_wl_test_", extra_env={"UPSTREAM_ROOT": str(ROOT_A)})

client = TestClient(app)

# 真实下游装箱单（只读）：批次级兜底用例要建一个 root=A 的真实挂起批次
DOWNSTREAM = os.environ.get(
    "YAMATO_TEST_DOWNSTREAM",
    "/Users/nz/Downloads/yamato/96/ContentsOfTheContainer_202624_青島XD_20260708.xlsx",
)
BATCH_TID = f"WL-TEST-BATCH-{int(time.time()*1000) % 100000}"


def _doc(path: Path, thread_id: str = "WL-TEST-T1"):
    return client.get(f"/api/v1/review/{thread_id}/document",
                      params={"path": str(path / "单据.jpg")})


def case_1_initial_root_a() -> None:
    """初始 root=A：A 内单据 200。"""
    assert get_settings().upstream_root == str(ROOT_A)
    r = _doc(ROOT_A)
    assert r.status_code == 200, f"A 内单据应放行: {r.status_code} {r.text}"
    print(f"  ✓ 初始 root=A 放行（200）")


def case_2_apply_paths_switch_to_b() -> None:
    """apply_paths 改 B：A 403 / B 200，临时 .env 已更新 + .bak 备份。"""
    tmp_env = Path(tempfile.mkdtemp(prefix="yamato_wl_env_")) / ".env"
    shutil.copy2(APP_ROOT / ".env", tmp_env)

    result = agent_chat.apply_paths({"upstream_root": str(ROOT_B)},
                                    env_path=tmp_env)
    assert result["applied"]["UPSTREAM_ROOT"] == str(ROOT_B), result
    assert f"UPSTREAM_ROOT={ROOT_B}" in tmp_env.read_text(encoding="utf-8")
    assert (tmp_env.parent / ".env.bak").exists(), "缺 .env.bak 备份"

    assert get_settings().upstream_root == str(ROOT_B), "运行时应已切到 B"
    r_old = _doc(ROOT_A)
    assert r_old.status_code == 403, \
        f"改 B 后 A 应 403: {r_old.status_code} {r_old.text}"
    r_new = _doc(ROOT_B)
    assert r_new.status_code == 200, \
        f"改 B 后 B 应 200: {r_new.status_code} {r_new.text}"
    print(f"  ✓ apply_paths 改 B：A 403 / B 200，.env 已写 + .bak 已备份")


def case_3_manual_env_edit_auto_refresh() -> None:
    """手工改 .env（os.environ+cache_clear，不调 refresh）：下请求自动放行 C。"""
    os.environ["UPSTREAM_ROOT"] = str(ROOT_C)
    get_settings.cache_clear()
    # 不调 refresh_review_roots：靠 _auto_refresh_roots 每请求懒感知
    # （thread_id 用本例专属值：批次 root 缓存按 thread_id 记忆，
    #   复用前面用例的 tid 会吃到旧缓存）
    r_new = _doc(ROOT_C, thread_id="WL-TEST-C3")
    assert r_new.status_code == 200, \
        f"手工改 C 后 C 应 200（自动感知）: {r_new.status_code} {r_new.text}"
    r_old = _doc(ROOT_B, thread_id="WL-TEST-C3")
    assert r_old.status_code == 403, \
        f"手工改 C 后 B 应 403: {r_old.status_code} {r_old.text}"
    print(f"  ✓ 手工改 .env → C：白名单自动感知，C 200 / B 403")


def case_4_batch_root_fallback() -> None:
    """批次级 root 二级兜底：带 root=A 的批次 thread_id 放行 A；别的 403。"""
    # 建一个 upstream_root=A 的挂起批次（A 无工厂子目录 → 占位挂起即可）
    r = service.run_until_interrupt(
        BATCH_TID,
        downstream_file_path=DOWNSTREAM,
        upstream_root=str(ROOT_A),
        factory_filter=["山東中地"],
    )
    assert r["status"] == "pending_human_review", r
    state = service.get_order_state(BATCH_TID)
    assert state["values"].get("upstream_root") == str(ROOT_A), state["values"]

    # 全局 root 已是 C：A 内单据带批次 thread_id 二级兜底放行
    r_ok = _doc(ROOT_A, thread_id=BATCH_TID)
    assert r_ok.status_code == 200, \
        f"批次兜底应放行 A: {r_ok.status_code} {r_ok.text}"
    # 别的 thread_id（无 state，回退 settings=C）不放行 A
    r_ng = _doc(ROOT_A, thread_id="WL-TEST-OTHER")
    assert r_ng.status_code == 403, \
        f"别的 thread_id 不应放行 A: {r_ng.status_code} {r_ng.text}"
    print(f"  ✓ 批次级兜底：带 {BATCH_TID} 放行 A，别的 thread_id 403")


def case_5_negatives() -> None:
    """负例：白名单外 403 / 路径穿越 403 / 不存在 404 / 不支持类型 415。"""
    r = client.get("/api/v1/review/WL-TEST-T1/document",
                   params={"path": "/etc/hosts"})
    assert r.status_code == 403, f"白名单外应 403: {r.status_code} {r.text}"
    print("  ✓ 白名单外路径 403")

    traversal = str(ROOT_C / ".." / ".." / "etc" / "hosts")
    r = client.get("/api/v1/review/WL-TEST-T1/document",
                   params={"path": traversal})
    assert r.status_code == 403, \
        f"路径穿越应 403: {r.status_code} {r.text}"
    print("  ✓ 路径穿越 403")

    r = client.get("/api/v1/review/WL-TEST-T1/document",
                   params={"path": str(ROOT_C / "不存在文件.jpg")})
    assert r.status_code == 404, f"白名单内不存在文件应 404: {r.status_code}"
    print("  ✓ 白名单内不存在文件 404")

    (ROOT_C / "说明.txt").write_text("x", encoding="utf-8")
    r = client.get("/api/v1/review/WL-TEST-T1/document",
                   params={"path": str(ROOT_C / "说明.txt")})
    assert r.status_code == 415, f"不支持扩展名应 415: {r.status_code}"
    print("  ✓ 不支持扩展名 415")


CASES = [
    ("1. 初始 root=A 放行", case_1_initial_root_a),
    ("2. apply_paths 改 B：A 403 / B 200", case_2_apply_paths_switch_to_b),
    ("3. 手工改 .env 自动感知 C", case_3_manual_env_edit_auto_refresh),
    ("4. 批次级 root 二级兜底", case_4_batch_root_fallback),
    ("5. 负例（白名单外/穿越 403，不存在 404，类型 415）", case_5_negatives),
]


def main() -> int:
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
        print("🎉 W1 白名单自动刷新全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
