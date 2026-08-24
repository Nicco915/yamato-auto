# -*- coding: utf-8 -*-
"""主数据维护 API：产品映射 + 品名组（/api/v1/mappings）。

端点：
- GET    /api/v1/mappings/products          映射列表（?q= 模糊搜品名/税号/供应商，?incomplete=true 只看待完善）
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
- PUT    /api/v1/mappings/skus/{sku_id}     编辑 SKU 主数据（逐字段 diff 写 sku_master_audits 留痕；品名/税号/商检变更反向回填 SKU 级映射）
- POST   /api/v1/mappings/products/batch-delete   批量删除产品映射
- POST   /api/v1/mappings/groups/batch-delete     批量删除品名组（含成员）
- POST   /api/v1/mappings/factories/batch-delete  批量删除工厂（有关联跳过）
- POST   /api/v1/mappings/skus/batch-delete       批量删除 SKU 主数据
"""

from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.db.models import (
    Factory,
    FactoryAlias,
    FactorySKU,
    ProductGroup,
    ProductGroupMember,
    ProductMapping,
    SkuMasterAudit,
)
from app.db.session import get_session
from app.db.sync import sync_mapping_to_sku, sync_sku_to_mapping

router = APIRouter(prefix="/api/v1/mappings", tags=["mappings"])

GROUP_TYPES = ("set_split", "box_share")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ProductUpsert(BaseModel):
    """产品映射新增/编辑（编辑为全量字段提交）。"""

    product_name_cn: str
    hs_code: Optional[str] = None
    supplier_name: Optional[str] = None
    inspection_required: bool = False
    name_en: Optional[str] = None
    unit_code: Optional[str] = None
    sku_code: Optional[str] = None
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


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def _product_dict(m: ProductMapping) -> dict:
    return {
        "id": m.id,
        "product_name_cn": m.product_name_cn,
        "hs_code": m.hs_code,
        "supplier_name": m.supplier_name,
        "inspection_required": bool(m.inspection_required),
        "name_en": m.name_en,
        "unit_code": m.unit_code,
        "sku_code": m.sku_code,
        "factory_id": m.factory_id,
        "is_incomplete": bool(m.is_incomplete),
        "updated_at": m.updated_at.isoformat(sep=" ") if m.updated_at else None,
    }


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
    """映射列表：q 模糊搜品名/税号/供应商/SKU；incomplete=true 只看待完善。"""
    with get_session() as s:
        query = s.query(ProductMapping)
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (ProductMapping.product_name_cn.like(like))
                | (ProductMapping.hs_code.like(like))
                | (ProductMapping.supplier_name.like(like))
                | (ProductMapping.sku_code.like(like))
            )
        if incomplete:
            query = query.filter(ProductMapping.is_incomplete.is_(True))
        rows = query.order_by(ProductMapping.id).all()
        return [_product_dict(m) for m in rows]


@router.post("/products", status_code=201)
def create_product(req: ProductUpsert):
    """新增映射。税号缺失时自动标待完善。"""
    if _blank(req.product_name_cn):
        raise HTTPException(status_code=400, detail="中文品名不能为空")
    with get_session() as s:
        m = ProductMapping(
            product_name_cn=req.product_name_cn.strip(),
            hs_code=(req.hs_code or "").strip() or None,
            supplier_name=(req.supplier_name or "").strip() or None,
            inspection_required=req.inspection_required,
            name_en=(req.name_en or "").strip() or None,
            unit_code=(req.unit_code or "").strip() or None,
            sku_code=(req.sku_code or "").strip() or None,
            factory_id=req.factory_id,
            is_incomplete=_blank(req.hs_code),
        )
        s.add(m)
        s.commit()
        s.refresh(m)
        return _product_dict(m)


@router.put("/products/{product_id}")
def update_product(product_id: int, req: ProductUpsert):
    """编辑映射：保存后 sku_code 非空时调 sync_mapping_to_sku 回填 factory_skus。

    人工补全税号后自动清除待完善标记。返回 synced_skus 便于前端提示。
    """
    if _blank(req.product_name_cn):
        raise HTTPException(status_code=400, detail="中文品名不能为空")
    with get_session() as s:
        m = s.get(ProductMapping, product_id)
        if m is None:
            raise HTTPException(status_code=404, detail=f"映射不存在: id={product_id}")
        m.product_name_cn = req.product_name_cn.strip()
        m.hs_code = (req.hs_code or "").strip() or None
        m.supplier_name = (req.supplier_name or "").strip() or None
        m.inspection_required = req.inspection_required
        m.name_en = (req.name_en or "").strip() or None
        m.unit_code = (req.unit_code or "").strip() or None
        m.sku_code = (req.sku_code or "").strip() or None
        m.factory_id = req.factory_id
        m.is_incomplete = _blank(m.hs_code)
        synced = sync_mapping_to_sku(s, m)
        s.commit()
        s.refresh(m)
        return {**_product_dict(m), "synced_skus": synced}


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
    """新增品名组（含成员）。"""
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
        s.commit()
        members = (
            s.query(ProductGroupMember)
            .filter(ProductGroupMember.group_id == g.id)
            .order_by(ProductGroupMember.display_order, ProductGroupMember.id)
            .all()
        )
        return _group_dict(g, members)


@router.put("/groups/{group_id}")
def update_group(group_id: int, req: GroupUpsert):
    """编辑品名组：成员整体替换（先删旧成员再插入）。"""
    _validate_group(req)
    with get_session() as s:
        g = _load_group(s, group_id)
        g.name = req.name.strip()
        g.group_type = req.group_type
        g.source_name_cn = req.source_name_cn.strip()
        s.query(ProductGroupMember).filter(
            ProductGroupMember.group_id == group_id
        ).delete()
        _insert_members(s, group_id, req.members)
        s.commit()
        members = (
            s.query(ProductGroupMember)
            .filter(ProductGroupMember.group_id == group_id)
            .order_by(ProductGroupMember.display_order, ProductGroupMember.id)
            .all()
        )
        return _group_dict(g, members)


@router.delete("/groups/{group_id}")
def delete_group(group_id: int):
    """删除品名组及其成员。"""
    with get_session() as s:
        g = _load_group(s, group_id)
        s.query(ProductGroupMember).filter(
            ProductGroupMember.group_id == group_id
        ).delete()
        s.delete(g)
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
    品名/税号/商检有变化时反向回填 product_mappings 的 SKU 级行
    （sync_sku_to_mapping，仅 sku_code 精确匹配；品名级行不动）。
    返回 audited_fields + synced_mappings 便于前端提示。
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
        # 反向打通：品名/英文品名/税号/商检变了才回填映射表（与正向 sync 对称的字段）
        synced = sync_sku_to_mapping(
            s, k,
            sync_name="name_cn" in audited,
            sync_name_en="name_en" in audited,
            sync_hs="hs_code" in audited,
            sync_inspection="inspection_required" in audited,
        )
        s.commit()
        s.refresh(k)
        return {**_sku_dict(k), "audited_fields": audited, "synced_mappings": synced}
