"""统一调用日志查询（按服务 / 状态 / Key / 厂商 / 模型 / 时间过滤）。

列表只返回摘要，详情（请求体 / 上游返回体）走单独的详情接口：
一条日志的请求体可能有几十 KB，一页 50 条一起塞进列表会让接口慢得没法用。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, defer

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.logretention import (
    MAX_DAYS,
    MIN_DAYS,
    count_rows,
    get_retention_days,
    purge_expired,
    set_retention_days,
)
from app.core.models import RequestLog
from app.core.schemas import (
    LogCleanupResult,
    LogDetail,
    LogItem,
    LogPage,
    LogRetention,
)

router = APIRouter(prefix="/api/admin/logs", tags=["管理"])

# 列表查询需要的列；body 列不在其中（defer 掉，避免一页几十条的大字段传输）
SUMMARY_COLUMNS = (
    RequestLog.id,
    RequestLog.service,
    RequestLog.endpoint,
    RequestLog.key_name,
    RequestLog.provider_name,
    RequestLog.provider_type,
    RequestLog.model,
    RequestLog.status,
    RequestLog.http_status,
    RequestLog.latency_ms,
    RequestLog.tokens_in,
    RequestLog.tokens_out,
    RequestLog.cost,
    RequestLog.currency,
    RequestLog.char_count,
    RequestLog.error_message,
    RequestLog.error_type,
    RequestLog.upstream_status,
    RequestLog.upstream_url,
    RequestLog.request_id,
    RequestLog.ip,
    RequestLog.safety_result,
    RequestLog.safety_policy,
    RequestLog.safety_detail,
    RequestLog.created_at,
)

DETAIL_FLAG = case(
    (
        or_(
            RequestLog.request_body != "",
            RequestLog.response_body != "",
            RequestLog.upstream_body != "",
        ),
        True,
    ),
    else_=False,
).label("has_detail")


def _to_item(r: RequestLog, has_detail: bool) -> LogItem:
    return LogItem(
        id=r.id,
        service=r.service,
        endpoint=r.endpoint,
        key_name=r.key_name,
        provider_name=r.provider_name,
        provider_type=r.provider_type,
        model=r.model,
        status=r.status,
        http_status=r.http_status,
        latency_ms=r.latency_ms,
        tokens_in=r.tokens_in,
        tokens_out=r.tokens_out,
        cost=r.cost,
        currency=r.currency,
        char_count=r.char_count,
        error_message=r.error_message,
        error_type=r.error_type or "",
        upstream_status=r.upstream_status,
        upstream_url=r.upstream_url or "",
        request_id=r.request_id or "",
        ip=r.ip,
        safety_result=r.safety_result or "",
        safety_policy=r.safety_policy or "",
        safety_detail=r.safety_detail or "",
        has_detail=has_detail,
        created_at=r.created_at,
    )


def _to_detail(r: RequestLog) -> LogDetail:
    base = _to_item(
        r,
        bool(r.request_body or r.response_body or r.upstream_body),
    )
    return LogDetail(
        **base.model_dump(),
        request_body=r.request_body or "",
        response_body=r.response_body or "",
        upstream_body=r.upstream_body or "",
    )


def _retention_info(db: Session) -> LogRetention:
    """保留策略 + 当前详情占用情况，便于判断是否真的在清理。"""
    days = get_retention_days(db)
    has_body = or_(
        RequestLog.request_body != "",
        RequestLog.response_body != "",
        RequestLog.upstream_body != "",
    )
    row = db.execute(
        select(func.count(), func.min(RequestLog.created_at)).where(has_body)
    ).one()
    return LogRetention(
        days=days,
        max_days=MAX_DAYS,
        detail_rows=int(row[0] or 0),
        oldest_detail_at=row[1],
        total_rows=count_rows(db),
    )


@router.get("/retention", response_model=LogRetention, summary="查看日志详情保留策略")
def read_retention(
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    return _retention_info(db)


@router.put("/retention", response_model=LogRetention, summary="修改日志详情保留天数")
def update_retention(
    days: int,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"保留天数需在 {MIN_DAYS}~{MAX_DAYS} 之间",
        )
    set_retention_days(db, days)
    return _retention_info(db)


@router.post("/retention/cleanup", response_model=LogCleanupResult, summary="清空超期详情（保留记录）")
def cleanup_now(
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    days = get_retention_days(db)
    kept = count_rows(db)
    cleared = purge_expired(db, days)
    return LogCleanupResult(cleared=cleared, days=days, kept_rows=kept)


@router.get("", response_model=LogPage, summary="查询调用日志")
def query_logs(
    service: Optional[str] = None,
    status_filter: Optional[str] = None,
    key_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    model: Optional[str] = None,
    error_type: Optional[str] = None,
    safety_result: Optional[str] = None,
    request_id: Optional[str] = None,
    has_detail: Optional[bool] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    page = max(1, page)
    page_size = min(max(page_size, 1), 500)

    filters = []
    if service:
        filters.append(RequestLog.service == service)
    if status_filter:
        filters.append(RequestLog.status == status_filter)
    if key_id:
        filters.append(RequestLog.api_key_id == key_id)
    if provider_id:
        filters.append(RequestLog.provider_id == provider_id)
    if model:
        filters.append(RequestLog.model == model)
    if error_type:
        filters.append(RequestLog.error_type == error_type)
    if safety_result:
        filters.append(RequestLog.safety_result == safety_result)
    if request_id:
        filters.append(RequestLog.request_id == request_id)
    if has_detail is not None:
        has_body = or_(
            RequestLog.request_body != "",
            RequestLog.response_body != "",
            RequestLog.upstream_body != "",
        )
        filters.append(has_body if has_detail else ~has_body)
    if start:
        filters.append(RequestLog.created_at >= start.replace(tzinfo=None))
    if end:
        filters.append(RequestLog.created_at <= end.replace(tzinfo=None))

    count_stmt = select(func.count()).select_from(RequestLog)
    stmt = select(RequestLog, DETAIL_FLAG).options(
        defer(RequestLog.request_body),
        defer(RequestLog.response_body),
        defer(RequestLog.upstream_body),
    )
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    total = db.scalar(count_stmt) or 0
    rows = db.execute(
        stmt.order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return LogPage(
        items=[_to_item(r, bool(flag)) for r, flag in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{log_id}", response_model=LogDetail, summary="查看单条日志详情")
def get_log(
    log_id: str,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    r = db.query(RequestLog).filter(RequestLog.id == log_id).first()
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="日志不存在")
    return _to_detail(r)
