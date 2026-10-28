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

手动录入模式（第七阶段 §1.1，控制台入口）：
  启动时逐厂询问「跳过识别（手动录入）？」y/N，预检建议（三列已填全）作为
  默认值直接回车采纳；非交互参数：
  python3 scripts/run_cli.py --skip-all-manual              # 全部厂手动录入
  python3 scripts/run_cli.py --skip-factories 山東中地,xx厂  # 指定厂手动录入
  --folder-name 指定监控目录子文件夹名（预检三列完整性的数据来源）；
  缺省时只对本冒烟目标厂提问，且无完整性建议。

契约差异说明：§2.2 只给 start_batch_from_scan/create_batch 定义了
manual_skip_factories 参数；本脚本走 run_until_interrupt（冒烟语义需复用
已有 checkpoint），其 manual_skip_factories 形参依赖任务 A 透传——未就位
前仅在勾选非空时传参，空清单不触发差异。
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


def _parse_skip_factories(raw: str | None) -> list[str]:
    """解析 --skip-factories 逗号分隔清单（去空白、去空项、保序去重）。"""
    out: list[str] = []
    for name in (raw or "").split(","):
        name = name.strip()
        if name and name not in out:
            out.append(name)
    return out


def collect_manual_skips(
    *,
    folder_name: str | None = None,
    skip_all: bool = False,
    skip_factories: str | None = None,
    factories_hint: list[str] | None = None,
    precheck_fn=None,
    input_fn=input,
    print_fn=print,
) -> list[str]:
    """汇总「跳过识别（手动录入）」工厂清单（第七阶段 §1.1 控制台入口）。

    优先级：--skip-factories 显式清单 > --skip-all-manual 全部跳过 >
    逐厂 y/N 交互（默认采纳预检建议：三列已填全的厂默认 Y，其余默认 N）。

    - folder_name 给出时调 service.precheck_manual_completeness 取三列
      完整性建议；未给出或预检失败时降级为无建议（默认全 N），工厂全集
      退化为 factories_hint（本冒烟脚本即 [TARGET_FACTORY]）；
    - precheck_fn/input_fn/print_fn 可注入，便于测试不碰真服务/真终端；
    - 非交互参数与交互互斥：给了 --skip-factories/--skip-all-manual 不再提问。
    """
    explicit = _parse_skip_factories(skip_factories)
    if explicit:
        return explicit

    # 预检建议（可选）：folder_name → service 层纯函数（无服务环境直接 import）
    suggestions: dict[str, bool] = {}
    factory_names: list[str] = list(factories_hint or [])
    if folder_name:
        fn = precheck_fn or service.precheck_manual_completeness
        try:
            report = fn(folder_name) or {}
            factories = report.get("factories") or []
            names = [str(f.get("name") or "") for f in factories]
            factory_names = [n for n in names if n]
            suggestions = {
                str(f.get("name") or ""): bool(f.get("complete"))
                for f in factories
            }
        except Exception as e:  # noqa: BLE001 预检是建议不是门槛，失败降级
            print_fn(f"[手动录入] 三列完整性预检失败（降级为无建议）: {e}")

    if skip_all:
        if not factory_names:
            print_fn("[手动录入] --skip-all-manual 生效，但未能确定工厂全集"
                     "（未提供 --folder-name 且预检不可用）")
        return list(factory_names)

    skips: list[str] = []
    for name in factory_names:
        suggested = suggestions.get(name, False)
        mark = "Y/n" if suggested else "y/N"
        suffix = "（三列已填完整，建议手动录入）" if suggested else ""
        try:
            ans = input_fn(
                f"工厂「{name}」跳过识别（手动录入）？[{mark}]{suffix} "
            ).strip().lower()
        except EOFError:
            ans = ""  # 管道/重定向无 stdin：按默认值走，不炸
        if not ans:
            take = suggested
        else:
            take = ans in ("y", "yes")
        if take:
            skips.append(name)
            print_fn(f"[手动录入] 「{name}」将跳过识别，请在输出表格手动填三列")
    return skips


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
    # ---- 手动录入模式（第七阶段 §1.1 控制台入口）----
    parser.add_argument("--skip-all-manual", action="store_true",
                        help="全部工厂跳过识别（手动录入），非交互")
    parser.add_argument("--skip-factories", default=None,
                        help="指定工厂跳过识别，逗号分隔（如 厂A,厂B），非交互")
    parser.add_argument("--folder-name", default=None,
                        help="监控目录子文件夹名：预检三列完整性的数据来源；"
                             "缺省时只对目标厂提问且无完整性建议")
    args = parser.parse_args()

    if args.reset:
        reset_env()

    # ---- 手动录入勾选：逐厂 y/N（默认采纳预检建议），非交互参数优先 ----
    manual_skips = collect_manual_skips(
        folder_name=args.folder_name,
        skip_all=args.skip_all_manual,
        skip_factories=args.skip_factories,
        factories_hint=[TARGET_FACTORY],
    )
    if manual_skips:
        print(f"[手动录入] 本批次跳过识别的工厂: {'、'.join(manual_skips)}")

    # ---- 步骤 A：启动流程，跑到 Node5 interrupt 挂起 ----
    print(f"\n===== 启动流程 thread_id={THREAD_ID} =====")
    # 契约差异（§2.2 未列 run_until_interrupt）：manual_skip_factories 形参
    # 依赖任务 A 透传；空清单不传参，签名未就位也不影响既有冒烟流程
    extra = {}
    if manual_skips:
        extra["manual_skip_factories"] = manual_skips
    result = service.run_until_interrupt(
        THREAD_ID, factory_filter=[TARGET_FACTORY], **extra
    )
    all_manual = result["status"] == "completed"
    if all_manual:
        print("全部手动录入，跳过识别审核环节")
        resume_result = result
    else:
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

    if all_manual:
        state = service.get_order_state(THREAD_ID)
        assert not state["values"].get("pending_factories"), "仍有工厂待处理"
        assert resume_result["status"] == "completed"
        print("[断言通过] 全手动流程已完成，输出副本可供下游使用")
    else:
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
