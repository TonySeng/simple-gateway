"""统一用量记账。

所有服务在完成一次调用后都调用 :func:`record`，从而：
* 写入一条统一的 ``request_logs``（带 service 维度）；
* 在原会话内重新取出 ApiKey 行并自增计数（避免跨 Session 修改不落库的坑）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logbody import limit_text
from app.core.models import ApiKey, Provider, RequestLog

logger = logging.getLogger("gateway.usage")


def record(
    db: Session,
    *,
    service: str,
    endpoint: str,
    key: Optional[ApiKey] = None,
    provider: Optional[Provider] = None,
    model: str = "",
    mode: str = "",
    status: str = "success",
    http_status: int | None = None,
    latency_ms: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
    currency: str = "CNY",
    char_count: int = 0,
    error_message: str = "",
    ip: str = "",
    # ---- 失败归因（可选，成功请求留空）----
    error_type: str = "",
    upstream_status: int | None = None,
    upstream_body: str = "",
    upstream_url: str = "",
    request_id: str = "",
    # ---- 详情内容（已脱敏；超过保留期会被清空）----
    request_body: str = "",
    response_body: str = "",
    # ---- 安全审查结果 ----
    safety_result: str = "",
    safety_policy: str = "",
    safety_detail: str = "",
) -> None:
    log = RequestLog(
        service=service,
        endpoint=endpoint,
        api_key_id=key.id if key else None,
        key_name=key.name if key else "",
        provider_id=provider.id if provider else None,
        provider_name=provider.name if provider else "",
        provider_type=provider.type if provider else "",
        model=model or "",
        mode=mode,
        status=status,
        http_status=http_status,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=round(cost, 8),
        currency=currency,
        char_count=char_count,
        error_message=(error_message or "")[:2000],
        ip=ip,
        error_type=(error_type or "")[:48],
        upstream_status=upstream_status,
        upstream_body=(upstream_body or "")[:2000],
        upstream_url=(upstream_url or "")[:512],
        request_id=(request_id or "")[:32],
        request_body=limit_text(request_body),
        response_body=limit_text(response_body),
        safety_result=(safety_result or "")[:32],
        safety_policy=(safety_policy or "")[:128],
        safety_detail=(safety_detail or "")[:2000],
    )
    db.add(log)

    if key is not None:
        # 在同一 Session 内重新取对象，确保 UPDATE 落库
        ak = db.query(ApiKey).filter(ApiKey.id == key.id).first()
        if ak is not None:
            ak.used_requests += 1
            ak.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
            if status == "success":
                ak.used_tokens += (tokens_in or 0) + (tokens_out or 0)
                ak.used_cost = round((ak.used_cost or 0.0) + (cost or 0.0), 8)

    try:
        db.commit()
    except Exception:  # 记账失败不应影响已返回给客户端的结果
        logger.exception("写入用量日志失败")
        db.rollback()
