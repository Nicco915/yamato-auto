"""人工审核界面独立演示服务（端口 8001）。

- 使用 MockBackend 提供两份 mock 审核 payload（模拟两个工厂连续挂起）；
- mock 版 /api/v1/orders/{thread_id}/resume 与 /state，让单页可以完整走通
  "审核 → 提交 → 下一工厂 → 完成" 的流转；
- mock 版 /api/v1/review/{thread_id}/reextract（批次3 D2）：先于真实 router
  注册以遮蔽真实端点，返回固定条目（一条命中已有卡片触发覆盖确认、一条命中
  missing 触发新增+红条联动），避免 demo 环境调用真实 LLM；
- document 接口指向真实工厂文件夹文件（PDF/Excel/JPG 各一），用于验证渲染链路。

运行（macOS，当前开发机）：cd /Users/nz/Downloads/yamato/app && python -m app.review.demo_server
打开：http://127.0.0.1:8001/review?thread_id=demo

【Windows 运行】本脚本启动时会扫描真实工厂文件夹（settings.upstream_root，
默认值为 macOS 开发机路径），Windows 上必须先在 app/.env 中配置
UPSTREAM_ROOT 指向本机工厂文件夹（例如 UPSTREAM_ROOT=D:/yamato/96/工厂），
并确保其下存在 中地/ 达安/ 子目录及对应演示单据文件，然后：
  cd <项目>\\app && python -m app.review.demo_server
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from app.config import get_settings
from app.review.router import configure_review, router, set_review_backend

UPSTREAM = get_settings().upstream_root  # /Users/nz/Downloads/yamato/96/工厂


def _real(folder: str, prefix: str, contains: str = "") -> str:
    """按前缀在工厂文件夹里找真实文件名。

    真实单据文件名里常含不间断空格 U+00A0（如 'ZDA26-0882A 家盈知 ….pdf'），
    直接硬编码路径会 404，因此 demo 启动时从磁盘读取真实文件名。
    """
    d = Path(UPSTREAM) / folder
    for f in sorted(p.name for p in d.iterdir()):
        if f.startswith(prefix) and contains in f:
            return str(d / f)
    raise FileNotFoundError(f"{d} 下找不到 {prefix}*{contains}*")


# 真实单据文件（中地：PDF+Excel；达安：JPG+legacy xls）
PDF_ZHONGDI = _real("中地", "ZDA26-0882A")
XLSX_ZHONGDI = _real("中地", "XD-269760PackingList")
JPG_DAAN = _real("达安", "XD-269759-001.jpg")
XLS_DAAN = _real("达安", "DA26461", contains="箱单")

# ---- 两份 mock payload：模拟多工厂循环（中地 → 达安）----
MOCK_PAYLOADS: list[dict[str, Any]] = [
    {
        "factory_name": "中地",
        "folder_path": f"{UPSTREAM}/中地",
        "source_documents": [
            XLSX_ZHONGDI,
            PDF_ZHONGDI,
        ],
        "missing_skus": ["4901234567894", "4901234567900"],
        "items": [
            {
                "sku": "4549509623861",
                "extracted_data": {
                    "total_quantity": 50,
                    "total_net_weight": 250.0,
                    "total_gross_weight": 265.0,
                    "weight_unit": "KG",
                    "source_file": PDF_ZHONGDI,
                },
                "calculation": {
                    "net_formula": "250.0 / 50",
                    "gross_formula": "265.0 / 50",
                    "calculated_unit_net": 5.0,
                    "calculated_unit_gross": 5.3,
                },
                "status": "Normal",
                "is_human_edited": False,
                "is_new_sku": False,
                "db_record": {"unit_net_weight": 5.0, "name_cn": "示例既存商品",
                              "hs_code": "9404909000", "inspection_required": 1},
                # 老 SKU：Node5 已把主库三字段提升到顶层，作前端编辑框初值
                "name_cn": "示例既存商品",
                "hs_code": "9404909000",
                "inspection_required": 1,
            },
            {
                "sku": "4549509623878",
                "extracted_data": {
                    "total_quantity": 120,
                    "total_net_weight": 1180.5,
                    "total_gross_weight": 1260.0,
                    "weight_unit": "KG",
                    "source_file": XLSX_ZHONGDI,
                },
                "calculation": {
                    "net_formula": "1180.5 / 120",
                    "gross_formula": "1260.0 / 120",
                    "calculated_unit_net": 9.8375,
                    "calculated_unit_gross": 10.5,
                },
                "status": "Warning",
                "is_human_edited": False,
                "is_new_sku": False,
                "db_record": {"unit_net_weight": 9.5, "weight_diff_ratio": 0.0355,
                              "name_cn": "示例老品·差异预警", "hs_code": "6109100021",
                              "inspection_required": 0},
                "name_cn": "示例老品·差异预警",
                "hs_code": "6109100021",
                "inspection_required": 0,
            },
            {
                "sku": "4549509623885",
                "extracted_data": {
                    "total_quantity": None,
                    "total_net_weight": None,
                    "total_gross_weight": None,
                    "weight_unit": "KG",
                    "source_file": XLSX_ZHONGDI,
                },
                "calculation": {
                    "net_formula": "",
                    "gross_formula": "",
                    "calculated_unit_net": None,
                    "calculated_unit_gross": None,
                },
                "status": "Error",
                "is_human_edited": False,
                "is_new_sku": True,
                "fields_to_fill": ["name_cn", "hs_code", "inspection_required"],
                "db_record": {},
            },
        ],
    },
    {
        "factory_name": "达安",
        "folder_path": f"{UPSTREAM}/达安",
        "source_documents": [
            JPG_DAAN,
            XLS_DAAN,
        ],
        "missing_skus": [],
        "items": [
            {
                "sku": "4936695359672",
                "extracted_data": {
                    "total_quantity": 80,
                    "total_net_weight": 400.0,
                    "total_gross_weight": 428.8,
                    "weight_unit": "KG",
                    "source_file": JPG_DAAN,
                },
                "calculation": {
                    "net_formula": "400.0 / 80",
                    "gross_formula": "428.8 / 80",
                    "calculated_unit_net": 5.0,
                    "calculated_unit_gross": 5.36,
                },
                "status": "Normal",
                "is_human_edited": False,
                "is_new_sku": False,
                "db_record": {"name_cn": "达安示例商品", "hs_code": "4202920000",
                              "inspection_required": 1},
                "name_cn": "达安示例商品",
                "hs_code": "4202920000",
                "inspection_required": 1,
            },
        ],
    },
]


class MockBackend:
    """demo 数据源：按提交次数依次返回 mock payload。"""

    def __init__(self) -> None:
        self.round = 0

    def get_payload(self, thread_id: str) -> dict[str, Any] | None:  # noqa: ARG002
        if self.round < len(MOCK_PAYLOADS):
            return MOCK_PAYLOADS[self.round]
        return None


_backend = MockBackend()


class ReviewSubmitRequest(BaseModel):
    approved: bool = True
    items: list[dict[str, Any]] = []


def create_demo_app() -> FastAPI:
    app = FastAPI(title="人工审核界面 Demo", version="0.1.0")

    # mock reextract（批次3 D2）必须先于 router 注册：FastAPI 按注册序匹配，
    # 先注册者命中，从而遮蔽 router 里的真实端点（真实端点会调 LLM 提取）。
    @app.post("/api/v1/review/{thread_id}/reextract")
    async def mock_reextract(thread_id: str, request: dict[str, Any]):  # noqa: ARG001
        """固定返回两条：一条命中已有卡片（触发覆盖确认），
        一条命中 missing_skus（触发新增 + 红条联动）。"""
        path = str((request or {}).get("path") or "")
        return {
            "source_file": path,
            "items": [
                {
                    "sku": "4549509623861",
                    "extracted_data": {
                        "total_quantity": 55,
                        "total_net_weight": 260.0,
                        "total_gross_weight": 276.0,
                        "weight_unit": "KG",
                        "source_file": path,
                    },
                    "calculation": {
                        "net_formula": "260.0 / 55",
                        "gross_formula": "276.0 / 55",
                        "calculated_unit_net": 260.0 / 55,
                        "calculated_unit_gross": 276.0 / 55,
                    },
                    "status": "Normal",
                    "is_human_edited": False,
                    "is_new_sku": False,
                    "db_record": {"unit_net_weight": 5.0, "name_cn": "示例既存商品",
                                  "hs_code": "9404909000", "inspection_required": 1},
                    "name_cn": "示例既存商品",
                    "hs_code": "9404909000",
                    "inspection_required": 1,
                },
                {
                    "sku": "4901234567894",
                    "extracted_data": {
                        "total_quantity": 30,
                        "total_net_weight": 150.0,
                        "total_gross_weight": 162.0,
                        "weight_unit": "KG",
                        "source_file": path,
                    },
                    "calculation": {
                        "net_formula": "150.0 / 30",
                        "gross_formula": "162.0 / 30",
                        "calculated_unit_net": 5.0,
                        "calculated_unit_gross": 5.4,
                    },
                    "status": "Normal",
                    "is_human_edited": False,
                    "is_new_sku": True,
                    "db_record": {},
                },
            ],
        }

    # 注入 mock 数据源 + 白名单（真实工厂文件夹根目录）
    set_review_backend(_backend)
    configure_review(allowed_roots=[UPSTREAM])
    app.include_router(router)

    @app.post("/api/v1/orders/{thread_id}/resume")
    async def mock_resume(thread_id: str, req: ReviewSubmitRequest):
        """mock resume：第一次返回第二个工厂的 payload，第二次返回完成。"""
        edited = sum(1 for i in req.items if i.get("is_human_edited"))
        _backend.round += 1
        if _backend.round < len(MOCK_PAYLOADS):
            return {
                "status": "pending_human_review",
                "thread_id": thread_id,
                "review_data": MOCK_PAYLOADS[_backend.round],
                "_demo_note": f"上一轮收到 {len(req.items)} 项（含人工修改标记 {edited} 项）",
            }
        return {
            "status": "success",
            "message": "数据已成功落库并写入下游表格（demo）",
            "final_validation_status": "Approved",
            "final_output_path": "app/output/demo_final.xlsx",
        }

    @app.get("/api/v1/orders/{thread_id}/state")
    async def mock_state(thread_id: str):
        done = _backend.round >= len(MOCK_PAYLOADS)
        return {
            "thread_id": thread_id,
            "exists": True,
            "next_nodes": [] if done else ["human_review"],
            "values": {},
        }

    return app


app = create_demo_app()

if __name__ == "__main__":
    # DEMO_PORT 覆盖默认端口（生产占用 8000/8001 时验证用 8399+）
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEMO_PORT", "8001")),
                log_level="info")
