"""字段级加密（Fernet = AES-128-CBC + HMAC）。

所有厂商凭证（OCR 的 secret_id/secret_key、LLM 的 api_key）统一以密文入库，
只在真正发起上游调用的瞬间解密；管理接口永不回显明文。
"""
from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if not settings.fernet_key:
            from app.core.config import resolve_secrets

            resolve_secrets()
        _fernet = Fernet(settings.fernet_key.encode())
    return _fernet


def encrypt(plain: str | None) -> str | None:
    if plain is None:
        return None
    return _get_fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt(token: str | None) -> str | None:
    if token is None:
        return None
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        # 兼容历史明文/旧密钥数据，避免整表不可用
        return token


def encrypt_json(obj: Any) -> str | None:
    if obj is None:
        return None
    return encrypt(json.dumps(obj, ensure_ascii=False))


def decrypt_json(token: str | None) -> dict:
    if not token:
        return {}
    raw = decrypt(token)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
