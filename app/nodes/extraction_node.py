"""Node 3: 提取引擎 Agent（Extraction Node）——薄封装层。

真实模式（默认）：以增量会话（app/extraction/session.py::FactorySession）驱动
文件夹内单据逐个处理——与生产模式（单据陆续到达）同一套语义：
- set_expected_skus() 对接 Node1 的下游期望 SKU，每次处理都产出覆盖率；
- 负向候选（报关/汇总版）只登记暂缓、不调 LLM，成本只花在真箱单上；
- 同 SKU 改单自动覆盖旧值入 history 并标 needs_human_review；
- 会话 JSON 落盘 app/data/sessions/{safe(batch_id)}/<工厂名>.json
  供审计追溯；每批次独立缓存、跨批次不回看。

供 Node4 的字段契约（每条 extracted_items）：
    sku_code / sku_name / total_quantity / total_net_weight / total_gross_weight /
    weight_unit / source_file / needs_human_review / review_reason

EXTRACTION_MOCK=1 或提取线 import 失败时回落 mock 数据，保证骨架冒烟不依赖
提取线；文件夹未匹配/提取为空时生成 needs_human_review 占位条目，让人工在
Node5 补录，不中断流转。

后台预提取（2026-07-28）：service.py 在首次 interrupt 后启动 daemon 线程，
对剩余工厂逐个预提取并存入 session JSON；本节点发现 session 已有结果时
直接加载跳过 LLM 调用——实现"审核工厂 A 时提取工厂 B，审核不阻塞提取"。
保存用原子写入（.tmp → rename），防并发读脏数据。

缓存新鲜度校验（2026-08-05，方案C兜底）：会话缓存按 batch_id 隔离目录
（data/sessions/{safe(batch_id)}/），跨批次独立。命中前再校验缓存内的路径证据
（targets[].path / source_file）是否仍落在本批次 upstream_root 之下，
不在（或无任何可用路径证据）则视为陈旧、弃缓存重提，防"改了路径但
没清缓存""rerun_with_paths 改路径重跑"等场景全厂误用上一批次数据。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

from app.config import get_settings
from app.logging_config import bind_factory_from_state
from app.state import AgentState

logger = logging.getLogger(__name__)

# 提取线接口的惰性引用（import 失败不代表系统不可用）
_session_mod = None
_session_import_error: Exception | None = None

if not get_settings().extraction_mock:
    try:
        from app.extraction import session as _sm

        _session_mod = _sm
    except ImportError as e:  # 提取线尚未就绪时兜底
        _session_import_error = e

# 与 extraction.agent 一致的垃圾文件过滤
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}

# 会话缓存目录（与 extraction/session.py 的 SESSIONS_DIR 同路径）
_SESSIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "sessions"


def _mock_items(expected_skus: list[str], source: str) -> list[dict]:
    """生成确定性的 mock 提取数据（仅供开发/冒烟测试）。"""
    items = []
    for i, sku in enumerate(expected_skus, start=1):
        qty = 50 * i
        net = 5.0 * qty          # 单件净重 5.0 KG
        gross = 5.3 * qty        # 单件毛重 5.3 KG
        items.append({
            "sku_code": sku,
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
        "sku_code": sku,
        "sku_name": sku,
        "total_quantity": None,
        "total_net_weight": None,
        "total_gross_weight": None,
        "weight_unit": "KG",
        "source_file": reason,
        "needs_human_review": True,
    } for sku in expected_skus]


def _compute_coverage_from_cache(cached: dict, expected_skus: list[str]) -> dict:
    """从缓存 session 数据重算覆盖率（与 session.coverage() 口径一致）。"""
    items = cached.get("items") or {}
    expected = set(expected_skus)
    have = set(items.keys())
    if expected:
        return {
            "extracted": len(have & expected),
            "expected": len(expected),
            "missing": sorted(expected - have),
            "extra": sorted(have - expected),
        }
    return {"extracted": len(have), "expected": None, "missing": [], "extra": []}


def _run_factory_session(
    batch_id: str,
    folder_path: str,
    factory_name: str,
    expected_skus: list[str],
):
    """增量模式驱动：单据逐个 process_file，返回跑完的 FactorySession。

    每批次独立缓存：写入 data/sessions/{safe(batch_id)}/{factory}.json，
    与 `_try_load_cached_session` 的读路径完全对称。

    保存走原子写入（tmp → rename）：后台预提取线程与图内 Node3 可能
    同时访问同一工厂的 session 文件，原子 rename 保证读方要么看到旧文件
    要么看到完整新文件，不会读到半截 JSON。
    """
    from app.extraction.session import batch_session_dir
    session = _session_mod.FactorySession(factory=factory_name)
    session.set_expected_skus(expected_skus)
    root = Path(folder_path)
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name not in IGNORE_NAMES
    )
    for p in files:
        r = _session_mod.process_file(session, str(p))
        logger.info("[Node3]   [%s] %s：%s", r.action, p.name, r.message)

    # 每批次专属目录：data/sessions/{safe(batch_id)}/
    session_dir = batch_session_dir(batch_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / f"{factory_name}.json"
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=f"{factory_name}.", dir=str(session_dir))
    try:
        os.write(tmp_fd, json.dumps(session.to_dict(), ensure_ascii=False,
                                     indent=1).encode("utf-8"))
    finally:
        os.close(tmp_fd)
    os.replace(tmp_path, target)  # POSIX 原子 rename
    return session


def _looks_like_path(value: str) -> bool:
    """区分真实文件路径与 "mock"/"no_folder_matched" 这类非路径标记。

    真路径必含路径分隔符（/ 或 \\）或 Windows 盘符前缀（如 D:）；
    纯标记词不含分隔符，跳过不作为新鲜度证据。
    """
    if "/" in value or "\\" in value:
        return True
    # Windows 盘符退化形态（无分隔符的 "D:xxx"，防御性判断）
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def _collect_path_evidence(data: dict) -> list[str]:
    """从缓存 JSON 收集路径证据：优先 targets[].path，
    targets 无可用路径时回落 items[*].source_file / no_code_items[*].source_file。
    非路径标记（mock/no_folder_matched 等）一律跳过。"""
    target_paths = [t.get("path") for t in (data.get("targets") or [])]
    evidence = [p for p in target_paths
                if isinstance(p, str) and _looks_like_path(p)]
    if evidence:
        return evidence
    items = list((data.get("items") or {}).values())
    items += list(data.get("no_code_items") or [])
    return [i["source_file"] for i in items
            if isinstance(i.get("source_file"), str)
            and _looks_like_path(i["source_file"])]


def _is_cache_fresh(data: dict, factory_name: str, upstream_root: str) -> bool:
    """校验缓存路径证据是否仍属于当前批次的上游根目录。

    判定规则（宁可重提、不误用旧批次数据）：
    - 缓存里没有任何可用路径证据 → 陈旧；
    - 至少一条证据位于 upstream_root 之下且该文件仍存在 → 新鲜；
    - 其余情况（全部证据不在 root 下 / 在 root 下但文件已删）→ 陈旧。
    """
    root = Path(upstream_root).expanduser().resolve()
    evidence = _collect_path_evidence(data)
    if not evidence:
        logger.info("[Node3] 工厂「%s」会话缓存无任何路径证据，视为陈旧弃用"
                    "（当前上游根目录 %s），走正常提取", factory_name, root)
        return False
    for raw in evidence:
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue  # resolve 失败（如符号链接环）不致命，跳过该条
        if p.is_relative_to(root) and p.exists():
            return True
    logger.info("[Node3] 工厂「%s」会话缓存视为陈旧弃用：缓存示例路径 %s "
                "不在当前上游根目录 %s 之下（或文件已删），走正常提取",
                factory_name, evidence[0], root)
    return False


def _try_load_cached_session(batch_id: str,
                             factory_name: str,
                             upstream_root: str | None = None) -> dict | None:
    """读本批次 data/sessions/{safe(batch_id)}/{factory_name}.json。

    每批次独立缓存；跨批次不回看（由调用方按 batch_id 隔离语义保证）。

    upstream_root 不为 None 时做方案 C 校验：缓存内 targets[].path
    （回落 items/no_code_items 的 source_file，非路径标记如 mock 自动剔除）
    至少一条在 upstream_root 之下、且该文件仍存在才视为新鲜。

    upstream_root=None 时不做新鲜度校验（兼容预提取）。

    文件不存在/JSON 损坏/不新鲜均返回 None。
    """
    from app.extraction.session import batch_session_path
    path = batch_session_path(batch_id, factory_name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if upstream_root is not None and not _is_cache_fresh(
            data, factory_name, upstream_root):
        return None
    return data


def extraction_node(state: AgentState) -> dict:
    # L2 日志关联：多工厂循环中本节点处理的是 state 里的新工厂，须从 state
    # 重绑（submit 时拷贝的 context 残留上一工厂）；节点独立 context 保证
    # 绑定不外泄，无需清理（与 folder_router 同，见 logging_config 注释）
    bind_factory_from_state(state)
    cur = dict(state.get("current_factory_data") or {})
    factory_name = cur.get("factory_name") or "未知工厂"
    folder_path = cur.get("folder_path")
    expected_skus = cur.get("expected_skus") or []
    # 单厂重试标志（service.retry_factory_extraction 经 update_state 写入）：
    # 为 True 时跳过会话缓存强制重提；本节点所有返回分支都带
    # force_reextract=False 自清，防残留污染后续工厂
    force = bool(state.get("force_reextract"))

    if not folder_path:
        logger.warning("[Node3] 工厂「%s」未匹配到文件夹，生成人工补录占位数据",
                       factory_name)
        cur["extracted_items"] = _placeholder_items(expected_skus, "no_folder_matched")
        # W6a：提取失败标记，供 Node4 条件边分流暂缓队列
        cur["extraction_ok"] = False
        cur["failure_reason"] = "no_folder_matched"
        return {"current_factory_data": cur, "force_reextract": False}

    if _session_mod is None:
        logger.warning("[Node3] 提取引擎不可用（%s），使用 mock 数据",
                       _session_import_error or 'EXTRACTION_MOCK=1')
        cur["extracted_items"] = _mock_items(expected_skus, "mock")
        # mock 视为成功（保证既有 mock 测试主流程不变），不写 failure_reason
        cur["extraction_ok"] = True
        return {"current_factory_data": cur, "force_reextract": False}

    # ---- 缓存短路：后台预提取线程可能已跑完该工厂 ----
    if force:
        logger.info("[Node3] 工厂「%s」：强制重提，跳过会话缓存", factory_name)
    # 新鲜度校验（方案C兜底）：以本批次 upstream_root 判定缓存是否陈旧，
    # state 缺省时回落 .env 缺省上游根目录（与 folder_router 口径一致）
    upstream_root = state.get("upstream_root") or get_settings().upstream_root
    batch_id = state.get("batch_id") or "unknown"
    cached = None if force else _try_load_cached_session(
        batch_id, factory_name, upstream_root)
    if cached is not None:
        items = [dict(d) for d in (cached.get("items") or {}).values()]
        items += [dict(d) for d in (cached.get("no_code_items") or [])]
        coverage = _compute_coverage_from_cache(cached, expected_skus)
        cur["extracted_items"] = items
        cur["extraction_issues"] = cached.get("issues") or []
        cur["extraction_coverage"] = coverage
        n_blocking = sum(1 for i in (cached.get("issues") or [])
                         if i.get("level") == "blocking")
        logger.info("[Node3] 工厂「%s」：缓存命中，%d 条 SKU（跳过 LLM 提取），"
                    "覆盖率 %s/%s，状态 %s",
                    factory_name, len(items),
                    coverage.get("extracted"), coverage.get("expected") or "?",
                    cached.get("status"))
        if n_blocking:
            logger.info("[Node3]   反馈 %d 条（blocking %d）",
                        len(cached.get("issues") or []), n_blocking)
        if not items:
            cur["extracted_items"] = _placeholder_items(
                expected_skus, "no_items_extracted")
            # 缓存命中但 items 为空：等同提取失败，走暂缓/挂起分流
            cur["extraction_ok"] = False
            cur["failure_reason"] = "no_items_extracted"
        else:
            cur["extraction_ok"] = True
        return {"current_factory_data": cur, "force_reextract": False}

    try:
        session = _run_factory_session(batch_id, folder_path, factory_name, expected_skus)
    except Exception as e:  # 提取异常不中断流转，转人工兜底
        logger.exception("[Node3] 提取引擎异常：%s，生成人工补录占位数据", e)
        cur["extracted_items"] = _placeholder_items(expected_skus, f"extraction_error: {e}")
        cur["extraction_ok"] = False
        cur["failure_reason"] = f"extraction_error: {e}"
        return {"current_factory_data": cur, "force_reextract": False}

    # 会话累积结果 → Node4 契约（ExtractedItem.model_dump() 字段已对齐）
    items = [dict(d) for d in session.items.values()]
    items += [dict(d) for d in session.no_code_items]  # 无条码条目也交人工过目
    coverage = session.coverage()
    cur["extracted_items"] = items
    cur["extraction_issues"] = session.issues
    cur["extraction_coverage"] = coverage

    n_blocking = sum(1 for i in session.issues if i.get("level") == "blocking")
    logger.info("[Node3] 工厂「%s」：提取 %d 条 SKU，"
                "覆盖率 %s/%s，反馈 %d 条（blocking %d），状态 %s",
                factory_name, len(items),
                coverage.get('extracted'), coverage.get('expected') or '?',
                len(session.issues), n_blocking, session.status)

    if not items:
        # 零容错：一条都没提出来绝不空转，全量占位交人工补录
        logger.warning("[Node3] 提取结果为空，生成人工补录占位数据")
        cur["extracted_items"] = _placeholder_items(expected_skus, "no_items_extracted")
        cur["extraction_ok"] = False
        cur["failure_reason"] = "no_items_extracted"
    else:
        cur["extraction_ok"] = True

    return {"current_factory_data": cur, "force_reextract": False}
