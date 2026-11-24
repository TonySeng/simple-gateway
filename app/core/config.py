"""统一网关运行时配置（环境变量 / .env，前缀 SG_）。

设计要点
--------
* 所有配置项都在这里集中声明，新增「中转服务」不需要额外环境变量。
* ``SG_JWT_SECRET`` / ``SG_FERNET_KEY`` 留空时自动生成并持久化到数据目录的
  ``.secrets.json``（权限 0600），保证容器重建后已加密的厂商凭证仍可解密、
  已签发的登录令牌不失效。
* 管理员登录可用 ``SG_ADMIN_AUTH_ENABLED`` 关闭（仅限受信任内网）。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("gateway.config")

DEFAULT_JWT_SECRET = "change-me-in-production-please-use-a-long-random-string"
DEFAULT_ADMIN_PASSWORD = "admin123"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SG_", env_file=".env", extra="ignore")

    # ---------- 基础 ----------
    app_name: str = "Simple Gateway"
    version: str = "1.0.0"
    debug: bool = False

    # ---------- 数据库 ----------
    # 默认 SQLite（零依赖）；生产可换 postgresql+psycopg://user:pass@host/db
    database_url: str = "sqlite:///./data/gateway.db"

    # ---------- 管理后台 ----------
    admin_auth_enabled: bool = True
    admin_username: str = "admin"
    admin_password: str = DEFAULT_ADMIN_PASSWORD
    # 强制下次登录修改密码（如怀疑口令泄露）；与弱/默认口令自动强制改密叠加
    admin_force_password_change: bool = False

    # ---------- 密钥 ----------
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    fernet_key: str = ""
    secrets_file: str = ""

    # ---------- API Key ----------
    api_key_prefix: str = "sg_sk_"
    api_key_length: int = 40
    default_rate_limit_per_min: int = 0  # 新建 Key 的默认速率上限，0=不限

    # ---------- 转发 ----------
    proxy_timeout: float = 120.0
    default_currency: str = "CNY"

    # ---------- 日志详情 ----------
    # 详情（请求体/上游返回体）保留天数；后台可改，改完即时生效
    log_detail_days: int = 7
    # 单条详情的最长字符数，超出部分截断（防止一条大响应撑爆库）
    log_body_limit: int = 32768

    # ---------- 服务 ----------
    host: str = "0.0.0.0"
    port: int = 8000
    log_requests: bool = True
    # 允许的前端来源，逗号分隔；* 表示全部
    cors_origins: str = "*"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def _sqlite_path(url: str) -> str:
    if not url.startswith("sqlite"):
        return ""
    return url.replace("sqlite:///", "").replace("sqlite://", "")


def _default_secrets_file() -> str:
    """密钥文件始终落在数据目录（与数据库类型无关），随数据卷一起持久化。

    早期实现按 SQLite 数据库文件路径推断密钥位置；切到 PostgreSQL 后
    ``_sqlite_path`` 返回空串，密钥路径退化成进程工作目录，导致启动
    时找不到既有密钥而自生成新密钥，无法解密既有加密凭证。现在固定到
    数据目录（可由 SG_DATA_DIR 覆盖），与 SQLite 时期的布局完全一致。
    """
    data_dir = os.environ.get("SG_DATA_DIR") or os.path.abspath("data")
    return os.path.join(data_dir, ".secrets.json")


def resolve_secrets() -> None:
    """确保 JWT / Fernet 密钥存在，缺失则生成并持久化（幂等）。

    安全约束：密钥文件「存在但读不了/损坏」时**必须中止启动**。
    历史上这里曾静默吞掉 PermissionError 并生成临时密钥，导致应用以非 root
    运行、而 restore 脚本以 root 写入 .secrets.json 的场景下，进程用临时密钥
    跑了一整天，期间录入的所有厂商凭证都用临时密钥加密，容器一重建就全部
    解不开（不可逆）。宁可启动失败，也不能静默污染数据。
    """
    sf = settings.secrets_file or _default_secrets_file()
    data: dict = {}
    if os.path.exists(sf):
        try:
            with open(sf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except PermissionError as e:
            raise RuntimeError(
                f"密钥文件存在但当前用户无法读取：{sf}（{e}）。"
                "容器以非 root（appuser 999）运行时，文件属主必须是 999:999；"
                "常见成因：恢复脚本以 root 写入了 .secrets.json。"
                "修复：docker run --rm -v <卷>:/data alpine chown 999:999 /data/.secrets.json 。"
                "为避免用临时密钥覆盖既有密文（会导致全部厂商凭证永久失效），进程拒绝启动。"
            ) from e
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"密钥文件格式损坏：{sf}（{e}）。请先备份该文件并从可用备份恢复后再启动。"
            ) from e
        except OSError as e:
            raise RuntimeError(f"密钥文件读取失败：{sf}（{e}）") from e
        if not isinstance(data, dict):
            raise RuntimeError(f"密钥文件内容不是 JSON 对象：{sf}")

    changed = False

    if not settings.jwt_secret:
        if data.get("jwt_secret"):
            settings.jwt_secret = data["jwt_secret"]
        else:
            settings.jwt_secret = secrets.token_urlsafe(48)
            data["jwt_secret"] = settings.jwt_secret
            changed = True

    if not settings.fernet_key:
        if data.get("fernet_key"):
            settings.fernet_key = data["fernet_key"]
        else:
            settings.fernet_key = Fernet.generate_key().decode()
            data["fernet_key"] = settings.fernet_key
            changed = True

    if changed:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(sf)) or ".", exist_ok=True)
            with open(sf, "w", encoding="utf-8") as f:
                json.dump(data, f)
            try:
                os.chmod(sf, 0o600)
            except Exception:
                pass
            logger.warning("已生成并持久化运行时密钥到 %s（请妥善备份）", sf)
        except Exception as e:
            # 密钥无法持久化 = 每次重启都换密钥 = 已入库的密文全部作废。
            # 这种情况下继续启动只会造成"看起来正常、实际在用临时密钥"的假象。
            raise RuntimeError(
                f"密钥文件写入失败（{sf}）：{e}。"
                "本轮进程只能用临时密钥，重启后已加密的厂商凭证将无法解密。"
                "请检查数据卷挂载与目录权限（属主应为运行用户）后重启。"
            ) from e

    try:
        Fernet(settings.fernet_key.encode())
    except Exception:
        raise RuntimeError(
            "SG_FERNET_KEY 不是合法的 Fernet 密钥（需 32 字节 base64urlsafe 字符串）。"
            "生成方式：python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    # ---------- 安全提示 ----------
    if settings.admin_auth_enabled and settings.jwt_secret == DEFAULT_JWT_SECRET:
        logger.warning("⚠️ 管理员登录已启用，但 JWT 密钥为默认值，请通过 SG_JWT_SECRET 设置强随机值")
    if settings.admin_auth_enabled and settings.admin_password == DEFAULT_ADMIN_PASSWORD:
        # 注意：运行时口令以数据库 meta 表中的哈希为准；此处的环境变量仅在
        # 「空库首次初始化」时用于播种一次，因此这里只提示、不告警（避免误报）。
        logger.info(
            "SG_ADMIN_PASSWORD 仍为默认 admin123 —— 仅在空库首次初始化时生效；"
            "已有数据时以数据库中的口令为准"
        )
    if not settings.admin_auth_enabled:
        logger.warning(
            "⚠️ 管理员登录已关闭（SG_ADMIN_AUTH_ENABLED=false），管理接口对外开放，仅限受信任内网"
        )


def ensure_data_dir() -> None:
    """SQLite 模式下确保数据库目录存在。"""
    path = _sqlite_path(settings.database_url)
    if path:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
