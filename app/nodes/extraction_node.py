"""Node 3: 提取引擎 Agent（Extraction Node）——薄封装层。

接口契约：另一条并行开发线提供 app/extraction/pipeline.py，对外签名
    extract_folder(folder_path: str) -> list[dict]
每个 dict 至少含字段：
    sku_name / total_quantity / total_net_weight / total_gross_weight /
    weight_unit / source_file / needs_human_review

本节点的职责：
- 调用 extract_folder（import 失败或 EXTRACTION_MOCK=1 时回落到 mock 数据，
  保证骨架端到端冒烟不依赖提取线完成）；
- 若文件夹未匹配（folder_path 为 None），为每个期望 SKU 生成
  needs_human_review=True 的占位条目，让人工在 Node5 补录，不中断流转。
"""
from app.config import get_settings
from app.state import AgentState

# 提取线接口的惰性引用（import 失败不代表系统不可用）
_extract_folder = None
_extract_import_error: Exception | None = None

if not get_settings().extraction_mock:
    try:
        from app.extraction.pipeline import extract_folder as _ef

        _extract_folder = _ef
    except ImportError as e:  # 提取线尚未就绪时兜底
        _extract_import_error = e


def _mock_items(expected_skus: list[str], source: str) -> list[dict]:
    """生成确定性的 mock 提取数据（仅供开发/冒烟测试）。"""
    items = []
    for i, sku in enumerate(expected_skus, start=1):
        qty = 50 * i
        net = 5.0 * qty          # 单件净重 5.0 KG
        gross = 5.3 * qty        # 单件毛重 5.3 KG
        items.append({
            "sku_name": sku,
            "total_quantity": qty,
            "total_net_weight": net,
            "total_gross_weight": gross,
            "weight_unit": "KG",
            "source_file": source,
            "needs_human_review": False,
        })
    return items


def _placeholder_items(expected_skus: list[str], reason: str) -> list[dict]:
    """文件夹缺失/提取失败时的占位数据：全部标记需人工补录。"""
    return [{
        "sku_name": sku,
        "total_quantity": None,
        "total_net_weight": None,
        "total_gross_weight": None,
        "weight_unit": "KG",
        "source_file": reason,
        "needs_human_review": True,
    } for sku in expected_skus]


def extraction_node(state: AgentState) -> dict:
    cur = dict(state.get("current_factory_data") or {})
    folder_path = cur.get("folder_path")
    expected_skus = cur.get("expected_skus") or []

    if not folder_path:
        print(f"[Node3] 工厂「{cur.get('factory_name')}」未匹配到文件夹，生成人工补录占位数据")
        cur["extracted_items"] = _placeholder_items(expected_skus, "no_folder_matched")
        return {"current_factory_data": cur}

    if _extract_folder is None:
        print(f"[Node3] 提取引擎不可用（{_extract_import_error or 'EXTRACTION_MOCK=1'}），"
              f"使用 mock 数据")
        cur["extracted_items"] = _mock_items(expected_skus, "mock")
        return {"current_factory_data": cur}

    try:
        items = _extract_folder(folder_path)
        print(f"[Node3] 提取引擎返回 {len(items)} 条 SKU 数据")
        cur["extracted_items"] = items
    except Exception as e:  # 提取异常不中断流转，转人工兜底
        print(f"[Node3] 提取引擎异常：{e}，生成人工补录占位数据")
        cur["extracted_items"] = _placeholder_items(expected_skus, f"extraction_error: {e}")

    return {"current_factory_data": cur}
