"""管理员口令的持久化存储（落库到 ``meta`` 表，可运行时修改）。

设计
----
* 管理员口令哈希与「是否强制改密」标记都存于 ``meta`` 表，而非仅来自环境变量，
  这样才能在登录后运行时修改口令，并对弱/默认口令强制改密。
* 仅在库内尚无哈希时，用 ``SG_ADMIN_PASSWORD``（默认 admin123）播种一次；
  之后库内值才是权威来源，避免环境变量改动悄悄覆盖运行时改过的口令。
* 强制改密触发条件：
  - 种子口令为默认 admin123；
  - 种子口令不满足强密码规则（弱口令）；
  - 显式设置 ``SG_ADMIN_FORCE_PASSWORD_CHANGE=true``（如怀疑泄露）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.config import DEFAULT_ADMIN_PASSWORD, settings
from app.core.models import Meta
from app.core.security import hash_password, is_strong_password

logger = logging.getLogger("gateway.admin_store")

META_PWD_HASH = "admin_pwd_hash"
META_PWD_MUST_CHANGE = "admin_pwd_must_change"


def _get(db: Session, key: str) -> str | None:
    row = db.query(Meta).filter(Meta.key == key).first()
    return row.value if row else None


def _set(db: Session, key: str, value: str) -> None:
    row = db.query(Meta).filter(Meta.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Meta(key=key, value=value))
    db.commit()


def get_admin_hash(db: Session) -> str | None:
    return _get(db, META_PWD_HASH)


def get_must_change(db: Session) -> bool:
    return _get(db, META_PWD_MUST_CHANGE) == "true"


def set_must_change(db: Session, val: bool) -> None:
    _set(db, META_PWD_MUST_CHANGE, "true" if val else "false")


def set_admin_password(db: Session, plain: str) -> None:
    _set(db, META_PWD_HASH, hash_password(plain))


def ensure_admin_seeded(db: Session) -> None:
    """首次启动播种管理员口令；返回是否处于强制改密状态。"""
    existing = get_admin_hash(db)
    if existing:
        # 库内已有口令（运行时改过），以库为准；仅在显式强制时置位
        if settings.admin_force_password_change:
            set_must_change(db, True)
        return

    plain = settings.admin_password  # 来自 SG_ADMIN_PASSWORD，默认 admin123
    set_admin_password(db, plain)

    weak = (plain == DEFAULT_ADMIN_PASSWORD) or (not is_strong_password(plain)[0])
    must_change = bool(settings.admin_force_password_change) or weak
    set_must_change(db, must_change)

    if must_change:
        logger.warning(
            "⚠️ 管理员口令需要强制修改（默认/弱口令或显式强制），首次登录后将被要求设置强密码"
        )
