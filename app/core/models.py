"""统一 ORM 模型。

四张表支撑所有中转服务：

* ``providers``     —— 所有服务共用一张接入配置表（按 ``service`` 区分），
                       凭证加密存放、settings 明文存放，新增服务不需要改表结构。
* ``models``        —— LLM 专用：模型与单价（其他服务可忽略）。
* ``api_keys``      —— 统一对外密钥：可授权的服务、服务级厂商白名单、统一配额。
* ``request_logs``  —— 统一调用日志：带 ``service`` 维度，一套仪表盘看全部服务。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.security import new_id


def utcnow() -> datetime:
    """统一使用「无时区的 UTC」入库，保证 SQLite / PostgreSQL 行为一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Provider(Base):
    """一个上游接入配置（OCR 厂商 / LLM 供应商 / 未来任意服务）。"""

    __tablename__ = "providers"
    __table_args__ = (
        UniqueConstraint("service", "name", name="uq_provider_service_name"),
        Index("ix_providers_service_enabled", "service", "is_enabled"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    service: Mapped[str] = mapped_column(String(32), index=True)  # ocr / llm / ...
    type: Mapped[str] = mapped_column(String(32), index=True)     # tencent / openai / ...
    name: Mapped[str] = mapped_column(String(128))

    # LLM 用；OCR 也可用它记录接入点（同时兼容 settings.url）
    base_url: Mapped[str] = mapped_column(String(512), default="")

    # 加密后的凭证 JSON（如 {"secret_id": "...", "secret_key": "..."} / {"api_key": "..."}）
    credentials_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 明文配置 JSON（url / engine / action / headers / path ...）
    settings: Mapped[dict] = mapped_column(JSON, default=dict)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 路由：weight 越大越优先，priority 越小越优先
    weight: Mapped[int] = mapped_column(Integer, default=100)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    # 上游超时（秒）：None/0 = 用全局默认 SG_PROXY_TIMEOUT。
    # 慢模型（长推理/长文补全）调大；弱网通道可调小以更快降级。
    timeout_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    models: Mapped[list["Model"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", lazy="selectin"
    )


class Model(Base):
    """LLM 对外暴露的模型名 + 上游真实模型 id + 单价。"""

    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("provider_id", "name", name="uq_model_provider_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("providers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160), index=True)          # 客户端看到的模型名
    provider_model: Mapped[str] = mapped_column(String(160), default="")  # 上游真实模型 id
    input_price: Mapped[float] = mapped_column(Float, default=0.0)   # 每 1K tokens
    output_price: Mapped[float] = mapped_column(Float, default=0.0)  # 每 1K tokens
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    context_window: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    provider: Mapped["Provider"] = relationship(back_populates="models")


class ApiKey(Base):
    """统一对外密钥。

    * ``allowed_services``：可调用的服务列表，``["*"]`` 表示全部。
    * ``allowed_providers``：按服务限定可用厂商，如 ``{"ocr": ["baidu"], "llm": ["*"]}``；
      缺失该服务键或含 ``["*"]`` 表示该服务下全部厂商可用。
    * 配额：``quota_requests`` / ``quota_tokens`` / ``quota_cost``，0 表示不限；
      用量分别累计在 ``used_requests`` / ``used_tokens`` / ``used_cost``。
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(24), default="")  # 便于列表识别
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/disabled

    allowed_services: Mapped[list] = mapped_column(JSON, default=lambda: ["*"])
    allowed_providers: Mapped[dict] = mapped_column(JSON, default=dict)
    # 指定渠道（旧版单渠道）：绑定到某个具体的上游厂商(provider_id)。
    # 已被下面的 channels 取代，仅保留用于向后兼容与一次性迁移。
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 按服务指定渠道：service_key -> provider_id，如 {"llm": "...", "ocr": "..."}。
    # 每个服务可单独钉定到不同的上游厂商；未包含的服务按模型/厂商白名单自动路由。
    channels: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 安全审查规则绑定：
    #   None = 继承全局默认规则；"" = 该 Key 关闭审查；具体 id = 使用该规则
    safety_policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    quota_requests: Mapped[int] = mapped_column(Integer, default=0)
    quota_tokens: Mapped[int] = mapped_column(Integer, default=0)
    quota_cost: Mapped[float] = mapped_column(Float, default=0.0)

    used_requests: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_cost: Mapped[float] = mapped_column(Float, default=0.0)

    rate_limit: Mapped[int] = mapped_column(Integer, default=0)  # 次/分钟，0=不限
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RequestLog(Base):
    """统一调用日志（所有服务共用，按 service 区分）。"""

    __tablename__ = "request_logs"
    __table_args__ = (
        Index("ix_logs_service_created", "service", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    service: Mapped[str] = mapped_column(String(32), index=True, default="")
    endpoint: Mapped[str] = mapped_column(String(96), default="")

    api_key_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    key_name: Mapped[str] = mapped_column(String(128), default="")

    provider_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    provider_name: Mapped[str] = mapped_column(String(128), default="")
    provider_type: Mapped[str] = mapped_column(String(32), default="")

    model: Mapped[str] = mapped_column(String(160), default="", index=True)
    mode: Mapped[str] = mapped_column(String(32), default="")

    status: Mapped[str] = mapped_column(String(16), default="success", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")

    # ---- 失败归因（成功请求均为空；用于聚合统计与逐条排障）----
    # error_type 见 app/services/llm/proxy.py 的 ERROR_* 常量
    error_type: Mapped[str] = mapped_column(String(48), default="", index=True)
    # 上游真实 HTTP 状态码（与 http_status 不同：后者是网关回给客户端的）
    upstream_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 上游响应体原文（已脱敏，截断 2000 字）
    upstream_body: Mapped[str] = mapped_column(Text, default="")
    # 实际请求的上游地址，便于确认打到了哪个渠道
    upstream_url: Mapped[str] = mapped_column(String(512), default="")
    # 请求标识，同时通过 X-Request-Id 响应头回给客户端
    request_id: Mapped[str] = mapped_column(String(32), default="", index=True)

    # ---- 详情内容（已脱敏，超过保留期会被清空以控制体积）----
    # 客户端发来的原始请求体
    request_body: Mapped[str] = mapped_column(Text, default="")
    # 上游返回给网关的内容（成功时为响应体，失败时为错误体）
    response_body: Mapped[str] = mapped_column(Text, default="")

    # ---- 安全审查结果（直接标注在请求日志上）----
    # "" = 未配置审查；passed = 已审查通过；blocked_input / blocked_output /
    # blocked_overflow = 命中规则并被替换；error = 审查过程异常
    safety_result: Mapped[str] = mapped_column(String(32), default="", index=True)
    safety_policy: Mapped[str] = mapped_column(String(128), default="")
    safety_detail: Mapped[str] = mapped_column(Text, default="")

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), index=True
    )


class SafetyLibrary(Base):
    """敏感词库。

    ``terms`` 按行存储，一行一个词；以 ``re:`` 开头的行按正则处理。
    多个词库可被同一条审查规则引用，便于按业务/租户拆分维护。
    """

    __tablename__ = "safety_libraries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[str] = mapped_column(Text, default="")
    term_count: Mapped[int] = mapped_column(Integer, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 系统内置项标记：内置项同样可以在后台自由修改/停用/删除
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class SafetyPolicy(Base):
    """审查规则：引用若干词库，并定义方向、匹配方式与命中阈值。"""

    __tablename__ = "safety_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 全局默认规则：未显式绑定规则的 Key 使用它；同一时间只保留一条
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # 引用的词库 id 列表
    library_ids: Mapped[list] = mapped_column(JSON, default=list)
    input_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    output_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # contains = 子串匹配；word = 拉丁词按词边界匹配（中文仍按子串）
    match_mode: Mapped[str] = mapped_column(String(16), default="contains")
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    normalize: Mapped[bool] = mapped_column(Boolean, default=True)
    # 命中多少个不同词条才拦截（1 = 任意命中；用于「至少命中 N 个」规则）
    threshold: Mapped[int] = mapped_column(Integer, default=1)
    # 单次审查的最大字符数，超出部分截断，避免超长上下文拖慢网关
    max_text_chars: Mapped[int] = mapped_column(Integer, default=200000)
    # 审查引擎自身异常时是否按「拦截」处理（默认 false = 放行并记录 error）
    fail_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    default_reply: Mapped[str] = mapped_column(
        Text, default="抱歉，该内容未通过安全审查，无法为您生成回复。"
    )
    # 系统内置项标记；builtin_key 用于在版本升级/恢复出厂时定位同一条规则
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin_key: Mapped[str] = mapped_column(String(64), default="", index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class SafetyReview(Base):
    """独立审查记录：只记录真正触发规则/异常的审查，避免正常请求刷屏。

    通过（passed）的结果直接写在 ``request_logs.safety_result``，
    命中与异常在这里保留词库、命中词、原文摘录等可追溯信息。
    """

    __tablename__ = "safety_reviews"
    __table_args__ = (
        Index("ix_safety_reviews_created", "created_at"),
        Index("ix_safety_reviews_key", "api_key_id"),
        Index("ix_safety_reviews_policy", "policy_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    key_name: Mapped[str] = mapped_column(String(128), default="")
    policy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_name: Mapped[str] = mapped_column(String(128), default="")
    library_names: Mapped[list] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(16), default="input", index=True)  # input/output
    action: Mapped[str] = mapped_column(String(16), default="blocked", index=True)  # blocked/error/overflow
    matched_terms: Mapped[list] = mapped_column(JSON, default=list)
    text_excerpt: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    endpoint: Mapped[str] = mapped_column(String(96), default="")
    is_stream: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())


class Meta(Base):
    """键值元信息（如首次启动时间、迁移标记）。"""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    """可在后台修改的运行时配置（覆盖环境变量里的默认值）。

    只有少量需要“改完立即生效、不想重启容器”的开关放这里，
    其余仍以环境变量为准。
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


def key_channel(api_key: "ApiKey", service: str) -> str | None:
    """取该 Key 对某个服务指定的渠道(provider_id)。

    * 优先读按服务的 ``channels`` 映射；
    * 兼容旧数据：仅当 ``channels`` 为空且配了旧 ``channel`` 时，回退到旧值
      （旧值无法判断服务，启动时会由 ``backfill_channels`` 迁移到 channels）。
    """
    ch = getattr(api_key, "channels", None) or {}
    if service in ch and ch[service]:
        return ch[service]
    if not ch:
        legacy = getattr(api_key, "channel", None)
        if legacy:
            return legacy
    return None
