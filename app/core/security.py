"""安全工具：API Key 生成/哈希、管理员口令校验、JWT。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings


# ---------------- API Key ----------------
def generate_api_key() -> tuple[str, str]:
    """返回 (明文 key, sha256(key))；明文仅创建时展示一次。"""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(max(settings.api_key_length, 16)))
    raw = f"{settings.api_key_prefix}{body}"
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mask_key(raw: str) -> str:
    if not raw:
        return ""
    return raw[:8] + "…" + raw[-4:] if len(raw) > 14 else "****"


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------- 管理员口令 ----------------
def verify_admin(username: str, password: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    return hmac.compare_digest(username or "", settings.admin_username) and hmac.compare_digest(
        password or "", settings.admin_password
    )


# ---------------- 口令哈希（PBKDF2-HMAC-SHA256，无第三方依赖）----------------
_PBKDF2_ROUNDS = 200_000
_SPECIAL_CHARS = r"""!@#$%^&*()_+\-=[]{}|;:,.<>?/`~"'"""
_COMMON_WEAK = {
    "admin123", "password", "12345678", "1234567890", "qwerty123",
    "letmein", "admin", "root", "password123", "111111111111",
    "administrator", "changeme", "passw0rd", "abc123456789",
}


def hash_password(password: str) -> str:
    """返回 'pbkdf2_sha256$rounds$salt_b64$hash_b64'。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or stored.count("$") != 3:
        return False
    try:
        algo, rounds_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds_s))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def is_strong_password(password: str, username: str = "") -> tuple[bool, str]:
    """返回 (是否达标, 不达标原因)。"""
    if not password:
        return False, "密码不能为空"
    if len(password) < 12 or len(password) > 128:
        return False, "长度需为 12-128 位"
    if not re.search(r"[a-z]", password):
        return False, "需包含小写字母"
    if not re.search(r"[A-Z]", password):
        return False, "需包含大写字母"
    if not re.search(r"\d", password):
        return False, "需包含数字"
    if not re.search(r"[" + re.escape(_SPECIAL_CHARS) + r"]", password):
        return False, "需包含特殊字符（如 !@#$%^&* 等）"
    if username and password.lower() == username.lower():
        return False, "密码不能与用户名相同"
    if password.lower() in _COMMON_WEAK:
        return False, "密码过于常见，请更换"
    if len(set(password)) < 4:
        return False, "密码字符种类过少"
    return True, "强密码"


def password_policy() -> dict:
    return {
        "min_length": 12,
        "max_length": 128,
        "require_upper": True,
        "require_lower": True,
        "require_digit": True,
        "require_special": True,
    }


# ---------------- JWT ----------------
def create_access_token(subject: str, expires_minutes: int | None = None, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes or settings.jwt_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
