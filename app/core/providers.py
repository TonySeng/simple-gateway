"""Provider 通用操作：凭证读写、厂商选择（含权重/优先级排序）。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.crypto import decrypt_json, encrypt_json
from app.core.models import Provider


# ---------------- 凭证 ----------------
def get_credentials(provider: Provider) -> dict:
    """解密后的凭证字典（不缓存，按需解密）。"""
    return decrypt_json(provider.credentials_enc)


def set_credentials(provider: Provider, credentials: dict | None) -> None:
    if credentials is None:
        return
    if credentials == {}:
        provider.credentials_enc = None
        return
    provider.credentials_enc = encrypt_json(credentials)


def merge_credentials(provider: Provider, patch: dict) -> None:
    """合并语义：只填其中一项不会把其它项冲掉（前端不回显密钥）。"""
    merged = get_credentials(provider)
    merged.update({k: v for k, v in patch.items() if v not in (None, "")})
    provider.credentials_enc = encrypt_json(merged)


def mask_credentials(provider: Provider, fields: Optional[list[dict]] = None) -> dict:
    """给管理接口用的掩码凭证：只回显「是否已配置」与末四位。"""
    creds = get_credentials(provider)
    out: dict = {}
    for name, value in creds.items():
        text = str(value or "")
        if not text:
            out[name] = ""
        elif len(text) <= 4:
            out[name] = "****"
        else:
            out[name] = "****" + text[-4:]
    return out


def missing_required(provider: Provider, fields: list[dict]) -> list[str]:
    """返回缺失的必填凭证字段名；兼容 SecretId / SECRET_ID / secretId 等写法。"""
    creds = get_credentials(provider)
    missing: list[str] = []
    for f in fields or []:
        if not f.get("required"):
            continue
        name = f["name"]
        norm = name.lower().replace("_", "")
        hit = any(
            v and str(k).lower().replace("_", "") == norm for k, v in (creds or {}).items()
        )
        if not hit:
            missing.append(name)
    return missing


def provider_settings(provider: Provider) -> dict:
    return dict(provider.settings or {})


def merge_settings(provider: Provider, patch: dict) -> None:
    merged = dict(provider.settings or {})
    for k, v in (patch or {}).items():
        if v in (None, ""):
            merged.pop(k, None)
        else:
            merged[k] = v
    provider.settings = merged


# ---------------- 超时 ----------------
# 连接超时上限：跨境/专线首连有时要 10s+，但不该跟读超时一样长，
# 否则一个不可达的地址会白等满整个 timeout_s。
CONNECT_TIMEOUT_CAP = 15.0


def resolve_timeout(provider: Provider | None) -> float:
    """厂商生效的读超时（秒）。未配置 / <=0 时回落到全局 ``SG_PROXY_TIMEOUT``。"""
    from app.core.config import settings

    raw = getattr(provider, "timeout_s", None) if provider is not None else None
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        value = float(settings.proxy_timeout or 120.0)
    return value


def httpx_timeout(provider: Provider | None) -> "httpx.Timeout":
    """按厂商构造 httpx 超时对象：读/写用厂商值，连接单独封顶。"""
    import httpx

    total = resolve_timeout(provider)
    return httpx.Timeout(total, connect=min(CONNECT_TIMEOUT_CAP, total))


# ---------------- 厂商选择 ----------------
def select_providers(
    db: Session,
    service: str,
    hint: Optional[str] = None,
    allowed_types: Optional[list[str]] = None,
) -> list[Provider]:
    """按服务挑选候选厂商。

    * ``hint`` 可传 type 或 name（精确匹配优先）；
    * 未指定 hint 时按「默认标记 → weight 大 → priority 小」排序，供降级使用；
    * ``allowed_types`` 为 API Key 的白名单（空或含 ``*`` 表示不限）。
    """
    q = db.query(Provider).filter(Provider.service == service, Provider.is_enabled.is_(True))
    if hint:
        rows = q.filter((Provider.type == hint) | (Provider.name == hint)).all()
        # 同类型多个配置时，保持与自动路由一致的顺序
        rows.sort(key=lambda p: (not p.is_default, -p.weight, p.priority))
    else:
        rows = q.order_by(
            Provider.is_default.desc(), Provider.weight.desc(), Provider.priority.asc()
        ).all()

    if allowed_types and "*" not in allowed_types:
        rows = [p for p in rows if p.type in allowed_types]
    return rows
