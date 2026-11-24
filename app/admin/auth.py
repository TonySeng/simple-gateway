"""管理员登录、改密与基础信息。"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_admin_any, get_current_admin_claims
from app.core.registry import service_meta
from app.core.schemas import (
    AdminMe,
    ChangePasswordRequest,
    LoginRequest,
    TokenResponse,
)
from app.core.security import (
    create_access_token,
    is_strong_password,
    verify_password,
)

router = APIRouter(prefix="/api/admin", tags=["管理"])


@router.get("/config", summary="前端启动配置（无需登录）")
def config():
    from app.core.security import password_policy

    return {
        "app_name": settings.app_name,
        "version": settings.version,
        "auth_enabled": settings.admin_auth_enabled,
        "admin_username": settings.admin_username,
        "services": service_meta(),
        "password_policy": password_policy(),
    }


@router.post("/login", response_model=TokenResponse, summary="管理员登录")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    if not settings.admin_auth_enabled:
        return TokenResponse(
            access_token=create_access_token(settings.admin_username),
            must_change_password=False,
        )

    from app.core.admin_store import get_admin_hash, get_must_change

    stored = get_admin_hash(db)
    user_ok = hmac.compare_digest(payload.username or "", settings.admin_username)
    pwd_ok = bool(stored) and verify_password(payload.password, stored)
    if not (user_ok and pwd_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    must = get_must_change(db)
    token = create_access_token(payload.username, extra={"must_change": must})
    return TokenResponse(access_token=token, must_change_password=must)


@router.get("/me", response_model=AdminMe, summary="当前管理员")
def me(claims: dict = Depends(get_current_admin_claims)):
    return AdminMe(
        username=claims["sub"],
        auth_enabled=settings.admin_auth_enabled,
        app_name=settings.app_name,
        version=settings.version,
        must_change_password=bool(claims.get("must_change")),
    )


@router.post(
    "/change-password",
    response_model=TokenResponse,
    summary="修改管理员密码（强制改密时新密码须为强密码）",
)
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    username: str = Depends(get_current_admin_any),
):
    from app.core.admin_store import (
        get_admin_hash,
        set_admin_password,
        set_must_change,
    )

    stored = get_admin_hash(db)
    if not stored or not verify_password(payload.current_password, stored):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="当前密码错误")

    ok, reason = is_strong_password(payload.new_password, username)
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="新密码不符合强度要求：" + reason,
        )
    if verify_password(payload.new_password, stored):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同")

    set_admin_password(db, payload.new_password)
    set_must_change(db, False)
    token = create_access_token(username, extra={"must_change": False})
    return TokenResponse(access_token=token, must_change_password=False)
