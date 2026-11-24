"""FastAPI 依赖：管理员鉴权、统一 API Key 鉴权（服务范围 / 配额 / 限流）。

统一鉴权是「多服务共用一个 Key」的关键：同一个 Key 可以同时被授权调用 OCR 与 LLM，
也可以只授权其中一个，并分别限制各自可用的厂商。
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.models import ApiKey
from app.core.security import decode_access_token, hash_api_key

_bearer = HTTPBearer(auto_error=False)

# 进程内限流桶。多 worker / 多实例部署请换 Redis（接口保持不变即可）。
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------- 管理员 ----------------
def get_current_admin_claims(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """解码并返回管理员 JWT 声明（不拦截 must_change）。"""
    if not settings.admin_auth_enabled:
        return {"sub": settings.admin_username, "must_change": False}
    if not creds or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="缺少管理员令牌")
    payload = decode_access_token(creds.credentials)
    if not payload or payload.get("sub") != settings.admin_username:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    return payload


def get_current_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """普通管理接口鉴权：处于「强制改密」状态时一律拒绝（403）。"""
    claims = get_current_admin_claims(creds)
    if claims.get("must_change"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="请先修改管理员密码后再继续操作",
        )
    return claims["sub"]


def get_current_admin_any(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """改密接口专用：已登录即可（允许 must_change 状态调用）。"""
    return get_current_admin_claims(creds)["sub"]


# ---------------- API Key ----------------
def _rate_limited(key_id: str, limit_per_min: int) -> bool:
    if limit_per_min <= 0:
        return False
    now = time.monotonic()
    bucket = _rate_buckets[key_id]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= limit_per_min:
        return True
    bucket.append(now)
    return False


def _assert_service_allowed(key: ApiKey, service: str) -> None:
    allowed = key.allowed_services or ["*"]
    if "*" not in allowed and service not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"该 API Key 未授权调用「{service}」服务",
        )


def allowed_provider_types(key: ApiKey, service: str) -> list[str]:
    """该 Key 在指定服务下允许的厂商类型；空列表语义是「不限制」。"""
    mapping = key.allowed_providers or {}
    value = mapping.get(service)
    if not value:
        return []
    return list(value)


def _assert_quota(key: ApiKey) -> None:
    if key.quota_requests > 0 and key.used_requests >= key.quota_requests:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="API Key 的调用次数配额已用尽")
    if key.quota_tokens > 0 and key.used_tokens >= key.quota_tokens:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="API Key 的 tokens 配额已用尽")
    if key.quota_cost > 0 and key.used_cost >= key.quota_cost:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="API Key 的费用配额已用尽")


def authenticate_api_key(
    service: str,
    creds: HTTPAuthorizationCredentials | None,
    db: Session,
) -> ApiKey:
    if not creds or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="缺少 API Key")
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_api_key(creds.credentials)).first()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="API Key 无效")
    if key.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"API Key 状态异常：{key.status}")
    if key.expires_at and key.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="API Key 已过期")
    _assert_service_allowed(key, service)
    _assert_quota(key)
    if _rate_limited(key.id, key.rate_limit):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"超出速率限制（{key.rate_limit} 次/分钟），请稍后重试",
        )
    return key


def api_key_auth(service: str):
    """依赖工厂：为某个服务生成 API Key 鉴权依赖。"""

    def _dependency(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
        db: Session = Depends(get_db),
    ) -> ApiKey:
        return authenticate_api_key(service, creds, db)

    return _dependency
