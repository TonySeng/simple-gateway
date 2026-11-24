"""厂商接入配置管理 —— 一套接口适配所有服务。

服务之间只差「表单元信息」，因此新增一个中转服务时，本文件无需改动。
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.models import Model, Provider
from app.core.providers import (
    get_credentials,
    mask_credentials,
    merge_credentials,
    missing_required,
    resolve_timeout,
)
from app.core.registry import ServiceSpec, get_service
from app.core.schemas import (
    ProviderCreate,
    ProviderRead,
    ProviderTestResult,
    ProviderUpdate,
)

router = APIRouter(prefix="/api/admin/providers", tags=["管理"])


# ---------------- 内部工具 ----------------
def _spec(service_key: str) -> ServiceSpec:
    spec = get_service(service_key)
    if spec is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知服务：{service_key}")
    return spec


def _to_read(p: Provider, db: Session) -> ProviderRead:
    spec = get_service(p.service)
    type_spec = spec.provider_type(p.type) if spec else None
    missing = missing_required(p, type_spec.fields) if type_spec else []
    models_count = db.query(Model).filter(Model.provider_id == p.id).count()
    return ProviderRead(
        id=p.id,
        service=p.service,
        service_label=spec.label if spec else p.service,
        type=p.type,
        type_label=type_spec.label if type_spec else p.type,
        name=p.name,
        base_url=p.base_url or "",
        is_enabled=p.is_enabled,
        is_default=p.is_default,
        weight=p.weight,
        priority=p.priority,
        timeout_s=p.timeout_s,
        effective_timeout_s=resolve_timeout(p),
        has_credentials=bool(p.credentials_enc),
        masked_credentials=mask_credentials(p),
        missing_credentials=missing,
        settings=p.settings or {},
        comment=p.comment,
        models_count=models_count,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def _validate_type(spec: ServiceSpec, type_name: str) -> None:
    if not spec.provider_type(type_name):
        allowed = "、".join(t.type for t in spec.provider_types)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"「{spec.label}」不支持厂商类型 {type_name}，可选：{allowed}",
        )


def _invalidate(service_key: str) -> None:
    spec = get_service(service_key)
    if spec and spec.invalidate:
        spec.invalidate()


def _check_required(spec: ServiceSpec, type_name: str, credentials: dict) -> None:
    type_spec = spec.provider_type(type_name)
    if not type_spec:
        return
    missing = []
    for f in type_spec.fields:
        if not f.get("required"):
            continue
        name = f["name"]
        norm = name.lower().replace("_", "")
        if not any(v and str(k).lower().replace("_", "") == norm for k, v in (credentials or {}).items()):
            missing.append(name)
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{type_spec.label} 缺少必填凭证：{'、'.join(missing)}",
        )


# ---------------- CRUD ----------------
@router.get("", response_model=list[ProviderRead], summary="列出厂商配置")
def list_providers(
    service: Optional[str] = None,
    enabled: Optional[bool] = None,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    q = db.query(Provider)
    if service:
        q = q.filter(Provider.service == service)
    if enabled is not None:
        q = q.filter(Provider.is_enabled.is_(enabled))
    rows = q.order_by(
        Provider.service, Provider.is_default.desc(), Provider.weight.desc(), Provider.priority.asc()
    ).all()
    return [_to_read(p, db) for p in rows]


@router.post("", response_model=ProviderRead, status_code=status.HTTP_201_CREATED,
             summary="新建厂商配置")
def create_provider(
    payload: ProviderCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    spec = _spec(payload.service)
    _validate_type(spec, payload.type)
    _check_required(spec, payload.type, payload.credentials)

    if db.query(Provider).filter(
        Provider.service == payload.service, Provider.name == payload.name
    ).first():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="该服务下已存在同名配置")

    if payload.is_default:
        db.query(Provider).filter(
            Provider.service == payload.service, Provider.is_default.is_(True)
        ).update({Provider.is_default: False})

    p = Provider(
        service=payload.service,
        type=payload.type,
        name=payload.name,
        base_url=(payload.base_url or "").strip(),
        is_enabled=payload.is_enabled,
        is_default=payload.is_default,
        weight=payload.weight,
        priority=payload.priority,
        timeout_s=(payload.timeout_s or None),
        settings=payload.settings or {},
        comment=payload.comment,
    )
    if payload.credentials:
        merge_credentials(p, payload.credentials)
    db.add(p)
    db.commit()
    db.refresh(p)
    _invalidate(payload.service)
    return _to_read(p, db)


def _copy_name(db: Session, service: str, base: str) -> str:
    """副本命名：原名 + （副本）；重名时递增为（副本2）（副本3）…。

    providers.name 上限 128 字符，超长时先截断原名再拼后缀。
    """
    def make(suffix: str) -> str:
        return base[: 128 - len(suffix)] + suffix

    name = make("（副本）")
    n = 2
    while db.query(Provider).filter(
        Provider.service == service, Provider.name == name
    ).first():
        name = make(f"（副本{n}）")
        n += 1
    return name


@router.post("/{pid}/duplicate", response_model=ProviderRead,
             status_code=status.HTTP_201_CREATED, summary="复制配置（含加密凭证）")
def duplicate_provider(
    pid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    """深拷贝一条厂商配置，供「复制后编辑」使用。

    * 凭证密文整段复制（同一把 Fernet 密钥可解密），无需重新填写密钥；
    * settings 深拷贝，改副本不会串改原配置；
    * 副本强制停用且非默认，避免复制完立刻参与生产路由 / 抢占默认位；
    * LLM 的 models 不复制（模型需按新配置重新拉取）。
    """
    src = db.query(Provider).filter(Provider.id == pid).first()
    if not src:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")

    p = Provider(
        service=src.service,
        type=src.type,
        name=_copy_name(db, src.service, src.name),
        base_url=src.base_url or "",
        is_enabled=False,
        is_default=False,
        weight=src.weight,
        priority=src.priority,
        timeout_s=src.timeout_s,
        settings=json.loads(json.dumps(src.settings or {})),
        credentials_enc=src.credentials_enc,
        comment=src.comment,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    _invalidate(src.service)
    return _to_read(p, db)


@router.get("/{pid}", response_model=ProviderRead, summary="获取单个配置")
def get_provider(
    pid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(Provider).filter(Provider.id == pid).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")
    return _to_read(p, db)


@router.put("/{pid}", response_model=ProviderRead, summary="更新配置")
def update_provider(
    pid: str,
    payload: ProviderUpdate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(Provider).filter(Provider.id == pid).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")
    spec = _spec(p.service)

    if payload.type is not None and payload.type != p.type:
        _validate_type(spec, payload.type)
        p.type = payload.type
    if payload.name is not None and payload.name != p.name:
        if db.query(Provider).filter(
            Provider.service == p.service, Provider.name == payload.name, Provider.id != p.id
        ).first():
            raise HTTPException(status.HTTP_409_CONFLICT, detail="该服务下已存在同名配置")
        p.name = payload.name
    if payload.base_url is not None:
        p.base_url = payload.base_url.strip()
    if payload.is_enabled is not None:
        p.is_enabled = payload.is_enabled
    if payload.is_default is not None:
        if payload.is_default:
            db.query(Provider).filter(
                Provider.service == p.service,
                Provider.is_default.is_(True),
                Provider.id != p.id,
            ).update({Provider.is_default: False})
        p.is_default = payload.is_default
    if payload.weight is not None:
        p.weight = payload.weight
    if payload.priority is not None:
        p.priority = payload.priority
    # 0 = 显式恢复全局默认；null/缺省 = 不修改
    if payload.timeout_s is not None:
        p.timeout_s = payload.timeout_s or None
    # 凭证：null=不改；{}=清空；非空=合并（表单不回显密钥，避免只填一项冲掉其它项）
    if payload.credentials is not None:
        if payload.credentials:
            merge_credentials(p, payload.credentials)
        else:
            p.credentials_enc = None
    if payload.settings is not None:
        p.settings = payload.settings
    if payload.comment is not None:
        p.comment = payload.comment

    db.commit()
    db.refresh(p)
    _invalidate(p.service)
    return _to_read(p, db)


@router.delete("/{pid}", status_code=status.HTTP_204_NO_CONTENT, summary="删除配置")
def delete_provider(
    pid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(Provider).filter(Provider.id == pid).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")
    service_key = p.service
    db.delete(p)
    db.commit()
    _invalidate(service_key)
    return None


# ---------------- 配置即测 ----------------
@router.post("/{pid}/test", response_model=ProviderTestResult, summary="用真实凭证调用一次上游")
async def test_provider(
    pid: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(Provider).filter(Provider.id == pid).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")
    spec = _spec(p.service)
    if not spec.test_provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="该服务未提供连通性测试")

    # 手动解析表单，容忍「空 body / 空 multipart / 畸形 multipart」：
    # 浏览器对 fetch(new FormData())（无任何字段）会发出只有结尾 boundary 的
    # multipart body，FastAPI 用 File/Form 自动解析会抛
    # "There was an error parsing the body"（400）。这里捕获后按“无 file/params”处理，
    # 使纯连通性测试（LLM 的 /models）无需上传任何字段也能通过。
    file_bytes = None
    mime = None
    params: str | None = None
    try:
        form = await request.form()
    except Exception:
        form = {}
    try:
        upload = form.get("file") if hasattr(form, "get") else None
        if upload is not None and getattr(upload, "filename", None):
            file_bytes = await upload.read()
            mime = getattr(upload, "content_type", None)
        raw_params = form.get("params") if hasattr(form, "get") else None
        params = raw_params if isinstance(raw_params, str) else None
    except Exception:
        file_bytes = None
        mime = None
    try:
        extra = json.loads(params or "{}")
    except Exception:
        extra = {}

    result = await spec.test_provider(db, p, file_bytes, mime, extra)
    return ProviderTestResult(
        ok=bool(result.get("ok")),
        service=p.service,
        provider=p.name,
        type=p.type,
        message=str(result.get("message") or ""),
        latency_ms=int(result.get("latency_ms") or 0),
        detail=result.get("detail") or {},
    )


@router.get("/{pid}/credentials", summary="查看凭证（掩码）")
def show_credentials(
    pid: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    p = db.query(Provider).filter(Provider.id == pid).first()
    if not p:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="配置不存在")
    return {"masked": mask_credentials(p), "fields": sorted(get_credentials(p).keys())}
