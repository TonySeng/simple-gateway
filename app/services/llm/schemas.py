"""LLM 相关的管理端数据契约与新模型拉取接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.models import Model, Provider
from app.services.llm import proxy
from app.services.llm.dispatch import build_models_request

# ---------------- 契约 ----------------
class FetchedModel(BaseModel):
    id: str
    owned_by: str = ""
    context_window: int = 0


class FetchModelsResult(BaseModel):
    ok: bool
    message: str = ""
    models: list[FetchedModel] = []


class ModelCreate(BaseModel):
    provider_id: str
    name: str
    provider_model: str = ""
    input_price: float = 0.0
    output_price: float = 0.0
    currency: str = "CNY"
    context_window: int = 0
    is_active: bool = True


class ModelUpdate(BaseModel):
    name: Optional[str] = None
    provider_model: Optional[str] = None
    input_price: Optional[float] = None
    output_price: Optional[float] = None
    currency: Optional[str] = None
    context_window: Optional[int] = None
    is_active: Optional[bool] = None


class ModelOut(BaseModel):
    id: str
    provider_id: str
    provider_name: str = ""
    name: str
    provider_model: str
    input_price: float
    output_price: float
    currency: str
    context_window: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


def _to_out(m: Model) -> ModelOut:
    return ModelOut(
        id=m.id,
        provider_id=m.provider_id,
        provider_name=m.provider.name if m.provider else "",
        name=m.name,
        provider_model=m.provider_model,
        input_price=m.input_price,
        output_price=m.output_price,
        currency=m.currency,
        context_window=m.context_window,
        is_active=m.is_active,
        created_at=m.created_at,
    )


# ---------------- 模型管理 ----------------
router = APIRouter(prefix="/api/admin/models", tags=["LLM 模型"])


@router.get("", response_model=list[ModelOut], summary="列出模型")
def list_models(
    provider_id: str | None = None,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    q = db.query(Model)
    if provider_id:
        q = q.filter(Model.provider_id == provider_id)
    rows = q.order_by(Model.name).all()
    return [_to_out(m) for m in rows]


@router.post("", response_model=ModelOut, status_code=status.HTTP_201_CREATED, summary="新增模型")
def create_model(
    payload: ModelCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    provider = db.query(Provider).filter(Provider.id == payload.provider_id).first()
    if not provider or provider.service != "llm":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LLM 供应商不存在")
    if db.query(Model).filter(Model.provider_id == provider.id, Model.name == payload.name).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该供应商下已存在同名模型")
    data = payload.model_dump()
    if not data.get("provider_model"):
        data["provider_model"] = payload.name
    m = Model(**data)
    db.add(m)
    db.commit()
    db.refresh(m)
    return _to_out(m)


@router.put("/{model_id}", response_model=ModelOut, summary="更新模型")
def update_model(
    model_id: str,
    payload: ModelUpdate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    m = db.query(Model).filter(Model.id == model_id).first()
    if not m:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="模型不存在")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(m, k, v)
    if not m.provider_model:
        m.provider_model = m.name
    db.commit()
    db.refresh(m)
    return _to_out(m)


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除模型")
def delete_model(
    model_id: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    m = db.query(Model).filter(Model.id == model_id).first()
    if not m:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="模型不存在")
    db.delete(m)
    db.commit()
    return None


# ---------------- 拉取上游模型列表 ----------------
fetch_router = APIRouter(prefix="/api/admin/providers", tags=["LLM 模型"])


@fetch_router.post("/{provider_id}/fetch-models", response_model=FetchModelsResult,
                   summary="从上游拉取模型列表")
async def fetch_models(
    provider_id: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    provider = db.query(Provider).filter(Provider.id == provider_id).first()
    if not provider or provider.service != "llm":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LLM 供应商不存在")
    url, headers = build_models_request(provider)
    if not url or url.startswith("/"):
        return FetchModelsResult(ok=False, message="未配置 Base URL", models=[])
    try:
        resp = await proxy.get_client().get(url, headers=headers, timeout=20.0)
    except Exception as e:
        return FetchModelsResult(ok=False, message=f"请求异常: {e}", models=[])

    if resp.status_code != 200:
        return FetchModelsResult(
            ok=False, message=f"HTTP {resp.status_code}: {resp.text[:200]}", models=[]
        )
    try:
        data = resp.json()
    except Exception:
        return FetchModelsResult(ok=False, message="返回内容非 JSON", models=[])

    raw = data.get("data", []) if isinstance(data, dict) else []
    models = [
        FetchedModel(
            id=str(item.get("id") or item.get("name") or ""),
            owned_by=str(item.get("owned_by") or item.get("display_name") or ""),
            context_window=int(item.get("context_window") or 0),
        )
        for item in raw
        if isinstance(item, dict)
    ]
    models = [m for m in models if m.id]
    return FetchModelsResult(ok=True, message=f"获取到 {len(models)} 个模型", models=models)
