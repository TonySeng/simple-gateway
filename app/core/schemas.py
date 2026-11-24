"""管理端共用数据契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------- 登录 ----------------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class AdminMe(BaseModel):
    username: str
    auth_enabled: bool
    app_name: str
    version: str
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------------- 提供商 ----------------
class ProviderCreate(BaseModel):
    service: str
    type: str
    name: str
    base_url: str = ""
    is_enabled: bool = True
    is_default: bool = False
    weight: int = 100      # 越大越优先（降级顺序）
    priority: int = 100    # 越小越优先
    timeout_s: Optional[float] = Field(None, ge=0, le=3600)  # 上游超时（秒），None/0=用全局默认
    credentials: dict[str, Any] = Field(default_factory=dict)  # 明文，落库加密
    settings: dict[str, Any] = Field(default_factory=dict)     # 明文
    comment: Optional[str] = None


class ProviderUpdate(BaseModel):
    type: Optional[str] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    is_enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    weight: Optional[int] = None
    priority: Optional[int] = None
    timeout_s: Optional[float] = Field(None, ge=0, le=3600)
    # null/缺省 = 不修改；{} = 显式清空；非空 = 与原凭证合并
    credentials: Optional[dict[str, Any]] = None
    settings: Optional[dict[str, Any]] = None
    comment: Optional[str] = None


class ProviderRead(BaseModel):
    id: str
    service: str
    service_label: str = ""
    type: str
    type_label: str = ""
    name: str
    base_url: str = ""
    is_enabled: bool
    is_default: bool
    weight: int
    priority: int
    timeout_s: Optional[float] = None  # 0/None 表示使用全局默认
    effective_timeout_s: float = 0.0   # 实际生效值（含全局兜底），便于前端提示
    has_credentials: bool
    masked_credentials: dict[str, str] = {}
    missing_credentials: list[str] = []
    settings: dict[str, Any] = {}
    comment: Optional[str] = None
    models_count: int = 0
    created_at: datetime
    updated_at: datetime


class ProviderTestResult(BaseModel):
    ok: bool
    service: str
    provider: str
    type: str
    message: str = ""
    latency_ms: int = 0
    detail: dict[str, Any] = {}


# ---------------- API Key ----------------
class ApiKeyCreate(BaseModel):
    name: str
    allowed_services: list[str] = Field(default_factory=lambda: ["*"])
    allowed_providers: dict[str, list[str]] = Field(default_factory=dict)
    channel: Optional[str] = None  # 旧版单渠道（兼容）
    channels: dict[str, str] = Field(default_factory=dict)  # 按服务指定渠道 service->provider_id
    # None = 继承默认规则；"" = 关闭审查；具体 id = 指定规则
    safety_policy_id: Optional[str] = None
    quota_requests: int = 0
    quota_tokens: int = 0
    quota_cost: float = 0.0
    rate_limit: int = 0
    expires_at: Optional[datetime] = None
    comment: Optional[str] = None


class ApiKeyUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    allowed_services: Optional[list[str]] = None
    allowed_providers: Optional[dict[str, list[str]]] = None
    channel: Optional[str] = None  # 旧版单渠道（兼容）
    channels: Optional[dict[str, str]] = None  # 按服务指定渠道
    safety_policy_id: Optional[str] = None  # None=继承默认；""=关闭；id=指定规则
    quota_requests: Optional[int] = None
    quota_tokens: Optional[int] = None
    quota_cost: Optional[float] = None
    rate_limit: Optional[int] = None
    expires_at: Optional[datetime] = None
    comment: Optional[str] = None


class ApiKeyRead(BaseModel):
    id: str
    name: str
    key_prefix: str = ""
    status: str
    allowed_services: list[str] = []
    allowed_providers: dict[str, list[str]] = {}
    channel: Optional[str] = None  # 旧版单渠道（兼容回显，多服务时为空）
    channels: dict[str, str] = {}  # 按服务指定渠道 service->provider_id
    safety_policy_id: Optional[str] = None  # None=继承默认；""=关闭；id=指定规则
    quota_requests: int = 0
    quota_tokens: int = 0
    quota_cost: float = 0.0
    used_requests: int = 0
    used_tokens: int = 0
    used_cost: float = 0.0
    rate_limit: int = 0
    expires_at: Optional[datetime] = None
    comment: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None
    key: Optional[str] = None  # 仅创建/重置时返回一次明文


# ---------------- 日志与统计 ----------------
class LogItem(BaseModel):
    id: str
    service: str
    endpoint: str = ""
    key_name: str = ""
    provider_name: str = ""
    provider_type: str = ""
    model: str = ""
    status: str
    http_status: Optional[int] = None
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    currency: str = "CNY"
    char_count: int = 0
    error_message: str = ""
    # 失败归因
    error_type: str = ""
    upstream_status: Optional[int] = None
    upstream_url: str = ""
    request_id: str = ""
    ip: str = ""
    # 安全审查结果："" / passed / blocked_input / blocked_output / blocked_overflow / error
    safety_result: str = ""
    safety_policy: str = ""
    safety_detail: str = ""
    # 是否有可查看的详情（请求体 / 上游返回体）。列表不返回大字段，点开再取。
    has_detail: bool = False
    created_at: datetime


class LogDetail(LogItem):
    """单条日志的完整详情（请求体、上游返回体）。"""

    request_body: str = ""
    response_body: str = ""
    upstream_body: str = ""


class LogRetention(BaseModel):
    """日志详情保留策略。"""

    days: int
    max_days: int = 365
    oldest_detail_at: Optional[datetime] = None  # 当前仍保留详情的最早时间
    detail_rows: int = 0                          # 仍带详情的日志条数
    total_rows: int = 0                           # 日志记录总条数（清理详情不会改变）


class LogCleanupResult(BaseModel):
    """清理结果：只清空详情内容，日志记录一条不少。"""

    cleared: int                                  # 被清空详情的条数
    days: int
    kept_rows: int = 0                            # 清理后仍保留的日志记录数


class LogPage(BaseModel):
    items: list[LogItem]
    total: int
    page: int
    page_size: int


class LiveRequest(BaseModel):
    """一条仍在处理中的请求（未落库）。"""

    request_id: str
    service: str
    endpoint: str = ""
    key_id: str = ""
    key_name: str = ""
    model: str = ""
    provider_name: str = ""
    provider_type: str = ""
    client_ip: str = ""
    user_agent: str = ""
    stream: bool = False
    stage: str = "routing"
    stage_label: str = ""
    upstream_status: Optional[int] = None
    started_at: float = 0.0        # epoch 秒
    started_at_ms: int = 0         # epoch 毫秒（前端按本地时区格式化）
    elapsed_ms: int = 0            # 已持续毫秒（服务端计算）


class LiveSnapshot(BaseModel):
    now: datetime
    total: int = 0
    max_age_s: int = 1800
    items: list[LiveRequest] = []


class ServiceStat(BaseModel):
    service: str
    label: str
    requests: int = 0
    success: int = 0
    error: int = 0
    tokens: int = 0
    cost: float = 0.0
    chars: int = 0


class StatsOverview(BaseModel):
    total_requests: int = 0
    success: int = 0
    error: int = 0
    success_rate: float = 0.0
    tokens: int = 0
    cost: float = 0.0
    chars: int = 0
    avg_latency_ms: int = 0
    total_keys: int = 0
    active_keys: int = 0
    providers_total: int = 0
    providers_enabled: int = 0
    # 安全审查拦截量（输入/输出/超限），不计入失败率
    safety_blocked_input: int = 0
    safety_blocked_output: int = 0
    safety_blocked_overflow: int = 0
    services: list[ServiceStat] = []


class SeriesPoint(BaseModel):
    date: str
    requests: int = 0
    success: int = 0
    cost: float = 0.0
    tokens: int = 0


class NameStat(BaseModel):
    name: str
    requests: int = 0
    tokens: int = 0
    cost: float = 0.0


# ---------------- 安全审查 ----------------
class SafetyLibraryCreate(BaseModel):
    name: str
    description: str = ""
    terms: str = ""
    is_enabled: bool = True
    risk_level: str = "medium"
    hit_action: str = "block"


class SafetyLibraryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    terms: Optional[str] = None
    is_enabled: Optional[bool] = None
    risk_level: Optional[str] = None
    hit_action: Optional[str] = None


class SafetyLibraryRead(BaseModel):
    id: str
    name: str
    description: str = ""
    terms: str = ""
    term_count: int = 0
    is_enabled: bool = True
    risk_level: str = "medium"
    hit_action: str = "block"
    is_builtin: bool = False
    builtin_key: str = ""
    created_at: datetime
    updated_at: datetime


class SafetyPolicyCreate(BaseModel):
    name: str
    description: str = ""
    is_enabled: bool = True
    is_default: bool = False
    library_ids: list[str] = Field(default_factory=list)
    input_enabled: bool = True
    output_enabled: bool = True
    match_mode: str = "contains"
    case_sensitive: bool = False
    normalize: bool = True
    aggressive_normalize: bool = False
    threshold: int = Field(1, ge=1, le=1000)
    max_text_chars: int = Field(200000, ge=1000, le=2000000)
    fail_closed: bool = False
    default_reply: str = "抱歉，该内容未通过安全审查，无法为您生成回复。"


class SafetyPolicyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    library_ids: Optional[list[str]] = None
    input_enabled: Optional[bool] = None
    output_enabled: Optional[bool] = None
    match_mode: Optional[str] = None
    case_sensitive: Optional[bool] = None
    normalize: Optional[bool] = None
    aggressive_normalize: Optional[bool] = None
    threshold: Optional[int] = Field(None, ge=1, le=1000)
    max_text_chars: Optional[int] = Field(None, ge=1000, le=2000000)
    fail_closed: Optional[bool] = None
    default_reply: Optional[str] = None


class SafetyPolicyRead(BaseModel):
    id: str
    name: str
    description: str = ""
    is_enabled: bool = True
    is_default: bool = False
    library_ids: list[str] = []
    library_names: list[str] = []
    input_enabled: bool = True
    output_enabled: bool = True
    match_mode: str = "contains"
    case_sensitive: bool = False
    normalize: bool = True
    aggressive_normalize: bool = False
    threshold: int = 1
    max_text_chars: int = 200000
    fail_closed: bool = False
    default_reply: str = ""
    bound_keys: int = 0
    is_builtin: bool = False
    builtin_key: str = ""
    created_at: datetime
    updated_at: datetime


class SafetyTestRequest(BaseModel):
    text: str
    direction: str = "input"
    policy_id: Optional[str] = None


class SafetyTestMatch(BaseModel):
    term: str
    library: str = ""
    risk_level: str = "medium"


class SafetyTestResult(BaseModel):
    blocked: bool
    matched: list[SafetyTestMatch] = []
    policy_name: str = ""
    detail: str = ""
    truncated: bool = False
    # 汇总动作：block / sanitize / review / pass
    action: str = "pass"
    risk_level: str = ""


class SafetyReviewItem(BaseModel):
    id: str
    request_id: str = ""
    api_key_id: Optional[str] = None
    key_name: str = ""
    policy_id: Optional[str] = None
    policy_name: str = ""
    library_names: list[str] = []
    direction: str = "input"
    action: str = "blocked"
    matched_terms: list[dict[str, Any]] = []
    text_excerpt: str = ""
    model: str = ""
    endpoint: str = ""
    is_stream: bool = False
    detail: str = ""
    created_at: datetime


class SafetyReviewPage(BaseModel):
    items: list[SafetyReviewItem]
    total: int
    page: int
    page_size: int


class SafetyOverview(BaseModel):
    libraries: int = 0
    policies: int = 0
    reviews_today: int = 0
    blocked_input_today: int = 0
    blocked_output_today: int = 0


class SafetyBuiltinItem(BaseModel):
    key: str
    name: str
    kind: str  # library / policy
    exists: bool = False
    modified: bool = False


class SafetyBuiltinsRead(BaseModel):
    version: str = ""
    installed_version: str = ""
    items: list[SafetyBuiltinItem] = []
