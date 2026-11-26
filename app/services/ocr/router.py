"""OCR 对外接口：统一格式，屏蔽各厂商差异。

* ``POST /v1/ocr/recognize``      JSON（base64 或 URL）
* ``POST /v1/ocr/recognize/file`` 文件上传
* ``GET  /v1/ocr/types``          当前网关支持的厂商类型
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.concurrency import effective_max_concurrency, key_concurrency
from app.core.db import get_db
from app.core.deps import allowed_provider_types, api_key_auth, get_client_ip
from app.core.live import live
from app.core.logbody import dump_body
from app.core.models import ApiKey, key_channel
from app.core.usage import record
from app.services.ocr.providers.base import get_client
from app.services.ocr.routing import PROVIDER_TYPE_SPECS, route_recognize
from app.services.ocr.schemas import OCRBlock, OCRRequest, OCRResult

router = APIRouter(prefix="/v1/ocr", tags=["OCR"])


def _detect_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return "application/octet-stream"


def _decode_base64(s: str) -> bytes:
    s = (s or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


async def _fetch_url(url: str) -> tuple[bytes, str]:
    resp = await get_client().get(url, timeout=20)
    resp.raise_for_status()
    return resp.content, resp.headers.get("content-type", "application/octet-stream")


@router.get("/types", summary="支持的 OCR 厂商类型")
def list_types(key: ApiKey = Depends(api_key_auth("ocr"))):
    return {
        "types": [
            {
                "type": t.type,
                "label": t.label,
                "description": t.description,
                "default_engines": [f["placeholder"] for f in t.setting_fields if f.get("name") == "action"][:1],
            }
            for t in PROVIDER_TYPE_SPECS
        ]
    }


@router.post("/recognize", response_model=OCRResult, summary="OCR 识别（JSON）")
async def recognize(
    body: OCRRequest,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(api_key_auth("ocr")),
):
    if not body.image_base64 and not body.image_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="请提供 image_base64 或 image_url")
    # 请求详情里不存图片本身（太大），只存足以复现调用的元信息
    req_desc = dump_body({
        "provider": body.provider, "engine": body.engine, "options": body.options,
        "with_raw": body.with_raw,
        "image_url": body.image_url or "",
        "image_base64_chars": len(body.image_base64 or ""),
    })
    t0 = time.monotonic()
    try:
        if body.image_url:
            image, mime = await _fetch_url(body.image_url)
        else:
            image = _decode_base64(body.image_base64)
            mime = _detect_mime(image)
    except Exception as e:
        record(
            db, service="ocr", endpoint="/v1/ocr/recognize", key=key,
            status="error", latency_ms=int((time.monotonic() - t0) * 1000),
            error_message=f"图片解析失败: {e}", ip=get_client_ip(request),
            error_type="invalid_request",
            request_body=req_desc,
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"图片解析失败：{e}")

    return await _run(
        db, key, image, mime, body.provider, body.engine, body.options, body.with_raw,
        "/v1/ocr/recognize", get_client_ip(request), t0, req_desc,
        request.headers.get("user-agent", ""),
    )


@router.post("/recognize/file", response_model=OCRResult, summary="OCR 识别（文件上传）")
async def recognize_file(
    request: Request,
    file: UploadFile = File(...),
    provider: Optional[str] = Form(None),
    engine: Optional[str] = Form(None),
    options: str = Form("{}"),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(api_key_auth("ocr")),
):
    try:
        opts = json.loads(options or "{}")
    except Exception:
        opts = {}
    image = await file.read()
    mime = file.content_type or _detect_mime(image)
    req_desc = dump_body({
        "provider": provider, "engine": engine, "options": opts,
        "filename": file.filename or "", "content_type": file.content_type or "",
        "bytes": len(image),
    })
    t0 = time.monotonic()
    return await _run(
        db, key, image, mime, provider, engine, opts, False,
        "/v1/ocr/recognize/file", get_client_ip(request), t0, req_desc,
        request.headers.get("user-agent", ""),
    )


async def _run(db, key, image, mime, provider, engine, options, with_raw, endpoint, ip, t0,
               req_desc: str = "", user_agent: str = "") -> OCRResult:
    rid = uuid.uuid4().hex
    # 单 Key 并发限制：达到上限后不调用上游。放在解析图片之后、发起上游请求之前。
    max_concurrency = effective_max_concurrency(key)
    if not key_concurrency.try_acquire(key.id, rid, max_concurrency):
        record(
            db, service="ocr", endpoint=endpoint, key=key,
            status="error", latency_ms=int((time.monotonic() - t0) * 1000),
            error_message=f"并发请求已达上限（{max_concurrency}）", ip=ip,
            error_type="concurrency_limit", request_body=req_desc,
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"当前 API Key 的并发请求已达上限（{max_concurrency}），请等待部分请求完成后重试",
        )
    live.start(rid, service="ocr", endpoint=endpoint, key_id=key.id, key_name=key.name,
               client_ip=ip, user_agent=user_agent)
    try:
        return await _run_inner(
            db, key, image, mime, provider, engine, options, with_raw,
            endpoint, ip, t0, req_desc, rid,
        )
    finally:
        live.finish(rid)
        key_concurrency.release(key.id, rid)


async def _run_inner(db, key, image, mime, provider, engine, options, with_raw,
                     endpoint, ip, t0, req_desc, rid) -> OCRResult:
    allowed = allowed_provider_types(key, "ocr")
    try:
        result, cfg = await route_recognize(db, image, mime, engine, options, provider, allowed, key_channel(key, "ocr"))
    except HTTPException as e:
        record(
            db, service="ocr", endpoint=endpoint, key=key, provider=None,
            status="error", latency_ms=int((time.monotonic() - t0) * 1000),
            error_message=str(e.detail), http_status=e.status_code, ip=ip,
            error_type=getattr(e, "error_type", "") or "",
            request_body=req_desc,
        )
        raise

    live.update(rid, provider_name=cfg.name, provider_type=cfg.type, stage="writing")
    cost_ms = int((time.monotonic() - t0) * 1000)
    text = result.get("text", "") or ""
    blocks = [OCRBlock(**b) for b in (result.get("blocks") or [])]
    out = OCRResult(
        request_id=rid,
        provider=cfg.type,
        provider_name=cfg.name,
        engine=engine or result.get("engine"),
        text=text,
        blocks=blocks,
        pages=int(result.get("pages", 1) or 1),
        char_count=int(result.get("char_count", len(text)) or 0),
        cost_ms=cost_ms,
        raw=result.get("raw") if with_raw else None,
    )
    record(
        db, service="ocr", endpoint=endpoint, key=key, provider=cfg,
        status="success", http_status=200, latency_ms=cost_ms,
        char_count=out.char_count, ip=ip,
        request_body=req_desc,
        response_body=dump_body(result),
    )
    return out
