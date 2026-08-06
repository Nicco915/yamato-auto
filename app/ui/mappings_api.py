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
"""

from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, UploadFile
from pydantic import BaseModel

from app.db.models import ProductGroup, ProductGroupMember, ProductMapping
from app.db.session import get_session
from app.db.sync import sync_mapping_to_sku

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
    """映射列表：q 模糊搜品名/税号/供应商；incomplete=true 只看待完善。"""
    with get_session() as s:
        query = s.query(ProductMapping)
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(
                (ProductMapping.product_name_cn.like(like))
                | (ProductMapping.hs_code.like(like))
                | (ProductMapping.supplier_name.like(like))
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
