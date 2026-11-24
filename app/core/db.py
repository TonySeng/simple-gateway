"""SQLAlchemy 引擎 / 会话 / 声明基类。

默认 SQLite，可通过 SG_DATABASE_URL 切换到 PostgreSQL（psycopg v3）。
所有服务模块共用同一套 Base，因此新增中转服务无需独立建库。
"""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("gateway.db")

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等）。生产环境如需版本化迁移可引入 Alembic。"""
    from app.core import models  # noqa: F401  确保模型注册到 metadata
    from app.services import load_services  # noqa: F401  确保服务模块被加载

    load_services()
    Base.metadata.create_all(bind=engine)


def ensure_extra_columns() -> None:
    """补齐已有表中随版本新增的列（create_all 不会给已存在的表加列）。

    仅处理已知的、向后兼容的可空列；用 inspector 做幂等判断，重复执行安全。
    """
    wanted = {
        "api_keys": [
            ("channel", "VARCHAR(32)"),
            # 按服务指定渠道（JSON 映射 service -> provider_id）
            ("channels", "TEXT" if _is_sqlite else "JSON"),
            # 绑定的安全审查规则；NULL = 继承默认，'' = 关闭
            ("safety_policy_id", "VARCHAR(32)"),
        ],
        "providers": [
            # 上游超时（秒）；NULL = 使用全局默认
            ("timeout_s", "FLOAT"),
        ],
        "safety_libraries": [
            # 内置词库标记：内置项与自建项一样可编辑，仅用于来源识别与恢复出厂
            ("is_builtin", "BOOLEAN DEFAULT FALSE"),
            ("builtin_key", "VARCHAR(64) DEFAULT ''"),
            # 风险分级与命中动作：存量数据默认 medium/block，保持原「命中即拦截」行为
            ("risk_level", "VARCHAR(16) DEFAULT 'medium'"),
            ("hit_action", "VARCHAR(16) DEFAULT 'block'"),
        ],
        "safety_policies": [
            ("is_builtin", "BOOLEAN DEFAULT FALSE"),
            ("builtin_key", "VARCHAR(64) DEFAULT ''"),
            # 深度归一化（拆字识别）开关
            ("aggressive_normalize", "BOOLEAN DEFAULT FALSE"),
        ],
        "request_logs": [
            # 失败归因：分类 / 上游真实状态码 / 上游响应体 / 上游地址 / 请求标识
            ("error_type", "VARCHAR(48) DEFAULT ''"),
            ("upstream_status", "INTEGER"),
            ("upstream_body", "TEXT DEFAULT ''"),
            ("upstream_url", "VARCHAR(512) DEFAULT ''"),
            ("request_id", "VARCHAR(32) DEFAULT ''"),
            # 详情内容：请求体 / 上游返回体（超过保留期会被清空）
            ("request_body", "TEXT DEFAULT ''"),
            ("response_body", "TEXT DEFAULT ''"),
            # 安全审查结果：直接标注在请求日志上
            ("safety_result", "VARCHAR(32) DEFAULT ''"),
            ("safety_policy", "VARCHAR(128) DEFAULT ''"),
            ("safety_detail", "TEXT DEFAULT ''"),
        ],
    }
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col_name, col_type in cols:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))
                    logger.info("表 %s 已补齐列 %s", table, col_name)
        # 旧库补索引（SQLite / PostgreSQL 都支持 IF NOT EXISTS）
        for idx, ddl in (
            ("ix_request_logs_error_type", "CREATE INDEX IF NOT EXISTS ix_request_logs_error_type ON request_logs (error_type)"),
            ("ix_request_logs_request_id", "CREATE INDEX IF NOT EXISTS ix_request_logs_request_id ON request_logs (request_id)"),
            ("ix_request_logs_safety_result", "CREATE INDEX IF NOT EXISTS ix_request_logs_safety_result ON request_logs (safety_result)"),
            ("ix_api_keys_safety_policy_id", "CREATE INDEX IF NOT EXISTS ix_api_keys_safety_policy_id ON api_keys (safety_policy_id)"),
            ("ix_safety_libraries_builtin_key", "CREATE INDEX IF NOT EXISTS ix_safety_libraries_builtin_key ON safety_libraries (builtin_key)"),
            ("ix_safety_policies_builtin_key", "CREATE INDEX IF NOT EXISTS ix_safety_policies_builtin_key ON safety_policies (builtin_key)"),
        ):
            try:
                table = (
                    "api_keys" if "api_keys" in ddl
                    else "safety_libraries" if "safety_libraries" in ddl
                    else "safety_policies" if "safety_policies" in ddl
                    else "request_logs"
                )
                if insp.has_table(table):
                    conn.execute(text(ddl))
            except Exception as e:  # 索引失败不影响功能
                logger.warning("创建索引 %s 失败（忽略）：%s", idx, e)


def backfill_channels() -> None:
    """把旧版单 ``channel`` 一次性迁移到按服务的 ``channels``。

    旧数据 ``channel = <provider_id>``，按其 provider.service 落到
    ``channels[service]``；已有 ``channels`` 的 Key 跳过。幂等，可重复执行。
    """
    from app.core.models import ApiKey, Provider

    db = SessionLocal()
    try:
        rows = (
            db.query(ApiKey)
            .filter(ApiKey.channel.isnot(None), ApiKey.channels.is_(None))
            .all()
        )
        if not rows:
            return
        changed = 0
        for k in rows:
            if not k.channel:
                continue
            prov = db.query(Provider).filter(Provider.id == k.channel).first()
            if not prov:
                continue
            k.channels = {prov.service: k.channel}
            changed += 1
        if changed:
            db.commit()
            logger.info("已把 %d 个 Key 的旧 channel 迁移到 channels", changed)
    finally:
        db.close()
