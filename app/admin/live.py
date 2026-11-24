"""实时监控：当前正在处理、尚未返回的请求。

这些请求还没写进 ``request_logs``（日志是请求结束才落库），所以只能从内存表读。
一个请求处理完就自动从这里消失，历史照旧去「用量与日志」查。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app.core.deps import get_current_admin
from app.core.live import MAX_AGE_S, live
from app.core.schemas import LiveRequest, LiveSnapshot

router = APIRouter(prefix="/api/admin/live", tags=["管理"])


@router.get("", response_model=LiveSnapshot, summary="进行中的请求（实时）")
def snapshot(admin: str = Depends(get_current_admin)):
    rows = live.snapshot()
    return LiveSnapshot(
        now=datetime.utcnow(),
        total=len(rows),
        max_age_s=MAX_AGE_S,
        items=[LiveRequest(**r) for r in rows],
    )
