"""日志详情的保留期配置与定期清理。

只清空 ``request_body`` / ``response_body`` / ``upstream_body`` 这些大字段，
**不删除日志行** —— 否则用量统计和失败率会凭空变小，历史账目失真。

保留天数可在后台修改（存 ``app_settings`` 表），改完即时生效；
环境变量 ``SG_LOG_DETAIL_DAYS`` 只作为初始默认值。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import AppSetting, RequestLog, SafetyReview

logger = logging.getLogger("gateway.logretention")

SETTING_KEY = "log_detail_days"
MIN_DAYS = 0      # 0 = 不保留（关掉详情记录之外的清理，即立即清空全部历史详情）
MAX_DAYS = 365
CLEANUP_INTERVAL_S = 3600  # 每小时检查一次


def _fallback_days() -> int:
    return max(MIN_DAYS, min(MAX_DAYS, int(settings.log_detail_days)))


def get_retention_days(db: Session | None = None) -> int:
    """当前生效的保留天数：DB 设置优先，否则用环境变量默认值。

    表尚未创建（首次启动、独立脚本）时直接回落到默认值，不抛异常。
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == SETTING_KEY).first()
        if row is not None:
            try:
                return max(MIN_DAYS, min(MAX_DAYS, int(row.value)))
            except (TypeError, ValueError):
                pass
        return _fallback_days()
    except Exception:  # noqa: BLE001 - 表缺失等情况下不该让调用方崩
        if own:
            db.rollback()
        return _fallback_days()
    finally:
        if own:
            db.close()


def set_retention_days(db: Session, days: int) -> int:
    value = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    row = db.query(AppSetting).filter(AppSetting.key == SETTING_KEY).first()
    if row is None:
        row = AppSetting(key=SETTING_KEY, value=str(value))
        db.add(row)
    else:
        row.value = str(value)
        row.updated_at = datetime.utcnow()
    db.commit()
    return value


def count_rows(db: Session) -> int:
    """当前日志总条数（清理详情不会改变这个值）。"""
    return int(db.scalar(select(func.count()).select_from(RequestLog)) or 0)


def purge_expired(db: Session, days: int | None = None) -> int:
    """清空超期日志的详情字段，返回被清理的行数。

    这里只用 UPDATE 把 ``request_body`` / ``response_body`` / ``upstream_body``
    置空，**永不 DELETE**：日志行承载 tokens、耗时、费用、失败原因等统计信息，
    删掉会让历史用量凭空变少。需要减少体积时也只清详情。
    """
    retain = get_retention_days(db) if days is None else max(MIN_DAYS, int(days))
    cutoff = datetime.utcnow() - timedelta(days=retain)
    # 只处理确实还有内容的行，避免每小时全表 UPDATE
    cond = or_(
        RequestLog.request_body != "",
        RequestLog.response_body != "",
        RequestLog.upstream_body != "",
    )
    stmt = (
        update(RequestLog)
        .where(RequestLog.created_at < cutoff, cond)
        .values(request_body="", response_body="", upstream_body="")
        .execution_options(synchronize_session=False)
    )
    result = db.execute(stmt)
    # 审查记录同样只清空原文摘录，保留命中词、规则与统计信息
    review_stmt = (
        update(SafetyReview)
        .where(SafetyReview.created_at < cutoff, SafetyReview.text_excerpt != "")
        .values(text_excerpt="")
        .execution_options(synchronize_session=False)
    )
    review_result = db.execute(review_stmt)
    db.commit()
    count = int(result.rowcount or 0)
    reviews = int(review_result.rowcount or 0)
    if count or reviews:
        logger.info(
            "已清理 %s 条超期日志详情、%s 条审查原文摘录（保留 %s 天）",
            count, reviews, retain,
        )
    return count


async def cleanup_loop(stop: asyncio.Event | None = None) -> None:
    """后台常驻任务：启动即清一次，之后每小时一次。"""
    while True:
        try:
            with SessionLocal() as db:
                purge_expired(db)
        except Exception:  # noqa: BLE001
            logger.exception("日志详情清理失败（不影响服务）")
        if stop is None:
            await asyncio.sleep(CLEANUP_INTERVAL_S)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL_S)
            return  # 收到停止信号
        except asyncio.TimeoutError:
            continue
