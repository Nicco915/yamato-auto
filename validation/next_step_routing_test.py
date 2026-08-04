# -*- coding: utf-8 -*-
"""「下一步是什么」类状态问题路由测试（prompt 文本层 + mock 剧本集成）。

背景：操作员问「下一步是什么」，分诊层曾判成 qa → 直路由 ask_guide
（静态知识库）→ 关键词不命中 → 兜底「目前指引未覆盖该场景」。但该问题
本质是状态感知问题，必须走 action 通道让执行器先调 list_batches /
get_batch_status 拿真实批次状态再回答。本次改动全部在 prompt 文本层
（prompts.py 三处），不改任何路由/代码逻辑。

覆盖（session_id 一律用 DISP-NEXT-TEST- 前缀，与并行运行的其他
dispatcher 测试隔离）：
1. triage_prompt(phase=2)：含「下一步」相关指引（action 定义 + qa 排除），
   占位符 {tool_list} / {l2_context} 已正确注入无残留；
2. system_prompt(phase=2)：含「下一步」判别行（操作指导 vs 数据查询段）；
3. executor_prompt(phase=2)：含「下一步」行为指引（先调只读工具拿真实
   状态再给建议）；
4. mock 集成：分诊判 action/list_batches → handle_message("下一步是什么")
   进 loop（loop 剧本被消费、list_batches 真实执行），不走 ask_guide
   直路由（无 references）。

剧本队列均为模块级全局（triage._TRIAGE_MOCK_SCRIPT / loop._MOCK_SCRIPT），
每个用例前一并 clear 再注入，用例间互不污染。

用法（在 app/ 目录下）：
  EXTRACTION_MOCK=1 DISPATCHER_MOCK=1 GUIDE_MOCK=1 python3 validation/next_step_routing_test.py

隔离（血泪红线）：checkpoint/master db、output、alias_map、sessions 目录
全部指向临时目录（import app 之后再设 env + cache_clear + 真实库断言守卫，
见 validation/_test_isolation.py）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- env 前置（必须在 import app 之前；db 路径在 import 后隔离）----
os.environ.setdefault("EXTRACTION_MOCK", "1")   # 提取走 mock（本测试不跑图，防御性设置）
os.environ["DISPATCHER_MOCK"] = "1"             # 调度循环 + 分诊都走剧本，不调真实 LLM
os.environ["GUIDE_MOCK"] = "1"                  # ask_guide 走模板降级（确定性输出）
# 本文件测 triage 分诊层（legacy 引擎专属机制），无论外部环境变量如何都钉死
# legacy——防止全量套件以 DISPATCHER_ENGINE=react 外导时被误跑
os.environ["DISPATCHER_ENGINE"] = "legacy"

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import dispatcher  # noqa: E402
from app.dispatcher import loop, prompts, sessions, triage  # noqa: E402
from app.graph import get_graph  # noqa: E402

# 二次钉死：llm_client 模块 import 时 load_dotenv(override=True) 会把
# 生产 .env 的 DISPATCHER_ENGINE=react 灌进 os.environ 覆盖上面的钉——
# app import 完成后必须重钉（_engine() 在调用时才读，此处钉住即生效）
os.environ["DISPATCHER_ENGINE"] = "legacy"

from _test_isolation import isolate_to_tmp  # noqa: E402

# ---- 隔离（必须在全部 app import 之后，首个 db 使用之前）----
TMP = isolate_to_tmp("yamato_next_step_test_", alias_map_copy=True)


def set_scripts(triage_items: list[dict] | None = None,
                loop_items: list[dict] | None = None) -> None:
    """清空两个剧本队列再注入（模块级全局，用例间必须隔离）。"""
    triage._TRIAGE_MOCK_SCRIPT.clear()
    triage._TRIAGE_MOCK_SCRIPT.extend(triage_items or [])
    loop._MOCK_SCRIPT.clear()
    loop._MOCK_SCRIPT.extend(loop_items or [])


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def case_1_triage_prompt_guidance() -> None:
    """triage_prompt(phase=2)：含「下一步」指引，占位符注入无残留。"""
    p = prompts.triage_prompt(phase=2)
    # 「下一步」类问题归 action 的指引（target list_batches / get_batch_status）
    assert "下一步" in p, "triage_prompt 应含「下一步」相关指引"
    assert "list_batches" in p, "triage_prompt 应指引 target_tool=list_batches"
    assert "get_batch_status" in p, \
        "triage_prompt 应指引有批次上下文时 target_tool=get_batch_status"
    # qa 定义段的排除句
    assert "不属于 qa" in p, "triage_prompt 的 qa 定义应排除「下一步」类问题"
    # 占位符已注入，无残留（prompt 内含 JSON 示例花括号属正常，逐个查占位符）
    assert "{tool_list}" not in p, "triage_prompt 不应残留 {tool_list} 占位符"
    assert "{l2_context}" not in p, "triage_prompt 不应残留 {l2_context} 占位符"


def case_2_system_prompt_guidance() -> None:
    """system_prompt(phase=2)：「操作指导 vs 数据查询」段含「下一步」判别行。"""
    p = prompts.system_prompt(phase=2)
    assert "下一步是什么" in p, "system_prompt 应含「下一步是什么」判别行"
    assert "list_batches" in p and "get_batch_status" in p, \
        "system_prompt 判别行应指向 list_batches / get_batch_status"
    assert "状态问题" in p, "system_prompt 判别行应点明这是状态问题"


def case_3_executor_prompt_guidance() -> None:
    """executor_prompt(phase=2)：含「下一步」行为指引（先查状态再建议）。"""
    p = prompts.executor_prompt(phase=2)
    assert "下一步" in p, "executor_prompt 应含「下一步」行为指引"
    assert "list_batches" in p and "get_batch_status" in p, \
        "executor_prompt 指引应要求先调 list_batches / get_batch_status"
    assert "不编造状态" in p, "executor_prompt 指引应要求不编造状态"


def case_4_next_step_routes_to_loop() -> None:
    """mock 集成：action/list_batches → 进 loop 真实执行，不走 ask_guide。"""
    sid = "DISP-NEXT-TEST-C4"
    set_scripts(
        triage_items=[{
            "intent": "action", "target_tool": "list_batches",
            "extracted_args": {},
            "confidence": 0.95,
        }],
        loop_items=[
            {"tool_calls": [{"id": "c1", "name": "list_batches", "args": {}}]},
            {"final_text": "当前没有进行中的批次，可以发起新批次。"},
        ],
    )
    r = dispatcher.handle_message("下一步是什么", session_id=sid)
    assert r["status"] == "ok", r
    assert r["message"] == "当前没有进行中的批次，可以发起新批次。", r
    # 进了 loop：loop 剧本被恰好用尽（未走 qa 直路由）
    assert loop._MOCK_SCRIPT == [], "loop 剧本应被恰好用尽（确实进了 loop）"
    # 没走 ask_guide 直路由：qa 直路由会带 references 字段
    assert "references" not in r, \
        f"不应走 ask_guide 直路由（不应有 references）: {r}"
    assert r.get("intent") != "qa", f"不应被当作 qa: {r}"
    # list_batches 被真实执行（拿到真实批次状态再回答）
    hist = sessions.get_session(sid).tool_history
    assert any(h["tool"] == "list_batches" for h in hist), \
        f"list_batches 应被真实执行: {hist}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

CASES = [
    ("1. triage_prompt 含「下一步」指引（占位符无残留）", case_1_triage_prompt_guidance),
    ("2. system_prompt 含「下一步」判别行", case_2_system_prompt_guidance),
    ("3. executor_prompt 含「下一步」行为指引", case_3_executor_prompt_guidance),
    ("4. mock 集成：下一步 → action 进 loop，不走 ask_guide", case_4_next_step_routes_to_loop),
]


def main() -> int:
    # 预热：全新环境 checkpoints.db 不存在时，service 以 mode=ro 打开会失败，
    # 先触发建库建表（与 dispatcher_triage_test 同款规避）
    get_graph().get_state(
        {"configurable": {"thread_id": "DISP-NEXT-TEST-WARMUP"}})

    results: list[tuple[str, bool, str]] = []
    for name, fn in CASES:
        print(f"===== {name} =====")
        try:
            fn()
        except Exception as e:  # noqa: BLE001 收集全部失败，最后统一总结
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
        print("🎉 「下一步」类状态问题路由测试全部通过！")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
