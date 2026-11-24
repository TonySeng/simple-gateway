"""统一统计：一套仪表盘覆盖所有中转服务。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_admin
from app.core.models import ApiKey, Provider, RequestLog
from app.core.registry import all_services
from app.core.schemas import NameStat, SeriesPoint, ServiceStat, StatsOverview

router = APIRouter(prefix="/api/admin/stats", tags=["管理"])


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(days, 0))


def _base(db: Session, days: int, service: Optional[str]):
    q = db.query(RequestLog)
    if days > 0:
        q = q.filter(RequestLog.created_at >= _since(days))
    if service:
        q = q.filter(RequestLog.service == service)
    return q


@router.get("/overview", response_model=StatsOverview, summary="总览")
def overview(
    days: int = Query(7, ge=0, le=365),
    service: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    q = _base(db, days, service)
    total = q.count()
    success = q.filter(RequestLog.status == "success").count()
    error = total - success

    agg = (
        db.query(
            func.coalesce(func.sum(RequestLog.tokens_in + RequestLog.tokens_out), 0),
            func.coalesce(func.sum(RequestLog.cost), 0.0),
            func.coalesce(func.sum(RequestLog.char_count), 0),
            func.coalesce(func.avg(RequestLog.latency_ms), 0),
        )
        .filter(RequestLog.created_at >= _since(days) if days > 0 else True)
        .filter(RequestLog.service == service if service else True)
        .first()
    )
    tokens, cost, chars, avg_latency = (int(agg[0]), float(agg[1]), int(agg[2]), int(agg[3] or 0))

    services: list[ServiceStat] = []
    labels = {s.key: s.label for s in all_services()}
    for key in labels:
        if service and key != service:
            continue
        sq = _base(db, days, key)
        s_total = sq.count()
        s_success = sq.filter(RequestLog.status == "success").count()
        s_agg = (
            db.query(
                func.coalesce(func.sum(RequestLog.tokens_in + RequestLog.tokens_out), 0),
                func.coalesce(func.sum(RequestLog.cost), 0.0),
                func.coalesce(func.sum(RequestLog.char_count), 0),
            )
            .filter(RequestLog.created_at >= _since(days) if days > 0 else True)
            .filter(RequestLog.service == key)
            .first()
        )
        services.append(
            ServiceStat(
                service=key,
                label=labels[key],
                requests=s_total,
                success=s_success,
                error=s_total - s_success,
                tokens=int(s_agg[0]),
                cost=round(float(s_agg[1]), 4),
                chars=int(s_agg[2]),
            )
        )

    safety_q = _base(db, days, service)
    safety_input = safety_q.filter(RequestLog.safety_result == "blocked_input").count()
    safety_output = safety_q.filter(RequestLog.safety_result == "blocked_output").count()
    safety_overflow = safety_q.filter(RequestLog.safety_result == "blocked_overflow").count()

    return StatsOverview(
        total_requests=total,
        success=success,
        error=error,
        success_rate=round(success / total * 100, 2) if total else 0.0,
        tokens=tokens,
        cost=round(cost, 4),
        chars=chars,
        avg_latency_ms=avg_latency,
        total_keys=db.query(ApiKey).count(),
        active_keys=db.query(ApiKey).filter(ApiKey.status == "active").count(),
        providers_total=(
            db.query(Provider).filter(Provider.service == service).count()
            if service
            else db.query(Provider).count()
        ),
        providers_enabled=(
            db.query(Provider)
            .filter(Provider.service == service, Provider.is_enabled.is_(True))
            .count()
            if service
            else db.query(Provider).filter(Provider.is_enabled.is_(True)).count()
        ),
        safety_blocked_input=safety_input,
        safety_blocked_output=safety_output,
        safety_blocked_overflow=safety_overflow,
        services=services,
    )


@router.get("/series", response_model=list[SeriesPoint], summary="按天趋势")
def series(
    days: int = Query(7, ge=1, le=90),
    service: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    since = _since(days)
    day = func.date(RequestLog.created_at)
    q = (
        db.query(
            day.label("d"),
            func.count(),
            func.sum(case((RequestLog.status == "success", 1), else_=0)),
            func.coalesce(func.sum(RequestLog.cost), 0.0),
            func.coalesce(func.sum(RequestLog.tokens_in + RequestLog.tokens_out), 0),
        )
        .filter(RequestLog.created_at >= since)
    )
    if service:
        q = q.filter(RequestLog.service == service)
    rows = q.group_by(day).order_by(day).all()

    got = {
        str(r[0]): SeriesPoint(
            date=str(r[0]),
            requests=int(r[1] or 0),
            success=int(r[2] or 0),
            cost=round(float(r[3] or 0), 4),
            tokens=int(r[4] or 0),
        )
        for r in rows
    }
    out: list[SeriesPoint] = []
    for i in range(days):
        d = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        out.append(got.get(d, SeriesPoint(date=d)))
    return out


@router.get("/breakdown", summary="按模型 / 厂商 / Key 聚合")
def breakdown(
    days: int = Query(7, ge=0, le=365),
    service: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    since = _since(days)

    def _agg(col):
        q = db.query(
            col.label("name"),
            func.count(),
            func.coalesce(func.sum(RequestLog.tokens_in + RequestLog.tokens_out), 0),
            func.coalesce(func.sum(RequestLog.cost), 0.0),
        )
        if days > 0:
            q = q.filter(RequestLog.created_at >= since)
        if service:
            q = q.filter(RequestLog.service == service)
        rows = q.group_by(col).order_by(func.count().desc()).limit(20).all()
        out = [
            NameStat(
                name=str(r[0] or ""),
                requests=int(r[1] or 0),
                tokens=int(r[2] or 0),
                cost=round(float(r[3] or 0), 4),
            )
            for r in rows
        ]
        # OCR 等无模型的调用会产生空名称聚合，避免干扰展示
        return [x for x in out if x.name.strip() and x.name != "-"]

    # 失败原因分布：按 error_type 聚合，用于快速判断「为什么失败」
    eq = db.query(
        RequestLog.error_type.label("name"),
        func.count(),
        func.coalesce(func.sum(RequestLog.tokens_in + RequestLog.tokens_out), 0),
        func.coalesce(func.sum(RequestLog.cost), 0.0),
    ).filter(RequestLog.status == "error")
    if days > 0:
        eq = eq.filter(RequestLog.created_at >= since)
    if service:
        eq = eq.filter(RequestLog.service == service)
    error_rows = eq.group_by(RequestLog.error_type).order_by(func.count().desc()).limit(20).all()
    by_error = [
        NameStat(
            name=str(r[0] or "unclassified"),
            requests=int(r[1] or 0),
            tokens=int(r[2] or 0),
            cost=round(float(r[3] or 0), 4),
        )
        for r in error_rows
    ]

    return {
        "by_model": _agg(RequestLog.model),
        "by_provider": _agg(RequestLog.provider_name),
        "by_key": _agg(RequestLog.key_name),
        "by_error": by_error,
    }
