"""工厂名匹配唯一事实来源（W5）：规范化 / alias 读写 / 分档匹配 / 候选推荐。

Node2（folder_router）与批次预扫（service/tools）共用本模块：
- match_factory_folder：七档查找 —— 批次覆盖(override) → alias 精确 →
  alias 大小写不敏感 → 规范化精确 → 别名/短名即文件夹名(alias_folder) →
  rapidfuzz 模糊匹配 → 规范化包含兜底；
- recommend_candidates：预扫「低置信推荐」档的候选生成，
  rapidfuzz top-N + 包含强信号（如 天津市依依衛生用品 ⊃ 依依）保底 70 分；
- validate_subfolder：上游根目录下一级子目录名的防注入校验，
  发起批次 alias_decisions 与运行中 retry_factory 对照注入共用（同源）；
- load/save_alias_entries：工厂别名读写。DB（factory_aliases 表）为权威源，
  alias_map.json 退化为回退：DB 查询为空或异常时自动回落 json 文件
  （损坏容错、原子写、写前 .bak 备份、模块级锁串行）；
- load_folder_match_candidates：alias_folder 档数据源——工厂的 short_name
  与勾选「文件夹匹配」的别名本身即候选文件夹名（DB 唯一来源，json 无工厂
  分组概念，不提供回退；DB 异常/为空时该档静默跳过，不影响既有档位）；
- load_excel_normalize_map / load_inspection_factories：Excel 归一化映射与
  商检工厂名单，同样 DB 优先、config（FACTORY_NORMALIZE_MAP /
  INSPECTION_FACTORIES）兜底。
"""
import json
import logging
import os
import shutil
import threading
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

from app.config import get_settings
from app.db.models import Factory, FactoryAlias
from app.db.session import get_session

logger = logging.getLogger(__name__)

# save_alias_entries 串行化：同一进程内并发保存互不踩踏
_save_lock = threading.Lock()


def normalize_name(name: str) -> str:
    """规范化名称：全角转半角、去空白、小写，提高模糊匹配命中率。"""
    s = unicodedata.normalize("NFKC", name)
    return "".join(s.split()).lower()


def _alias_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return get_settings().alias_map_abs


def _load_alias_map_from_db() -> dict:
    """从 factory_aliases 读文件夹匹配别名：{alias: factory.short_name}。

    short_name 为空的行跳过。调用方负责异常捕获。"""
    with get_session() as sess:
        rows = (
            sess.query(FactoryAlias.alias, Factory.short_name)
            .join(Factory, FactoryAlias.factory_id == Factory.factory_id)
            .filter(FactoryAlias.use_folder_match.is_(True))
            .all()
        )
    return {alias: short for alias, short in rows if short}


def _load_alias_map_from_json(p: Path) -> dict:
    """读取 alias_map.json。文件不存在或 JSON 损坏时记日志并返回 {}，
    绝不抛异常（损坏即崩曾打崩 Node2 整条提取线）。"""
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.error("alias_map 读取失败,按空表处理: %s (%s)", p, e)
        return {}
    if not isinstance(data, dict):
        logger.error("alias_map 顶层不是 dict,按空表处理: %s", p)
        return {}
    return data


def load_alias_map(path: str | Path | None = None) -> dict:
    """读取工厂别名对照。DB 为权威源，json 文件为回退。

    未显式传 path 时优先查 DB（factory_aliases）：结果非空直接返回；
    DB 为空（未迁移）或查询任何异常，记日志并回退 json 文件，
    行为与旧版完全一致。显式传 path 时只读指定 json 文件（测试/工具用）。
    """
    if path is None:
        try:
            db_map = _load_alias_map_from_db()
        except Exception as e:  # DB 不可用绝不阻塞主流程
            logger.warning("factory_aliases 查询失败,回退 alias_map.json: %s", e)
            db_map = {}
        if db_map:
            return db_map
    return _load_alias_map_from_json(_alias_path(path))


def load_folder_match_candidates() -> dict[str, list[str]]:
    """alias_folder 档数据源：{工厂任一已知名字: [候选文件夹名, ...]}。

    每家工厂的候选 = short_name（若有）+ 全部勾选「文件夹匹配」的别名，
    工厂的 factory_name 与每个别名都作为 key 指向同一份候选列表。
    用途：别名不仅翻译装箱单名字，其本身常常就是文件夹名
    （如别名「YIYI」、文件夹「YIYI」），short_name 匹配不上时兜底。

    DB 唯一来源（json 无工厂分组概念，无法提供等价数据）；
    查询失败/为空返回 {}——alias_folder 档静默跳过，绝不影响既有六档。
    """
    try:
        with get_session() as sess:
            rows = (
                sess.query(
                    Factory.factory_name, Factory.short_name, FactoryAlias.alias)
                .outerjoin(
                    FactoryAlias,
                    (FactoryAlias.factory_id == Factory.factory_id)
                    & FactoryAlias.use_folder_match.is_(True))
                .order_by(Factory.factory_id, FactoryAlias.id)
                .all()
            )
    except Exception as e:  # DB 不可用绝不阻塞主流程
        logger.warning("folder_match_candidates 查询失败，alias_folder 档跳过: %s", e)
        return {}

    # 先按工厂聚合候选（short_name 在前、别名随后，去重保序）
    per_factory: dict[str, list[str]] = {}
    for factory_name, short_name, alias in rows:
        cands = per_factory.setdefault(factory_name, [])
        for name in (short_name, alias):
            if name and name not in cands:
                cands.append(name)

    # factory_name 与每个别名都映射到同一份候选列表；
    # 名字撞车（两家工厂同名/别名互撞）时合并候选，多试几个无害
    result: dict[str, list[str]] = {}
    for factory_name, cands in per_factory.items():
        if not cands:
            continue
        keys = [factory_name, *[c for c in cands]]
        for key in keys:
            slot = result.setdefault(key, [])
            for c in cands:
                if c not in slot:
                    slot.append(c)
    return result


def match_factory_folder(
    factory: str,
    folders: list[str],
    alias_map: dict,
    cutoff: float,
    overrides: dict | None = None,
    folder_candidates: dict[str, list[str]] | None = None,
) -> tuple[str | None, float, str]:
    """按七档顺序为工厂名匹配本地文件夹。

    返回 (文件夹名|None, 得分, 方式)。方式为
    override / alias / alias_ci / exact / alias_folder / fuzzy / contains / none 之一。
    override/alias 指向的文件夹不存在时落到后续档位（不硬失败）。

    folder_candidates：load_folder_match_candidates() 的输出
    （{工厂已知名字: [short_name, 别名...]}）；缺省时 alias_folder 档跳过。
    """
    if not folders:
        return None, 0.0, "none"

    # 1) 批次级覆盖（用户本轮确认的「仅本次生效」对照，不落盘）
    if overrides:
        ov = overrides.get(factory)
        if ov and ov in folders:
            return ov, 100.0, "override"
        if ov:
            logger.warning(
                "批次覆盖「%s」->「%s」指向的文件夹不存在,落后续匹配档",
                factory, ov)

    # 2) 别名映射表精确命中（跨语言场景的确定性解法）
    alias_hit = (alias_map or {}).get(factory)
    if alias_hit and alias_hit in folders:
        return alias_hit, 100.0, "alias"

    # 3) 别名大小写不敏感兜底：
    # Windows/macOS 文件系统不区分大小写（"TOP" 与 "Top" 是同一文件夹），
    # 但 Python 字符串比较区分大小写。Linux 文件系统大小写敏感，
    # 不敏感匹配可能匹错目录，故仅在精确匹配失败时启用并打印日志提示。
    if alias_hit:
        ci_hit = next(
            (f for f in folders if f.lower() == alias_hit.lower()), None
        )
        if ci_hit:
            logger.info(
                "别名「%s」精确匹配未命中,大小写不敏感兜底命中文件夹「%s」",
                alias_hit, ci_hit)
            return ci_hit, 100.0, "alias_ci"

    # 4) 规范化后精确命中
    norm_map = {normalize_name(f): f for f in folders}
    norm_factory = normalize_name(factory)
    if norm_factory in norm_map:
        return norm_map[norm_factory], 100.0, "exact"

    # 5) 别名/短名即文件夹名：exact 失败后、fuzzy 猜测之前，
    # 用主数据里该工厂的 short_name 与勾选「文件夹匹配」的别名
    # 本身做规范化精确匹配（确定性配置，优于 fuzzy 概率匹配）。
    # 场景：文件夹以别名命名（如文件夹「YIYI」、工厂 short_name「依依」）。
    cands = (folder_candidates or {}).get(factory) or []
    for cand in cands:
        if cand in folders:
            return cand, 100.0, "alias_folder"
    for cand in cands:
        norm_cand = normalize_name(cand)
        if norm_cand and norm_cand in norm_map:
            logger.info(
                "工厂「%s」按备选名「%s」规范化命中文件夹「%s」",
                factory, cand, norm_map[norm_cand])
            return norm_map[norm_cand], 100.0, "alias_folder"

    # 6) rapidfuzz 模糊匹配兜底
    hit = process.extractOne(
        norm_factory,
        list(norm_map.keys()),
        scorer=fuzz.ratio,
        score_cutoff=cutoff,
    )
    if hit:
        return norm_map[hit[0]], hit[1], "fuzzy"

    # 7) 规范化后包含关系兜底：短文件夹名（>=2 字）被工厂名包含，
    #    或工厂名被文件夹名包含，均视为可信命中。
    for norm_name, folder in norm_map.items():
        if len(norm_name) >= 2 and (
            norm_name in norm_factory or norm_factory in norm_name
        ):
            return folder, 70.0, "contains"

    return None, 0.0, "none"


def recommend_candidates(
    factory: str,
    folders: list[str],
    cutoff: float,
    top_n: int = 3,
) -> list[dict]:
    """为存疑工厂推荐候选文件夹（预扫「低置信推荐」档用）。

    返回 [{"folder", "score", "signals"}]，按分数降序、最多 top_n 条。
    signals 取值：
    - "contains"：规范化后工厂名包含文件夹名（如 天津市依依衛生用品 ⊃ 依依）；
    - "contained_by"：文件夹名包含工厂名。
    命中任一包含信号的候选 score 保底 70，且不受 fuzzy top-N 漏召影响。
    """
    if not folders:
        return []

    norm_factory = normalize_name(factory)
    norm_map = {normalize_name(f): f for f in folders}
    entries: dict[str, dict] = {}

    def _entry(folder: str) -> dict:
        return entries.setdefault(
            folder, {"folder": folder, "score": 0.0, "signals": []})

    # 包含强信号：全量扫描（fuzzy top-N 可能漏掉短名包含项）
    for norm_name, folder in norm_map.items():
        e = _entry(folder)
        if norm_name and norm_name in norm_factory:
            e["signals"].append("contains")
        if norm_factory and norm_factory in norm_name:
            e["signals"].append("contained_by")

    # rapidfuzz top-N
    for norm_name, score, _ in process.extract(
        norm_factory, list(norm_map.keys()), scorer=fuzz.ratio, limit=top_n
    ):
        e = _entry(norm_map[norm_name])
        e["score"] = max(e["score"], score)

    out = []
    for e in entries.values():
        if e["signals"]:
            e["score"] = max(e["score"], 70.0)
        if e["score"] >= cutoff:
            out.append(e)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_n]


def validate_subfolder(upstream_root: str | Path, folder: str) -> Path:
    """校验 folder 是 upstream_root 下现存的一级子目录名（防注入同源）。

    发起批次 alias_decisions 校验（dispatcher/tools._validate_alias_decisions）
    与运行中 retry_factory 对照注入（service.retry_factory_extraction）共用
    本函数，保证两处判定口径一致。

    拒绝规则（任一命中即 ValueError，中文消息）：
    - 空串/纯空白；
    - 含「..」路径穿越片段；
    - 含路径分隔符（/ 或 \\）或为绝对路径（folder 只能是目录名，
      不能是完整路径）；
    - 解析后不是现存目录。
    通过时返回 resolve 后的 Path。
    """
    if not folder or not folder.strip():
        raise ValueError("文件夹名不能为空")
    folder = folder.strip()
    if Path(folder).is_absolute():
        raise ValueError(f"文件夹名「{folder}」是绝对路径，请只填一级子目录名")
    if folder in (".", "..") or ".." in Path(folder).parts:
        raise ValueError(f"文件夹名「{folder}」含 .. 路径穿越，拒绝接受")
    if "/" in folder or "\\" in folder:
        raise ValueError(f"文件夹名「{folder}」含路径分隔符，请只填一级子目录名")
    p = Path(upstream_root).expanduser() / folder
    if not p.is_dir():
        raise ValueError(
            f"文件夹「{folder}」不是上游目录下现存的一级子目录，拒绝接受")
    return p.resolve()


def _save_alias_entries_to_db(entries: dict) -> dict:
    """把 alias 对照写入 factory_aliases（upsert）。异常抛给调用方回退 json。

    目标工厂解析顺序：short_name == 中文短名 → factory_name == 日文名 →
    新建 Factory（factory_name=日文名, short_name=中文短名）。
    同 alias 已指向不同 factory 时记入 overwritten（语义与 json 版一致）。"""
    overwritten: list[str] = []
    with get_session() as sess:
        for alias, short in entries.items():
            factory = (
                sess.query(Factory).filter(Factory.short_name == short).first()
                or sess.query(Factory)
                .filter(Factory.factory_name == alias)
                .first()
            )
            if factory is None:
                factory = Factory(factory_name=alias, short_name=short)
                sess.add(factory)
                sess.flush()
            row = (
                sess.query(FactoryAlias)
                .filter(FactoryAlias.alias == alias)
                .first()
            )
            if row is None:
                sess.add(FactoryAlias(
                    factory_id=factory.factory_id,
                    alias=alias,
                    use_folder_match=True,
                ))
            else:
                if row.factory_id != factory.factory_id:
                    overwritten.append(alias)
                row.factory_id = factory.factory_id
                row.use_folder_match = True
        sess.commit()
    if overwritten:
        logger.warning("alias 对照改指向其他工厂: %s", overwritten)
    return {"saved": len(entries), "overwritten": overwritten,
            "path": "db:factory_aliases"}


def save_alias_entries(
    entries: dict,
    path: str | Path | None = None,
) -> dict:
    """追加/覆盖 alias 对照。DB 为权威源，json 原子写为回退。

    未显式传 path 时优先写 DB（factory_aliases upsert）；DB 写入任何异常
    记日志并回退 json 落盘（写前备份 .bak、临时文件 + os.replace 原子写、
    模块级锁串行）。显式传 path 时只写指定 json 文件。
    返回 {"saved": 写入条数, "overwritten": [被覆盖的 key], "path": 落盘位置}。
    """
    if path is None:
        with _save_lock:
            try:
                return _save_alias_entries_to_db(entries)
            except Exception as e:
                logger.error("alias 写 DB 失败,回退 alias_map.json: %s", e)

    p = _alias_path(path)
    with _save_lock:
        existing = _load_alias_map_from_json(p)
        overwritten = [k for k, v in entries.items()
                       if k in existing and existing[k] != v]
        merged = {**existing, **entries}

        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            shutil.copy2(p, p.with_name(p.name + ".bak"))
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(tmp, p)

    if overwritten:
        logger.warning("alias 对照覆盖既有 key: %s", overwritten)
    return {"saved": len(entries), "overwritten": overwritten,
            "path": str(p)}


def load_excel_normalize_map() -> dict[str, str]:
    """Excel 工厂名归一化映射：{Excel 变体: 规范 factory_name}。

    DB（factory_aliases.use_excel_normalize=True）非空返回 DB 结果；
    DB 为空或查询异常回退 config.FACTORY_NORMALIZE_MAP。"""
    try:
        with get_session() as sess:
            rows = (
                sess.query(FactoryAlias.alias, Factory.factory_name)
                .join(Factory, FactoryAlias.factory_id == Factory.factory_id)
                .filter(FactoryAlias.use_excel_normalize.is_(True))
                .all()
            )
        db_map = {alias: name for alias, name in rows}
    except Exception as e:
        logger.warning("factory_aliases 查询失败,回退 FACTORY_NORMALIZE_MAP: %s", e)
        db_map = {}
    if db_map:
        return db_map
    return dict(get_settings().FACTORY_NORMALIZE_MAP)


def load_inspection_factories() -> list[str]:
    """商检工厂名单（factory_name 列表）。

    DB 有任一 is_inspection_factory=True 行 → 返回 DB 名单；
    否则（未标记/查询异常）回退 config.INSPECTION_FACTORIES。"""
    try:
        with get_session() as sess:
            names = [
                r[0]
                for r in sess.query(Factory.factory_name)
                .filter(Factory.is_inspection_factory.is_(True))
                .all()
            ]
    except Exception as e:
        logger.warning("factories 查询失败,回退 INSPECTION_FACTORIES: %s", e)
        names = []
    if names:
        return names
    return list(get_settings().INSPECTION_FACTORIES)
