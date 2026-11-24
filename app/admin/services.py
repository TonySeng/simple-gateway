"""服务元信息 —— 前端据此动态渲染厂商表单，新增服务无需改前端。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.core.registry import all_services, service_meta

router = APIRouter(prefix="/api/admin/services", tags=["管理"])


@router.get("", summary="服务清单与厂商表单元信息")
def list_services(admin: str = Depends(get_current_admin)):
    return {"services": service_meta()}


@router.get("/keys", summary="服务 key 列表")
def keys(admin: str = Depends(get_current_admin)):
    return {"keys": [s.key for s in all_services()]}
