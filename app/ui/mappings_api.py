# -*- coding: utf-8 -*-
"""主数据维护 API：产品映射 + 品名组（/api/v1/mappings）。

端点：
- GET    /api/v1/mappings/products          映射列表（?q= 模糊搜品名/税号/供应商，?incomplete=true 只看待完善）
- GET    /api/v1/mappings/products/lookup-by-name   按品名精确查映射（审核页新 SKU 品名失焦自动带出税号/商检）
- POST   /api/v1/mappings/products          新增映射
- PUT    /api/v1/mappings/products/{id}     编辑映射（sku_code 非空时回填 factory_skus）
- DELETE /api/v1/mappings/products/{id}     删除映射
- GET    /api/v1/mappings/groups            品名组列表（含成员）
- POST   /api/v1/mappings/groups            新增组
- PUT    /api/v1/mappings/groups/{id}       编辑组（成员整体替换）
- DELETE /api/v1/mappings/groups/{id}       删除组
- GET    /api/v1/mappings/factories         工厂列表（含 short_name/商检标记/别名数组/SKU 数量）
- POST   /api/v1/mappings/factories         新增工厂
- PUT    /api/v1/mappings/factories/{id}    编辑工厂（规范名/短名/商检标记）
- DELETE /api/v1/mappings/factories/{id}    删除工厂（有 SKU 或别名关联时拒绝）
- POST   /api/v1/mappings/factories/{id}/aliases  新增别名
- PUT    /api/v1/mappings/aliases/{alias_id}      编辑别名（文本 + 两个用途开关）
- DELETE /api/v1/mappings/aliases/{alias_id}      删除别名
- GET    /api/v1/mappings/skus              SKU 主数据列表（?factory_id=&q= 模糊搜 SKU/品名）
- DELETE /api/v1/mappings/skus/{sku_id}     删除 SKU 主数据
- PUT    /api/v1/mappings/skus/{sku_id}     编辑 SKU 主数据（逐字段 diff 写 sku_master_audits 留痕；品名变更走 relink 移动映射归属）
- POST   /api/v1/mappings/products/batch-delete   批量删除产品映射
- POST   /api/v1/mappings/groups/batch-delete     批量删除品名组（含成员）
- POST   /api/v1/mappings/factories/batch-delete  批量删除工厂（有关联跳过）
- POST   /api/v1/mappings/skus/batch-delete       批量删除 SKU 主数据
- GET    /api/v1/mappings/ports             港口列表（首次访问若表空自动种子 5 港）
- POST   /api/v1/mappings/ports             新增港口
- PUT    /api/v1/mappings/ports/{id}        编辑港口
- DELETE /api/v1/mappings/ports/{id}        删除港口（仅影响未来生成）
"""

from __future__ import annotations

import io
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.declare.naming import ensure_ports_seeded
from app.db.models import (
    Factory,
    FactoryAlias,
    FactorySKU,
    Port,
    ProductGroup,
    ProductGroupMember,
    ProductMapping,
    ProductMappingSku,
    SkuMasterAudit,
)
from app.db.session import get_session
from app.db.sync import (
    check_sku_conflicts,
    is_mapping_incomplete,
    relink_sku_to_name,
    sync_mapping_to_sku,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mappings", tags=["mappings"])

GROUP_TYPES = ("set_split", "box_share")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ProductUpsert(BaseModel):
    """产品映射新增/编辑（编辑为全量字段提交）。

    sku_codes 为 SKU 列表（一品名多 SKU）；旧单值 sku_code 保留兼容
    （dispatcher 工具等老调用方），两者合并去重（strip、去空）成最终列表。
    """

    product_name_cn: str
    hs_code: Optional[str] = None
    supplier_name: Optional[str] = None
    inspection_required: bool = False
    name_en: Optional[str] = None
    unit_code: Optional[str] = None
    sku_codes: Optional[list[str]] = None
    sku_code: Optional[str] = None  # 【兼容】旧单值入参，并入 sku_codes
    factory_id: Optional[int] = None


class GroupMemberIn(BaseModel):
    product_name_cn: str
    display_order: Optional[int] = None   # 缺省按数组顺序
    split_price: Optional[float] = None   # set_split 用；box_share 留空
    split_net_weight: Optional[float] = None


class GroupUpsert(BaseModel):
    name: str
    group_type: str                       # set_split | box_share
    source_name_cn: str
    members: list[GroupMemberIn] = []


class FactoryUpsert(BaseModel):
    """工厂新增/编辑（编辑为全量字段提交）。"""

    factory_name: str
    short_name: Optional[str] = None
    is_inspection_factory: bool = False


class AliasUpsert(BaseModel):
    """工厂别名新增/编辑（编辑为全量字段提交）。"""

    alias: str
    use_folder_match: bool = True
    use_excel_normalize: bool = False


class SkuUpsert(BaseModel):
    """SKU 主数据编辑（全量字段提交；单件净重/毛重允许留空=None，下批次 Node4 重算）。"""

    name_cn: Optional[str] = None
    name_en: Optional[str] = None
    hs_code: Optional[str] = None
    inspection_required: bool = False
    unit_net_weight: Optional[float] = None
    unit_gross_weight: Optional[float] = None


class IdsRequest(BaseModel):
    """批量删除请求：ID 列表。"""
    ids: list[int]


class PortUpsert(BaseModel):
    """港口新增/编辑（编辑为全量字段提交；name_en/inv_letter 服务端归一化大写）。"""

    port_jp: str
    name_cn: str
    name_en: str
    inv_letter: str


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def _product_dict(m: ProductMapping) -> dict:
    """映射行序列化：sku_codes 从子表读（按 id 排序，兼容未 flush 的新行）；
    兼容保留 sku_code=列表第一个；子表为空时兜底旧列值（未迁移数据保险）。"""
    sku_codes = [
        link.sku_code
        for link in sorted(m.sku_links, key=lambda l: (l.id is None, l.id or 0))
    ]
    return {
        "id": m.id,
        "product_name_cn": m.product_name_cn,
        "hs_code": m.hs_code,
        "supplier_name": m.supplier_name,
        "inspection_required": bool(m.inspection_required),
        "name_en": m.name_en,
        "unit_code": m.unit_code,
        "sku_codes": sku_codes,
        "sku_code": sku_codes[0] if sku_codes else (m.sku_code or None),
        "factory_id": m.factory_id,
        "is_incomplete": bool(m.is_incomplete),
        "updated_at": m.updated_at.isoformat(sep=" ") if m.updated_at else None,
    }


def _merge_sku_codes(req: ProductUpsert) -> list[str]:
    """合并 sku_codes + 旧单值 sku_code：strip、去空、去重保序。"""
    raw: list[str] = list(req.sku_codes or [])
    if req.sku_code:
        raw.append(req.sku_code)
    seen: set[str] = set()
    result: list[str] = []
    for c in raw:
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _raise_if_sku_conflict(s, sku_codes: list[str], exclude_id: Optional[int] = None) -> None:
    """冲突拦截：列表中任一 SKU 已被其他映射行占用 → 409（中文列出全部冲突）。

    先于任何写入调用，保证整体不落库（调用方 commit 前抛出）。
    """
    conflicts = check_sku_conflicts(s, sku_codes, exclude_mapping_id=exclude_id)
    if not conflicts:
        return
    parts = [
        f"SKU {c['sku_code']} 已被映射「{c['product_name_cn']}」(id={c['mapping_id']}) 占用"
        for c in conflicts
    ]
    raise HTTPException(status_code=409, detail="；".join(parts))


def _replace_sku_links(s, m: ProductMapping, sku_codes: list[str]) -> None:
    """整体替换映射行的 SKU 关联子表（先清后插，中间 flush 一次）。

    不能直接 `m.sku_links = [新列表]`：collection 整体赋值在同一次 flush 里
    先 INSERT 后 DELETE，保留不变的 SKU 会撞 unique_mapping_sku 唯一约束。
    同时把旧列 sku_code 同步为列表第一个/None：旧列已废弃不写新值，
    但保持与列表一致可防止启动迁移把已删 SKU 幽灵搬回（回滚保险语义不变）。
    """
    m.sku_links.clear()  # delete-orphan 级联：标记删除
    s.flush()            # 先落删除，再插新行，避免唯一约束撞车
    m.sku_links.extend(ProductMappingSku(sku_code=c) for c in sku_codes)
    m.sku_code = sku_codes[0] if sku_codes else None


def _group_dict(g: ProductGroup, members: list[ProductGroupMember]) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "group_type": g.group_type,
        "source_name_cn": g.source_name_cn,
        "members": [
            {
                "id": mb.id,
                "product_name_cn": mb.product_name_cn,
                "display_order": mb.display_order,
                "split_price": mb.split_price,
                "split_net_weight": mb.split_net_weight,
            }
            for mb in members
        ],
    }


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ---------------------------------------------------------------------------
# 产品映射
# ---------------------------------------------------------------------------

@router.get("/products")
def list_products(
    q: Optional[str] = Query(default=None),
    incomplete: bool = Query(default=False),
):
    """映射列表：q 模糊搜品名/税号/供应商/SKU（含子表多 SKU）；incomplete=true 只看待完善。"""
    from sqlalchemy.orm import selectinload

    with get_session() as s:
        query = s.query(ProductMapping).options(selectinload(ProductMapping.sku_links))
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (ProductMapping.product_name_cn.like(like))
                | (ProductMapping.hs_code.like(like))
                | (ProductMapping.supplier_name.like(like))
                | (ProductMapping.sku_code.like(like))  # 旧列兜底（未迁移数据）
                | (ProductMapping.sku_links.any(ProductMappingSku.sku_code.like(like)))
            )
        if incomplete:
            query = query.filter(ProductMapping.is_incomplete.is_(True))
        rows = query.order_by(ProductMapping.id).all()
        return [_product_dict(m) for m in rows]


@router.get("/products/lookup-by-name")
def lookup_product_by_name(name: str = Query(default="")):
    """按中文品名**精确**查产品映射（审核页新 SKU 填完品名失焦时自动带出税号/商检）。

    精确匹配（strip 前后空白，不用 LIKE，避免误带出相似品名）；
    同品名多行时取 updated_at 最新的一条，并带 ambiguous=true（前端可提示可不处理）。
    未命中返回 {"found": false}（前端静默，不打断审核流）。
    """
    key = (name or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="品名不能为空")
    with get_session() as s:
        rows = (
            s.query(ProductMapping)
            .filter(ProductMapping.product_name_cn == key)
            # 最新更新优先；updated_at 可能为 NULL 或同秒并列，id 倒序兜底保证确定性
            .order_by(ProductMapping.updated_at.desc(), ProductMapping.id.desc())
            .all()
        )
        if not rows:
            return {"found": False}
        m = rows[0]
        return {
            "found": True,
            "product_name_cn": m.product_name_cn,
            "hs_code": m.hs_code,
            "inspection_required": bool(m.inspection_required),
            "name_en": m.name_en,
            "unit_code": m.unit_code,
            "ambiguous": len(rows) > 1,
        }


@router.post("/products", status_code=201)
def create_product(req: ProductUpsert):
    """新增映射（多 SKU：写子表）。待完善统一口径：unit_code 空且品名非品名组源品名。

    冲突拦截：任一 SKU 已被其他映射行占用 → 409，整体不落库（先全量校验再写入）。
    """
    if _blank(req.product_name_cn):
        raise HTTPException(status_code=400, detail="中文品名不能为空")
    sku_codes = _merge_sku_codes(req)
    with get_session() as s:
        _raise_if_sku_conflict(s, sku_codes)
        m = ProductMapping(
            product_name_cn=req.product_name_cn.strip(),
            hs_code=(req.hs_code or "").strip() or None,
            supplier_name=(req.supplier_name or "").strip() or None,
            inspection_required=req.inspection_required,
            name_en=(req.name_en or "").strip() or None,
            unit_code=(req.unit_code or "").strip() or None,
            factory_id=req.factory_id,
        )
        m.is_incomplete = is_mapping_incomplete(s, m.product_name_cn, m.unit_code)
        _replace_sku_links(s, m, sku_codes)
        s.add(m)
        s.commit()
        s.refresh(m)
        return _product_dict(m)


@router.put("/products/{product_id}")
def update_product(product_id: int, req: ProductUpsert):
    """编辑映射（多 SKU：子表整体替换）：保存后调 sync_mapping_to_sku 批量回填 SKU 的 name_cn。

    冲突拦截：任一 SKU 已被其他映射行占用 → 409（排除本行），整体不落库。
    待完善统一口径：unit_code 空且品名非品名组源品名（is_mapping_incomplete）。
    联动 3：品名改名（strip 后有变化）时，同事务同步 product_groups.source_name_cn
    与 product_group_members.product_name_cn 里的旧品名 → 新品名，响应带
    renamed_groups / renamed_members（0 也返回，前端据此拼提示）。
    返回 synced_skus 便于前端提示。
    """
    if _blank(req.product_name_cn):
        raise HTTPException(status_code=400, detail="中文品名不能为空")
    sku_codes = _merge_sku_codes(req)
    with get_session() as s:
        m = s.get(ProductMapping, product_id)
        if m is None:
            raise HTTPException(status_code=404, detail=f"映射不存在: id={product_id}")
        _raise_if_sku_conflict(s, sku_codes, exclude_id=product_id)
        old_name = (m.product_name_cn or "").strip()
        m.product_name_cn = req.product_name_cn.strip()
        m.hs_code = (req.hs_code or "").strip() or None
        m.supplier_name = (req.supplier_name or "").strip() or None
        m.inspection_required = req.inspection_required
        m.name_en = (req.name_en or "").strip() or None
        m.unit_code = (req.unit_code or "").strip() or None
        m.factory_id = req.factory_id
        # 联动 3：品名改名 → 品名组三表同步（组源品名 + 组员品名），同一事务
        renamed_groups = 0
        renamed_members = 0
        if old_name != m.product_name_cn:
            renamed_groups = (
                s.query(ProductGroup)
                .filter(ProductGroup.source_name_cn == old_name)
                .update(
                    {ProductGroup.source_name_cn: m.product_name_cn},
                    synchronize_session=False,
                )
            )
            renamed_members = (
                s.query(ProductGroupMember)
                .filter(ProductGroupMember.product_name_cn == old_name)
                .update(
                    {ProductGroupMember.product_name_cn: m.product_name_cn},
                    synchronize_session=False,
                )
            )
            if renamed_groups or renamed_members:
                logger.info(
                    "[品名组联动] 映射行改名「%s」→「%s」：同步组源 %d 行、组员 %d 行",
                    old_name, m.product_name_cn, renamed_groups, renamed_members,
                )
        # 待完善重算放在改名同步之后：新品名可能因此成为组源（豁免待完善）
        m.is_incomplete = is_mapping_incomplete(s, m.product_name_cn, m.unit_code)
        _replace_sku_links(s, m, sku_codes)
        synced = sync_mapping_to_sku(s, m)
        s.commit()
        s.refresh(m)
        return {
            **_product_dict(m),
            "synced_skus": synced,
            "renamed_groups": renamed_groups,
            "renamed_members": renamed_members,
        }


@router.delete("/products/{product_id}")
def delete_product(product_id: int):
    """删除映射（不影响 factory_skus 主数据）。"""
    with get_session() as s:
        m = s.get(ProductMapping, product_id)
        if m is None:
            raise HTTPException(status_code=404, detail=f"映射不存在: id={product_id}")
        s.delete(m)
        s.commit()
        return {"deleted": product_id}


@router.post("/products/import")
async def import_products(file: UploadFile):
    """Excel 批量导入产品映射（与 scripts/import_product_mappings.py 同逻辑）。

    接受 .xlsx 上传（Sheet1 或首个 sheet：产品|税号|供应商|商检|产品组一|自定义七），
    按 (品名, 供应商) 幂等 upsert。返回 {created, updated, total}。
    """
    from app.declare.mapping_import import parse_mapping_rows, upsert_mappings

    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="只支持 .xlsx 文件")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件为空")
    try:
        rows = parse_mapping_rows(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Excel 解析失败: {e}")
    if not rows:
        raise HTTPException(status_code=400, detail="未解析到任何映射行（检查表头与列顺序）")
    with get_session() as s:
        created, updated = upsert_mappings(s, rows)
        s.commit()
    return {"created": created, "updated": updated, "total": created + updated}


@router.post("/products/batch-delete")
def batch_delete_products(req: IdsRequest):
    """批量删除产品映射。"""
    deleted, failed = 0, []
    with get_session() as s:
        for pid in req.ids:
            m = s.get(ProductMapping, pid)
            if m is None:
                failed.append({"id": pid, "reason": "映射不存在"})
            else:
                s.delete(m)
                deleted += 1
        s.commit()
    return {"deleted": deleted, "failed": failed}


# ---------------------------------------------------------------------------
# 品名组
# ---------------------------------------------------------------------------

def _validate_group(req: GroupUpsert) -> None:
    if req.group_type not in GROUP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"group_type 必须是 {'/'.join(GROUP_TYPES)}",
        )
    if _blank(req.name):
        raise HTTPException(status_code=400, detail="组名不能为空")
    if _blank(req.source_name_cn):
        raise HTTPException(status_code=400, detail="源品名不能为空")
    if not req.members:
        raise HTTPException(status_code=400, detail="至少需要一个成员")
    for mb in req.members:
        if _blank(mb.product_name_cn):
            raise HTTPException(status_code=400, detail="成员品名不能为空")


def _insert_members(s, group_id: int, members: list[GroupMemberIn]) -> None:
    for i, mb in enumerate(members):
        s.add(
            ProductGroupMember(
                group_id=group_id,
                product_name_cn=mb.product_name_cn.strip(),
                display_order=mb.display_order if mb.display_order is not None else i,
                split_price=mb.split_price,
                split_net_weight=mb.split_net_weight,
            )
        )


def _load_group(s, group_id: int) -> ProductGroup:
    g = s.get(ProductGroup, group_id)
    if g is None:
        raise HTTPException(status_code=404, detail=f"品名组不存在: id={group_id}")
    return g


def _ensure_member_mappings(s, member_names: list[str]) -> list[str]:
    """联动 2 组员兜底：组员品名在 product_mappings 无行 → 自动建品名级行。

    入参先 strip/去空/去重；新建行除品名外全部留空/默认，
    is_incomplete 走统一口径 is_mapping_incomplete（unit_code 必为空，
    组员品名一般非组源 → True，待补单位代码）。
    返回本次新建的品名列表（供响应 created_member_mappings）。
    """
    names: list[str] = []
    seen: set[str] = set()
    for n in member_names:
        n = (n or "").strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    created: list[str] = []
    for name in names:
        exists = (
            s.query(ProductMapping)
            .filter(ProductMapping.product_name_cn == name)
            .first()
        )
        if exists is not None:
            continue
        s.add(ProductMapping(
            product_name_cn=name,
            is_incomplete=is_mapping_incomplete(s, name, None),
        ))
        created.append(name)
    if created:
        s.flush()
        logger.info("[品名组联动] 组员兜底自动建映射行 %d 条: %s", len(created), created)
    return created


def _recalc_source_incomplete(s, source_name_cn: Optional[str]) -> None:
    """联动 2 组源行待完善重算：品名 == source_name_cn 的映射行按统一口径重算。

    成为组源（建组/改组源）后 unit_code 空也豁免待完善；
    脱离组源身份（改组源/删组）后 unit_code 空要重新标待完善。
    调用方保证组表变更已 flush（查询能见到最新组源身份）。
    """
    name = (source_name_cn or "").strip()
    if not name:
        return
    rows = (
        s.query(ProductMapping)
        .filter(ProductMapping.product_name_cn == name)
        .all()
    )
    for m in rows:
        want = is_mapping_incomplete(s, m.product_name_cn, m.unit_code)
        if bool(m.is_incomplete) != want:
            m.is_incomplete = want
            logger.info(
                "[品名组联动] 组源行待完善重算: 品名「%s」is_incomplete → %s",
                name, want,
            )


@router.get("/groups")
def list_groups():
    """品名组列表（含成员，按 display_order 排序）。"""
    with get_session() as s:
        groups = s.query(ProductGroup).order_by(ProductGroup.id).all()
        result = []
        for g in groups:
            members = (
                s.query(ProductGroupMember)
                .filter(ProductGroupMember.group_id == g.id)
                .order_by(ProductGroupMember.display_order, ProductGroupMember.id)
                .all()
            )
            result.append(_group_dict(g, members))
        return result


@router.post("/groups", status_code=201)
def create_group(req: GroupUpsert):
    """新增品名组（含成员）。

    联动 2（同事务）：组员品名在 product_mappings 无行 → 自动补建品名级行
    （响应带 created_member_mappings）；组源品名对应映射行按统一口径重算
    待完善（成为组源后 unit_code 空也豁免）。
    """
    _validate_group(req)
    with get_session() as s:
        g = ProductGroup(
            name=req.name.strip(),
            group_type=req.group_type,
            source_name_cn=req.source_name_cn.strip(),
        )
        s.add(g)
        s.flush()
        _insert_members(s, g.id, req.members)
        created = _ensure_member_mappings(s, [mb.product_name_cn for mb in req.members])
        _recalc_source_incomplete(s, g.source_name_cn)
        s.commit()
        members = (
            s.query(ProductGroupMember)
            .filter(ProductGroupMember.group_id == g.id)
            .order_by(ProductGroupMember.display_order, ProductGroupMember.id)
            .all()
        )
        return {**_group_dict(g, members), "created_member_mappings": created}


@router.put("/groups/{group_id}")
def update_group(group_id: int, req: GroupUpsert):
    """编辑品名组：成员整体替换（先删旧成员再插入）。

    联动 2（同事务）：组员兜底补建映射行；新/旧组源品名对应映射行都按
    统一口径重算待完善（旧组源脱离身份后 unit_code 空要重新标待完善）。
    注意：品名组 Tab 改 source_name_cn/组员品名**不**反向改映射行品名
    （反方向改名同步仅「映射行 → 品名组」，见 update_product）。
    """
    _validate_group(req)
    with get_session() as s:
        g = _load_group(s, group_id)
        old_source = (g.source_name_cn or "").strip()
        g.name = req.name.strip()
        g.group_type = req.group_type
        g.source_name_cn = req.source_name_cn.strip()
        s.query(ProductGroupMember).filter(
            ProductGroupMember.group_id == group_id
        ).delete()
        _insert_members(s, group_id, req.members)
        created = _ensure_member_mappings(s, [mb.product_name_cn for mb in req.members])
        _recalc_source_incomplete(s, g.source_name_cn)
        if old_source != g.source_name_cn:
            _recalc_source_incomplete(s, old_source)
        s.commit()
        members = (
            s.query(ProductGroupMember)
            .filter(ProductGroupMember.group_id == group_id)
            .order_by(ProductGroupMember.display_order, ProductGroupMember.id)
            .all()
        )
        return {**_group_dict(g, members), "created_member_mappings": created}


@router.delete("/groups/{group_id}")
def delete_group(group_id: int):
    """删除品名组及其成员。

    联动 2（同事务）：被删组的 source_name_cn 对应映射行按统一口径重算
    待完善（脱离组源身份后 unit_code 空重新标待完善）。
    """
    with get_session() as s:
        g = _load_group(s, group_id)
        source_name = g.source_name_cn
        s.query(ProductGroupMember).filter(
            ProductGroupMember.group_id == group_id
        ).delete()
        s.delete(g)
        s.flush()  # 先落删除，重算时组源身份已解除
        _recalc_source_incomplete(s, source_name)
        s.commit()
        return {"deleted": group_id}


@router.post("/groups/batch-delete")
def batch_delete_groups(req: IdsRequest):
    """批量删除品名组（含成员）。"""
    deleted, failed = 0, []
    with get_session() as s:
        for gid in req.ids:
            g = s.get(ProductGroup, gid)
            if g is None:
                failed.append({"id": gid, "reason": "品名组不存在"})
            else:
                s.query(ProductGroupMember).filter(ProductGroupMember.group_id == gid).delete()
                s.delete(g)
                deleted += 1
        s.commit()
    return {"deleted": deleted, "failed": failed}


# ---------------------------------------------------------------------------
# 工厂与别名
# ---------------------------------------------------------------------------

def _alias_dict(a: FactoryAlias) -> dict:
    return {
        "id": a.id,
        "factory_id": a.factory_id,
        "alias": a.alias,
        "use_folder_match": bool(a.use_folder_match),
        "use_excel_normalize": bool(a.use_excel_normalize),
    }


def _factory_dict(f: Factory, aliases: list[FactoryAlias], sku_count: int) -> dict:
    return {
        "id": f.factory_id,
        "factory_name": f.factory_name,
        "short_name": f.short_name,
        "is_inspection_factory": bool(f.is_inspection_factory),
        "aliases": [_alias_dict(a) for a in aliases],
        "sku_count": sku_count,
    }


def _load_factory(s, factory_id: int) -> Factory:
    f = s.get(Factory, factory_id)
    if f is None:
        raise HTTPException(status_code=404, detail=f"工厂不存在: id={factory_id}")
    return f


@router.get("/factories")
def list_factories():
    """工厂列表：含短名、商检标记、别名数组、SKU 数量。"""
    with get_session() as s:
        factories = s.query(Factory).order_by(Factory.factory_id).all()
        result = []
        for f in factories:
            aliases = (
                s.query(FactoryAlias)
                .filter(FactoryAlias.factory_id == f.factory_id)
                .order_by(FactoryAlias.id)
                .all()
            )
            sku_count = (
                s.query(FactorySKU)
                .filter(FactorySKU.factory_id == f.factory_id)
                .count()
            )
            result.append(_factory_dict(f, aliases, sku_count))
        return result


@router.post("/factories", status_code=201)
def create_factory(req: FactoryUpsert):
    """新增工厂。规范名唯一。"""
    if _blank(req.factory_name):
        raise HTTPException(status_code=400, detail="工厂规范名不能为空")
    with get_session() as s:
        name = req.factory_name.strip()
        dup = s.query(Factory).filter(Factory.factory_name == name).first()
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"工厂已存在: {name} (id={dup.factory_id})")
        f = Factory(
            factory_name=name,
            short_name=(req.short_name or "").strip() or None,
            is_inspection_factory=req.is_inspection_factory,
        )
        s.add(f)
        s.commit()
        s.refresh(f)
        return _factory_dict(f, [], 0)


@router.put("/factories/{factory_id}")
def update_factory(factory_id: int, req: FactoryUpsert):
    """编辑工厂：规范名/中文短名/商检工厂标记。短名留空即置 NULL（待补录）。"""
    if _blank(req.factory_name):
        raise HTTPException(status_code=400, detail="工厂规范名不能为空")
    with get_session() as s:
        f = _load_factory(s, factory_id)
        name = req.factory_name.strip()
        dup = (
            s.query(Factory)
            .filter(Factory.factory_name == name, Factory.factory_id != factory_id)
            .first()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"工厂规范名已被占用: {name} (id={dup.factory_id})")
        f.factory_name = name
        f.short_name = (req.short_name or "").strip() or None
        f.is_inspection_factory = req.is_inspection_factory
        s.commit()
        s.refresh(f)
        aliases = (
            s.query(FactoryAlias)
            .filter(FactoryAlias.factory_id == factory_id)
            .order_by(FactoryAlias.id)
            .all()
        )
        sku_count = (
            s.query(FactorySKU).filter(FactorySKU.factory_id == factory_id).count()
        )
        return _factory_dict(f, aliases, sku_count)


@router.delete("/factories/{factory_id}")
def delete_factory(factory_id: int):
    """删除工厂：有 SKU 或别名关联时拒绝（400 说明原因）。"""
    with get_session() as s:
        f = _load_factory(s, factory_id)
        sku_count = (
            s.query(FactorySKU).filter(FactorySKU.factory_id == factory_id).count()
        )
        alias_count = (
            s.query(FactoryAlias).filter(FactoryAlias.factory_id == factory_id).count()
        )
        if sku_count or alias_count:
            parts = []
            if sku_count:
                parts.append(f"{sku_count} 条 SKU 主数据")
            if alias_count:
                parts.append(f"{alias_count} 条别名")
            raise HTTPException(
                status_code=400,
                detail=f"工厂「{f.factory_name}」下仍有 {' 和 '.join(parts)}，请先清理关联后再删除",
            )
        s.delete(f)
        s.commit()
        return {"deleted": factory_id}


@router.post("/factories/batch-delete")
def batch_delete_factories(req: IdsRequest):
    """批量删除工厂：有关联的工厂跳过并报告原因。"""
    deleted, failed = 0, []
    with get_session() as s:
        for fid in req.ids:
            f = s.get(Factory, fid)
            if f is None:
                failed.append({"id": fid, "reason": "工厂不存在"})
                continue
            sku_count = s.query(FactorySKU).filter(FactorySKU.factory_id == fid).count()
            alias_count = s.query(FactoryAlias).filter(FactoryAlias.factory_id == fid).count()
            if sku_count or alias_count:
                parts = []
                if sku_count:
                    parts.append(f"{sku_count} 条 SKU")
                if alias_count:
                    parts.append(f"{alias_count} 条别名")
                failed.append({"id": fid, "reason": f"有 {' 和 '.join(parts)} 关联"})
            else:
                s.delete(f)
                deleted += 1
        s.commit()
    return {"deleted": deleted, "failed": failed}


@router.post("/factories/{factory_id}/aliases", status_code=201)
def create_alias(factory_id: int, req: AliasUpsert):
    """新增工厂别名（两个用途开关至少开一个）。"""
    if _blank(req.alias):
        raise HTTPException(status_code=400, detail="别名不能为空")
    if not req.use_folder_match and not req.use_excel_normalize:
        raise HTTPException(status_code=400, detail="文件夹匹配 / Excel 归一化至少勾选一个用途")
    with get_session() as s:
        _load_factory(s, factory_id)
        alias = req.alias.strip()
        dup = (
            s.query(FactoryAlias)
            .filter(FactoryAlias.factory_id == factory_id, FactoryAlias.alias == alias)
            .first()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"该工厂下别名已存在: {alias} (id={dup.id})")
        a = FactoryAlias(
            factory_id=factory_id,
            alias=alias,
            use_folder_match=req.use_folder_match,
            use_excel_normalize=req.use_excel_normalize,
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        return _alias_dict(a)


@router.put("/aliases/{alias_id}")
def update_alias(alias_id: int, req: AliasUpsert):
    """编辑别名：文本 + 两个用途开关。"""
    if _blank(req.alias):
        raise HTTPException(status_code=400, detail="别名不能为空")
    if not req.use_folder_match and not req.use_excel_normalize:
        raise HTTPException(status_code=400, detail="文件夹匹配 / Excel 归一化至少勾选一个用途")
    with get_session() as s:
        a = s.get(FactoryAlias, alias_id)
        if a is None:
            raise HTTPException(status_code=404, detail=f"别名不存在: id={alias_id}")
        alias = req.alias.strip()
        dup = (
            s.query(FactoryAlias)
            .filter(
                FactoryAlias.factory_id == a.factory_id,
                FactoryAlias.alias == alias,
                FactoryAlias.id != alias_id,
            )
            .first()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"该工厂下别名已存在: {alias} (id={dup.id})")
        a.alias = alias
        a.use_folder_match = req.use_folder_match
        a.use_excel_normalize = req.use_excel_normalize
        s.commit()
        s.refresh(a)
        return _alias_dict(a)


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int):
    """删除别名。"""
    with get_session() as s:
        a = s.get(FactoryAlias, alias_id)
        if a is None:
            raise HTTPException(status_code=404, detail=f"别名不存在: id={alias_id}")
        s.delete(a)
        s.commit()
        return {"deleted": alias_id}


# ---------------------------------------------------------------------------
# SKU 主数据（可编辑 + 完整留痕）
# ---------------------------------------------------------------------------

def _sku_dict(k: FactorySKU) -> dict:
    return {
        "sku_id": k.sku_id,
        "factory_id": k.factory_id,
        "sku_code": k.sku_code,
        "name_cn": k.name_cn,
        "name_en": k.name_en,
        "hs_code": k.hs_code,
        "inspection_required": bool(k.inspection_required),
        "unit_net_weight": float(k.unit_net_weight) if k.unit_net_weight is not None else None,
        "unit_gross_weight": float(k.unit_gross_weight) if k.unit_gross_weight is not None else None,
        "updated_at": k.updated_at.isoformat(sep=" ") if k.updated_at else None,
    }


@router.get("/skus")
def list_skus(
    factory_id: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None),
):
    """SKU 主数据列表：factory_id 精确筛选；q 模糊搜 SKU 编码/中文品名/英文品名。"""
    with get_session() as s:
        query = s.query(FactorySKU)
        if factory_id is not None:
            query = query.filter(FactorySKU.factory_id == factory_id)
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (FactorySKU.sku_code.like(like))
                | (FactorySKU.name_cn.like(like))
                | (FactorySKU.name_en.like(like))
            )
        rows = query.order_by(FactorySKU.factory_id, FactorySKU.sku_code).all()
        return [_sku_dict(k) for k in rows]


@router.delete("/skus/{sku_id}")
def delete_sku(sku_id: int):
    """删除 SKU 主数据。不检查引用、不级联。"""
    with get_session() as s:
        k = s.get(FactorySKU, sku_id)
        if k is None:
            raise HTTPException(status_code=404, detail=f"SKU 主数据不存在: id={sku_id}")
        code = k.sku_code
        s.delete(k)
        s.commit()
        return {"deleted": code}


@router.post("/skus/batch-delete")
def batch_delete_skus(req: IdsRequest):
    """批量删除 SKU 主数据。不检查引用、不级联。"""
    deleted, failed = 0, []
    with get_session() as s:
        for sid in req.ids:
            k = s.get(FactorySKU, sid)
            if k is None:
                failed.append({"id": sid, "reason": "SKU 不存在"})
            else:
                s.delete(k)
                deleted += 1
        s.commit()
    return {"deleted": deleted, "failed": failed}


# 可编辑字段 → 留痕字段名（与 SkuMasterAudit.field 对应）
_SKU_EDITABLE_FIELDS = (
    "name_cn",
    "name_en",
    "hs_code",
    "inspection_required",
    "unit_net_weight",
    "unit_gross_weight",
)


def _audit_str(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@router.put("/skus/{sku_id}")
def update_sku(sku_id: int, req: SkuUpsert):
    """编辑 SKU 主数据：逐字段 diff，有变化的字段各写一条 sku_master_audits。

    单件净重/毛重允许留空（None）：每批次 Node4 重新计算，DB 值仅作比对参考。
    SKU 侧任何字段变化都不再反向回填映射行（旧 sync_sku_to_mapping 已废弃）；
    仅品名（name_cn）变化时调 relink_sku_to_name 移动映射归属：先从所有映射行
    摘除（行保留），再按新品名挂接（命中追加/未命中建行）；品名清空 → 只摘除。
    返回 audited_fields + relink（无归属变动时为 None）便于前端提示。
    """
    with get_session() as s:
        k = s.get(FactorySKU, sku_id)
        if k is None:
            raise HTTPException(status_code=404, detail=f"SKU 主数据不存在: id={sku_id}")
        new_values = {
            "name_cn": (req.name_cn or "").strip() or None,
            "name_en": (req.name_en or "").strip() or None,
            "hs_code": (req.hs_code or "").strip() or None,
            "inspection_required": req.inspection_required,
            "unit_net_weight": req.unit_net_weight,
            "unit_gross_weight": req.unit_gross_weight,
        }
        audited = []
        for field in _SKU_EDITABLE_FIELDS:
            old = getattr(k, field)
            new = new_values[field]
            if field in ("unit_net_weight", "unit_gross_weight"):
                old = float(old) if old is not None else None
            if old == new:
                continue
            s.add(
                SkuMasterAudit(
                    sku_code=k.sku_code,
                    field=field,
                    old_value=_audit_str(old),
                    new_value=_audit_str(new),
                )
            )
            setattr(k, field, new)
            audited.append(field)
        # 品名变更 → 移动映射归属（同一 session 同一事务；此时 k 已是新值，
        # name_cn 清空 → None → 只摘除不挂接）
        relink = None
        if "name_cn" in audited:
            result = relink_sku_to_name(
                s,
                sku_code=k.sku_code,
                name_cn=k.name_cn,
                hs_code=k.hs_code,
                inspection_required=k.inspection_required,
                name_en=k.name_en,
            )
            if result["detached_from"] or result["action"] is not None:
                relink = {
                    # list 套 list 保证 JSON 可序列化（元组会原样转数组，显式转换更稳）
                    "detached_from": [[mid, name] for mid, name in result["detached_from"]],
                    "action": result["action"],
                }
        s.commit()
        s.refresh(k)
        return {**_sku_dict(k), "audited_fields": audited, "relink": relink}


# ---------------------------------------------------------------------------
# 港口主数据（报关票名/英文名/发票字母；inv_letter 全表唯一）
# ---------------------------------------------------------------------------

def _port_dict(p: Port) -> dict:
    return {
        "id": p.id,
        "port_jp": p.port_jp,
        "name_cn": p.name_cn,
        "name_en": p.name_en,
        "inv_letter": p.inv_letter,
        "updated_at": p.updated_at.isoformat(sep=" ") if p.updated_at else None,
    }


def _normalize_port(req: PortUpsert) -> tuple[str, str, str, str]:
    """字段归一化 + 必填/格式校验。name_en/inv_letter 转大写。"""
    port_jp = (req.port_jp or "").strip()
    name_cn = (req.name_cn or "").strip()
    name_en = (req.name_en or "").strip().upper()
    inv_letter = (req.inv_letter or "").strip().upper()
    if not port_jp:
        raise HTTPException(status_code=400, detail="港口原名不能为空")
    if not name_cn:
        raise HTTPException(status_code=400, detail="中文名不能为空")
    if not name_en:
        raise HTTPException(status_code=400, detail="英文名不能为空")
    if len(inv_letter) != 1 or not ("A" <= inv_letter <= "Z"):
        raise HTTPException(status_code=400, detail="发票字母必须是 A-Z 单字符")
    return port_jp, name_cn, name_en, inv_letter


def _check_inv_letter_free(s, inv_letter: str, exclude_id: Optional[int] = None) -> None:
    """inv_letter 全表唯一（撞字母会导致发票号串票）。冲突 → 409 说明占用方。"""
    q = s.query(Port).filter(Port.inv_letter == inv_letter)
    if exclude_id is not None:
        q = q.filter(Port.id != exclude_id)
    dup = q.first()
    if dup is not None:
        raise HTTPException(
            status_code=409,
            detail=f"发票字母 {inv_letter} 已被港口「{dup.port_jp}」占用，请换一个字母",
        )


@router.get("/ports")
def list_ports():
    """港口列表。首次访问若表空自动把硬编码 5 港种子入库（幂等）。"""
    ensure_ports_seeded()
    with get_session() as s:
        rows = s.query(Port).order_by(Port.id).all()
        return [_port_dict(p) for p in rows]


@router.post("/ports", status_code=201)
def create_port(req: PortUpsert):
    """新增港口。port_jp 唯一；inv_letter 全表唯一（冲突 409）。"""
    port_jp, name_cn, name_en, inv_letter = _normalize_port(req)
    with get_session() as s:
        dup = s.query(Port).filter(Port.port_jp == port_jp).first()
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"港口已存在: {port_jp} (id={dup.id})")
        _check_inv_letter_free(s, inv_letter)
        p = Port(
            port_jp=port_jp,
            name_cn=name_cn,
            name_en=name_en,
            inv_letter=inv_letter,
        )
        s.add(p)
        s.commit()
        s.refresh(p)
        logger.info("港口主数据新增: %s (%s/%s/%s)", port_jp, name_cn, name_en, inv_letter)
        return _port_dict(p)


@router.put("/ports/{port_id}")
def update_port(port_id: int, req: PortUpsert):
    """编辑港口：四字段全量提交。保存后立即生效（解析无缓存）。"""
    port_jp, name_cn, name_en, inv_letter = _normalize_port(req)
    with get_session() as s:
        p = s.get(Port, port_id)
        if p is None:
            raise HTTPException(status_code=404, detail=f"港口不存在: id={port_id}")
        dup = (
            s.query(Port)
            .filter(Port.port_jp == port_jp, Port.id != port_id)
            .first()
        )
        if dup is not None:
            raise HTTPException(status_code=400, detail=f"港口原名已被占用: {port_jp} (id={dup.id})")
        _check_inv_letter_free(s, inv_letter, exclude_id=port_id)
        old = f"{p.port_jp} ({p.name_cn}/{p.name_en}/{p.inv_letter})"
        p.port_jp = port_jp
        p.name_cn = name_cn
        p.name_en = name_en
        p.inv_letter = inv_letter
        s.commit()
        s.refresh(p)
        logger.info("港口主数据更新: id=%s %s → %s", port_id, old,
                    f"{port_jp} ({name_cn}/{name_en}/{inv_letter})")
        return _port_dict(p)


@router.delete("/ports/{port_id}")
def delete_port(port_id: int):
    """删除港口。不查引用——影响仅限未来生成（已生成的文件不受影响）。"""
    with get_session() as s:
        p = s.get(Port, port_id)
        if p is None:
            raise HTTPException(status_code=404, detail=f"港口不存在: id={port_id}")
        label = p.port_jp
        s.delete(p)
        s.commit()
        logger.info("港口主数据删除: %s (id=%s)", label, port_id)
        return {"deleted": port_id}
