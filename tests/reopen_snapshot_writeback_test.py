# -*- coding: utf-8 -*-
"""reopen 提交后回写 factory_outputs 快照测试（service.apply_reopen_payload）。

背景 bug：reopen 只写 Excel/DB、不回写 LangGraph 快照，而 reopen 读取数据源
优先级是 快照 > Excel 兜底 → 提交成功后重新打开仍显示改前的值。

修复：批次已结束（无挂起 interrupt）时把提交值回写进 factory_outputs 快照；
批次仍挂起时跳过（update_state 会销毁 Node5 interrupt 任务，实测）。

测试方式：monkeypatch get_graph 返回可控假 state（避开真实跑图到 completed
的高成本路径），writer 三件套（_ensure_output_copy/_write_excel/_upsert_db）
与 _write_audit 同样打桩——本测试聚焦「何时回写、回写什么」。

覆盖：
- completed + dict 快照 → 回写 calculated_items，其余快照字段保留
- completed + 旧格式 list 快照 → 整表替换
- completed + 无快照（Excel 兜底场景）→ 不写（Excel 自一致）
- pending_review（next 非空）→ 绝不回写（防 interrupt 销毁）

运行方式：
    cd app && PYTHONPATH=. python3 -m pytest tests/reopen_snapshot_writeback_test.py -v

隔离：validation/_test_isolation.isolate_to_tmp（血泪红线，绝不碰真实库）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "validation"))

os.environ["EXTRACTION_MOCK"] = "1"
os.environ["DISPATCHER_MOCK"] = "1"

import pytest  # noqa: E402

from app.api import service  # noqa: E402
from app.nodes import writer as writer_mod  # noqa: E402

from _test_isolation import isolate_to_tmp  # noqa: E402

# 隔离必须在 import 全部 app 模块之后（llm_client 的 load_dotenv override 红线）
TMP = isolate_to_tmp("yamato_reopen_snap_test_")

TID = "TEST-REOPEN-SNAPSHOT"
FACTORY = "中地"

ITEMS = [{
    "sku": "1234567890123",
    "extracted_data": {"total_quantity": 100, "total_net_weight": 1680.0,
                       "total_gross_weight": 1800.0, "weight_unit": "KG"},
    "calculation": {"calculated_unit_net": 16.8, "calculated_unit_gross": 18.0},
    "is_human_edited": True,
}]


class _FakeGraph:
    """可控假 graph：get_state 返回构造的 state，update_state 记录调用。"""

    def __init__(self, values, next_nodes=()):
        self._values = values
        self._next = tuple(next_nodes)
        self.update_calls: list[dict] = []

    def get_state(self, _config):
        return SimpleNamespace(values=self._values, next=self._next, tasks=[])

    def update_state(self, _config, values, as_node=None):
        self.update_calls.append({"values": values, "as_node": as_node})
        self._values.update(values)


def _make_values(snapshot):
    return {
        "batch_id": TID,
        "downstream_file_path": "/tmp/downstream.xlsx",
        "downstream_row_map": {},
        "factory_outputs": {FACTORY: snapshot} if snapshot is not None else {},
        "current_factory_data": {},
    }


@pytest.fixture
def patched(monkeypatch):
    """打桩 writer 三件套 + _write_audit，返回可控的 fake graph 工厂。"""
    monkeypatch.setattr(writer_mod, "_ensure_output_copy",
                        lambda state: TMP / "out.xlsx")
    monkeypatch.setattr(writer_mod, "_write_excel", lambda state, path: 3)
    monkeypatch.setattr(writer_mod, "_upsert_db", lambda state: (0, 0))
    monkeypatch.setattr(service, "_write_audit", lambda *a, **k: None)

    def install(values, next_nodes=()):
        fake = _FakeGraph(values, next_nodes)
        monkeypatch.setattr(service, "get_graph", lambda: fake)
        return fake
    return install


def test_completed_dict_snapshot_written_back(patched):
    snapshot = {"factory_name": FACTORY,
                "calculated_items": [{"sku": "1234567890123",
                                      "extracted_data": {"total_net_weight": 1679.99999}}],
                "folder_path": "/upstream/中地"}
    fake = patched(_make_values(snapshot), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert len(fake.update_calls) == 1
    written = fake.update_calls[0]["values"]["factory_outputs"][FACTORY]
    assert written["calculated_items"] == ITEMS
    # 快照其余字段保留
    assert written["folder_path"] == "/upstream/中地"


def test_completed_list_snapshot_replaced(patched):
    fake = patched(_make_values([{"sku": "old"}]), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["values"]["factory_outputs"][FACTORY] == ITEMS


def test_completed_no_snapshot_no_write(patched):
    fake = patched(_make_values(None), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert fake.update_calls == []


def test_pending_review_never_writes(patched):
    """批次仍挂起 Node5 interrupt：绝不 update_state（防销毁 interrupt）。"""
    snapshot = {"factory_name": FACTORY, "calculated_items": [{"sku": "old"}]}
    fake = patched(_make_values(snapshot), next_nodes=("node5_human_review",))
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert fake.update_calls == []


def test_writeback_anchors_at_terminal_node7(patched):
    """回写必须锚 NODE7（终点节点）：锚 NODE6 会让条件边重路由出
    next=(NODE7,)，批次永远卡「进行中」（2026-08-26 test-86 生产事故）。"""
    snapshot = {"factory_name": FACTORY, "calculated_items": [{"sku": "old"}]}
    fake = patched(_make_values(snapshot), next_nodes=())
    service.apply_reopen_payload(TID, FACTORY, {"approved": True, "items": ITEMS})
    assert len(fake.update_calls) == 1
    assert fake.update_calls[0]["as_node"] == "node7_export", fake.update_calls


# ---------------------------------------------------------------------------
# 端到端：真实图（mock 提取）跑完后 reopen —— 批次必须仍是 completed
# （test-86 生产事故的回归锚点，与 factory_skip_test 同模式）
# ---------------------------------------------------------------------------

E2E_THREAD = "REOPEN-E2E"
E2E_FACTORY = "回写快照厂"
E2E_SKU = "4900000000003"


def test_e2e_reopen_after_completion_keeps_completed(monkeypatch):
    from openpyxl import Workbook

    from app.nodes import extraction_node as en
    # 全量 pytest 下防止先收集模块把提取线绑成真实通道（同 factory_skip_test）
    monkeypatch.setattr(en, "_session_mod", None)
    monkeypatch.setattr(en, "_session_import_error", "EXTRACTION_MOCK=1（测试强制）")

    xlsx = TMP / "reopen_e2e_downstream.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["MAKER_MEI_KJ", "SHOHIN_CD", "SHOHIN_MEI_E", "SOTOBAKO_D_HACCHU_SU"])
    ws.append([E2E_FACTORY, E2E_SKU, "ITEM-C", 10])
    wb.save(xlsx)
    upstream = TMP / "reopen_e2e_upstream"
    (upstream / E2E_FACTORY).mkdir(parents=True)

    r1 = service.run_until_interrupt(
        E2E_THREAD, downstream_file_path=str(xlsx), upstream_root=str(upstream))
    assert r1["status"] == "pending_human_review", f"未挂起: {r1}"
    r2 = service.resume_order(E2E_THREAD, {"approved": True, "items": []})
    assert r2["status"] == "success", f"批次未完成: {r2}"

    # reopen 提交一个修改过的净重（真实 writer 写 Excel/DB + 快照回写）
    state = service.get_order_state(E2E_THREAD)
    items = (state["values"]["factory_outputs"][E2E_FACTORY]
             ["calculated_items"])
    assert items, "factory_outputs 快照应非空"
    items[0]["extracted_data"]["total_net_weight"] = 1680.0
    items[0]["is_human_edited"] = True
    out = service.apply_reopen_payload(
        E2E_THREAD, E2E_FACTORY, {"approved": True, "items": items})
    assert out["status"] == "success", out

    # 事故回归锚点：批次不得因回写变成「进行中」
    state2 = service.get_order_state(E2E_THREAD)
    assert not state2["next_nodes"], f"回写后 next 必须为空: {state2['next_nodes']}"
    detail = service.get_batch_detail(E2E_THREAD)
    assert detail["status"] == "completed", detail["status"]

    # 快照已更新为新值
    snap_items = (state2["values"]["factory_outputs"][E2E_FACTORY]
                  ["calculated_items"])
    assert snap_items[0]["extracted_data"]["total_net_weight"] == 1680.0
    print("[断言通过] e2e：reopen 回写后批次仍 completed，快照已更新")


def test_repair_script_unsticks_node6_anchored_batch():
    """修复脚本：把「锚 NODE6 卡死」的批次恢复到 completed。

    先用 as_node=NODE6 重放旧 bug 制造卡死现场（next=(NODE7,)），
    再跑 scripts/repair_stuck_running_batch.repair，断言 next 归空。
    """
    import importlib.util

    script = APP_ROOT / "scripts" / "repair_stuck_running_batch.py"
    spec = importlib.util.spec_from_file_location("repair_stuck", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from app.graph import NODE6, get_graph
    graph = get_graph()
    cfg = service._config(E2E_THREAD)

    # 制造卡死现场（模拟旧版 reopen 回写的副作用）
    graph.update_state(cfg, {}, as_node=NODE6)
    stuck = graph.get_state(cfg)
    assert stuck.next, f"制造卡死失败: {stuck.next}"

    assert mod.repair(E2E_THREAD) is True
    after = graph.get_state(cfg)
    assert not after.next, f"修复后 next 必须为空: {after.next}"

    # 幂等：再跑一次应跳过且不报错
    assert mod.repair(E2E_THREAD) is False
    print("[断言通过] 修复脚本：卡死批次恢复 completed，幂等")
