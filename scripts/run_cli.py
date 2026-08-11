#!/usr/bin/env python3
"""CLI 驱动脚本：不依赖真实 LLM，用 mock 提取数据跑通端到端冒烟测试。

流程：
  Node1 解析真实下游文件 -> Node2 匹配工厂文件夹 -> Node3(mock 提取)
  -> Node4 计算+查库 -> Node5 interrupt 挂起
  -> 模拟人工修改（改一个重量值 + 给新 SKU 补中文品名/HS 编码）
  -> Command(resume=...) 唤醒 -> Node6 写 Excel 副本+落库 -> Node7 导出

用法（在 app/ 目录下）：
  python3 scripts/run_cli.py --reset          # 清库重跑（推荐首次）
  python3 scripts/run_cli.py                  # 复用已有 DB（演示老 SKU 路径）
"""
import argparse
import json
import os
import sys
from pathlib import Path

# 冒烟测试强制使用 mock 提取数据（不依赖真实 LLM / 提取线）
os.environ["EXTRACTION_MOCK"] = "1"

# 保证能 import app 包（脚本位于 app/scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.api import service  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import FactorySKU  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402

# 中央日志配置（幂等）：CLI 跑图同样写 app.log/error.log 文件
# 必须放在 import 链（内部已 load_dotenv）之后调用，否则 .env 的 LOG_LEVEL 不生效
setup_logging()

THREAD_ID = "ETD0725-中地"
TARGET_FACTORY = "山東中地"  # 下游文件中的真实工厂名（日文）


def reset_env():
    """清掉 checkpoint / 主数据 / 输出副本，保证冒烟结果确定性。"""
    # 血泪红线（2026-08-11）：pytest 全量跑 tests/smoke_test.py 时，
    # 子进程 import 链的 load_dotenv(override=True) 会把父进程隔离用的
    # 临时 db 环境变量打回 .env 真实路径，--reset 曾因此删除生产
    # checkpoints.db / master.db。
    # 防线：pytest 环境下必须经 YAMATO_DOTENV_PATH 指向临时 .env 才允许删库。
    if "PYTEST_CURRENT_TEST" in os.environ and "YAMATO_DOTENV_PATH" not in os.environ:
        raise SystemExit(
            "[reset] 检测到 pytest 环境但未用 YAMATO_DOTENV_PATH 隔离，"
            "拒绝删除（防止误删生产库）"
        )
    settings = get_settings()
    for p in (settings.checkpoint_db_abs, settings.master_db_abs):
        if p.exists():
            p.unlink()
            print(f"[reset] 已删除 {p}")
    out_copy = settings.output_dir_abs / (
        Path(settings.downstream_file_path).stem + "_filled.xlsx"
    )
    if out_copy.exists():
        out_copy.unlink()
        print(f"[reset] 已删除 {out_copy}")


def simulate_human(review_data: dict) -> dict:
    """模拟人工双屏审核：
    1. 修改第 1 个 SKU 的总净重（250 -> 300），并重算单重与公式；
    2. 给所有新 SKU 补录中文品名 / HS 编码 / 商检状态。
    """
    items = review_data["items"]
    edited_items = []
    for i, item in enumerate(items):
        h_item = {
            "sku": item["sku"],
            "extracted_data": dict(item["extracted_data"]),
            "calculation": dict(item["calculation"] or {}),
        }
        if i == 0:
            # --- 人工修改：总净重 250 -> 300 ---
            ext = h_item["extracted_data"]
            ext["total_net_weight"] = 300.0
            qty = ext["total_quantity"]
            h_item["calculation"]["calculated_unit_net"] = round(300.0 / qty, 3)
            h_item["calculation"]["net_formula"] = f"300.0 / {qty}"
            print(f"[人工] 修改 SKU {item['sku']} 总净重 -> 300.0"
                  f"（单重 {h_item['calculation']['calculated_unit_net']}）")
        if item.get("is_new_sku"):
            # --- 新 SKU 强制补录合规字段 ---
            h_item["name_cn"] = f"测试中文品名-{i+1}"
            h_item["hs_code"] = "9404909000"
            h_item["inspection_required"] = False
        edited_items.append(h_item)
    return {"approved": True, "items": edited_items}


def main():
    parser = argparse.ArgumentParser(description="供应链单证自动化 CLI 冒烟驱动")
    parser.add_argument("--reset", action="store_true", help="清库后重跑")
    args = parser.parse_args()

    if args.reset:
        reset_env()

    # ---- 步骤 A：启动流程，跑到 Node5 interrupt 挂起 ----
    print(f"\n===== 启动流程 thread_id={THREAD_ID} =====")
    result = service.run_until_interrupt(
        THREAD_ID, factory_filter=[TARGET_FACTORY]
    )
    assert result["status"] == "pending_human_review", f"未挂起: {result}"
    review_data = result["review_data"]
    print(f"\n===== 挂起！审核 payload 摘要 =====")
    print(f"工厂: {review_data['factory_name']}")
    print(f"文件夹: {review_data['folder_path']}")
    print(f"源单据数: {len(review_data['source_documents'])}")
    print(f"SKU 数: {len(review_data['items'])}，缺失 SKU: {review_data['missing_skus']}")
    first = review_data["items"][0]
    print(f"首个 SKU 示例:\n{json.dumps(first, ensure_ascii=False, indent=2)[:800]}")

    # ---- 步骤 B：模拟人工审核并唤醒 ----
    print(f"\n===== 模拟人工审核并 resume =====")
    human_data = simulate_human(review_data)
    resume_result = service.resume_order(THREAD_ID, human_data)
    print(f"resume 结果: {resume_result}")

    # ---- 断言 ----
    print(f"\n===== 断言 =====")
    settings = get_settings()
    out_path = Path(resume_result.get("final_output_path") or "")
    assert out_path.exists(), f"输出 Excel 不存在: {out_path}"
    print(f"[断言通过] 输出 Excel 已生成: {out_path}")

    with get_session() as session:
        rows = session.scalars(select(FactorySKU)).all()
        assert len(rows) > 0, "factory_skus 无新行"
        print(f"[断言通过] factory_skus 共 {len(rows)} 行，示例: "
              f"sku={rows[0].sku_code} name_cn={rows[0].name_cn} "
              f"hs={rows[0].hs_code} unit_net={rows[0].unit_net_weight}")

    state = service.get_order_state(THREAD_ID)
    assert state["values"].get("validation_status") == "Approved", \
        f"validation_status 异常: {state['values'].get('validation_status')}"
    assert resume_result["status"] == "success"
    print(f"[断言通过] validation_status == Approved，流程走到 END")

    print(f"\n🎉 冒烟测试全部通过！")


if __name__ == "__main__":
    main()
