"""统一 API Key 管理：一个 Key 可授权多个服务，配额与限流统一。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.models import ApiKey, Provider, SafetyPolicy
from app.core.registry import service_keys
from app.core.schemas import ApiKeyCreate, ApiKeyRead, ApiKeyUpdate
from app.core.security import generate_api_key, mask_key

router = APIRouter(prefix="/api/admin/keys", tags=["管理"])


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """把带时区的输入统一转为无时区 UTC，避免 SQLite/PG 比较错乱。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _to_read(k: ApiKey, plaintext: Optional[str] = None) -> ApiKeyRead:
    ch = dict(k.channels or {})
    legacy_channel = k.channel
    if ch and not legacy_channel:
        # 旧字段兼容回显：只有一个渠道时给出该值，多服务时置空（以 channels 为准）
        legacy_channel = next(iter(ch.values())) if len(ch) == 1 else None
    return ApiKeyRead(
        id=k.id,
        name=k.name,
        key_prefix=k.key_prefix or "",
        status=k.status,
        allowed_services=list(k.allowed_services or ["*"]),
        allowed_providers=dict(k.allowed_providers or {}),
        channel=legacy_channel,
        channels=ch,
        safety_policy_id=getattr(k, "safety_policy_id", None),
        quota_requests=k.quota_requests,
        quota_tokens=k.quota_tokens,
        quota_cost=k.quota_cost,
        used_requests=k.used_requests,
        used_tokens=k.used_tokens,
        used_cost=k.used_cost,
        rate_limit=k.rate_limit,
        max_concurrency=getattr(k, "max_concurrency", 0) or 0,
        expires_at=k.expires_at,
        comment=k.comment,
        created_at=k.created_at,
        updated_at=k.updated_at,
        last_used_at=k.last_used_at,
        key=plaintext,
    )


def _validate_scope(services: list[str], providers: dict[str, list[str]]) -> None:
    known = set(service_keys())
    for s in services:
        if s != "*" and s not in known:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知服务：{s}")
    for s in providers:
        if s not in known and s != "*":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"厂商白名单中的未知服务：{s}")


def _resolve_channels(
    db: Session, channels: Optional[dict[str, str]], legacy_channel: Optional[str]
) -> dict[str, str]:
    """把「按服务 channels」与旧版「单 channel」统一成 service->provider_id 映射。"""
    ch = {s: p for s, p in (channels or {}).items() if p}
    if not ch and legacy_channel:
        prov = db.query(Provider).filter(Provider.id == legacy_channel).first()
        if not prov:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="指定的渠道(channel)不存在")
        ch = {prov.service: legacy_channel}
    return ch


def _validate_safety_policy(db: Session, policy_id: Optional[str]) -> None:
    """校验 Key 绑定的审查规则：None=继承默认，""=关闭，其余必须是已存在规则。"""
    if not policy_id:
        return
    if db.query(SafetyPolicy).filter(SafetyPolicy.id == policy_id).first() is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="指定的安全审查规则不存在")


def _validate_channels(
    db: Session, channels: dict[str, str] | None, services: list[str]
) -> None:
    """校验「按服务指定渠道」：每个 service->provider_id 必须存在、启用、服务匹配且在授权范围内。"""
    if not channels:
        return
    known = set(service_keys())
    allowed = services or ["*"]
    for svc, pid in channels.items():
        if not pid:
            continue
        if svc not in known:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"渠道指定了未知服务：{svc}")
        prov = db.query(Provider).filter(Provider.id == pid).first()
        if not prov:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"服务 {svc} 指定的渠道不存在")
        if not prov.is_enabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"指定的渠道「{prov.name}」已停用")
        if prov.service != svc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"渠道「{prov.name}」属于服务 {prov.service}，不能指定给 {svc}",
            )
        if "*" not in allowed and svc not in allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"渠道「{prov.name}」属于服务 {svc}，不在该 Key 授权的服务范围内",
            )


@router.get("", response_model=list[ApiKeyRead], summary="列出全部 Key")
def list_keys(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    rows = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    return [_to_read(k) for k in rows]


@router.post("", response_model=ApiKeyRead, status_code=status.HTTP_201_CREATED,
             summary="新建 Key（明文仅返回一次）")
def create_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    _validate_scope(payload.allowed_services, payload.allowed_providers)
    channels = _resolve_channels(db, payload.channels, payload.channel)
    _validate_channels(db, channels, payload.allowed_services)
    _validate_safety_policy(db, payload.safety_policy_id)
    raw, key_hash = generate_api_key()
    k = ApiKey(
        name=payload.name,
        key_hash=key_hash,
        key_prefix=raw[:12],
        status="active",
        allowed_services=payload.allowed_services or ["*"],
        allowed_providers=payload.allowed_providers or {},
        channel=None,
        channels=channels or None,
        safety_policy_id=payload.safety_policy_id,
        quota_requests=payload.quota_requests,
        quota_tokens=payload.quota_tokens,
        quota_cost=payload.quota_cost,
        rate_limit=payload.rate_limit,
        max_concurrency=payload.max_concurrency,
        expires_at=_naive_utc(payload.expires_at),
        comment=payload.comment,
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    return _to_read(k, plaintext=raw)


@router.get("/{kid}", response_model=ApiKeyRead, summary="获取单个 Key")
def get_key(kid: str, db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == kid).first()
    if not k:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Key 不存在")
    return _to_read(k)


@router.put("/{kid}", response_model=ApiKeyRead, summary="更新 Key")
def update_key(
    kid: str,
    payload: ApiKeyUpdate,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    k = db.query(ApiKey).filter(ApiKey.id == kid).first()
    if not k:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Key 不存在")

    data = payload.model_dump(exclude_unset=True)
    has_channels = "channels" in data
    has_channel = "channel" in data
    channels_in = data.pop("channels", None)
    channel_in = data.pop("channel", None)

    if "allowed_services" in data or "allowed_providers" in data:
        _validate_scope(
            data.get("allowed_services", list(k.allowed_services or ["*"])),
            data.get("allowed_providers", dict(k.allowed_providers or {})),
        )
    if "status" in data and data["status"] not in ("active", "disabled"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="status 只能为 active / disabled")
    if "safety_policy_id" in data:
        _validate_safety_policy(db, data["safety_policy_id"])

    if "expires_at" in data:
        data["expires_at"] = _naive_utc(data["expires_at"])

    # 渠道：优先按服务 channels；只传旧版 channel 时按其 provider.service 归一化
    if has_channels or has_channel:
        target_services = data.get("allowed_services", list(k.allowed_services or ["*"]))
        if has_channels:
            ch = {s: p for s, p in (channels_in or {}).items() if p}
        elif channel_in:
            prov = db.query(Provider).filter(Provider.id == channel_in).first()
            if not prov:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="指定的渠道(channel)不存在")
            ch = {prov.service: channel_in}
        else:
            ch = {}
        _validate_channels(db, ch, target_services)
        k.channels = ch or None
        k.channel = None

    for field, value in data.items():
        setattr(k, field, value)
    db.commit()
    db.refresh(k)
    return _to_read(k)


@router.post("/{kid}/reset", response_model=ApiKeyRead, summary="重置密钥（作废旧明文）")
def reset_key(kid: str, db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == kid).first()
    if not k:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Key 不存在")
    raw, key_hash = generate_api_key()
    k.key_hash = key_hash
    k.key_prefix = raw[:12]
    k.status = "active"
    db.commit()
    db.refresh(k)
    return _to_read(k, plaintext=raw)


@router.delete("/{kid}", status_code=status.HTTP_204_NO_CONTENT, summary="删除 Key")
def delete_key(kid: str, db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == kid).first()
    if not k:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Key 不存在")
    db.delete(k)
    db.commit()
    return None
