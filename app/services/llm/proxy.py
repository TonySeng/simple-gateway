"""LLM 转发引擎：路由、上游调用、双协议转换、流式、计费与日志。

内部统一为 OpenAI chat 格式；入站可为 OpenAI（/v1/chat/completions）或
Anthropic（/v1/messages），出站由厂商类型决定，二者可任意交叉组合。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.live import live
from app.core.models import ApiKey, Model, Provider, key_channel
from app.core.logbody import BodyCollector, dump_body, limit_text, redact
from app.core.providers import httpx_timeout, resolve_timeout
from app.core.usage import record
from app.services.llm import dispatch
from app.services.llm import safety
from app.services.llm.translators import (
    anthropic_event_to_openai,
    anthropic_to_openai_request,
    anthropic_to_openai_response,
    openai_chunk_to_anthropic,
    openai_to_anthropic_response,
)

logger = logging.getLogger("gateway.llm")

_client: httpx.AsyncClient | None = None

# ---- 失败归因分类（写入 request_logs.error_type，便于聚合统计）----
ERROR_INVALID_REQUEST = "invalid_request"        # 入参不合法（非 JSON / 缺 model）
ERROR_MODEL_NOT_FOUND = "model_not_found"        # 模型不存在或未启用
ERROR_CHANNEL_UNAVAILABLE = "channel_unavailable"  # Key 钉定的渠道不可用
ERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"  # 上游厂商被停用
ERROR_UPSTREAM_STATUS = "upstream_status"        # 上游返回非 2xx
ERROR_UPSTREAM_TIMEOUT = "upstream_timeout"      # 连接/读取超时
ERROR_UPSTREAM_CONNECT = "upstream_connect"      # DNS / 拒绝连接 / TLS
ERROR_UPSTREAM_NON_JSON = "upstream_non_json"    # 2xx 但响应体不是 JSON
ERROR_UPSTREAM_STREAM = "upstream_stream_error"  # 流内返回 error 事件
ERROR_STREAM_INTERRUPTED = "stream_interrupted"  # 流传输中途断开
ERROR_EMPTY_STREAM = "empty_stream"              # 上游 200 但一个 data 事件都没有
ERROR_INTERNAL = "internal_error"                # 网关自身异常

# 上游响应体脱敏（实现见 app.core.logbody，OCR 侧共用）
_redact = redact


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=settings.proxy_timeout)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _err(protocol: str, status_code: int, message: str, err_type: str = "invalid_request_error",
         request_id: str = "") -> JSONResponse:
    if protocol == "anthropic":
        body = {"type": "error", "error": {"type": err_type, "message": message}}
    else:
        body = {"error": {"message": message, "type": err_type, "code": err_type}}
    headers = {"X-Request-Id": request_id} if request_id else None
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _safety_response(
    protocol: str,
    model: str,
    reply: str,
    request_id: str,
    stream: bool,
    result: str,
    usage: dict[str, int] | None = None,
):
    """命中审查时按调用方是否 stream 返回 JSON 或 SSE，避免流式客户端解析失败。"""
    headers = {"X-Request-Id": request_id, "X-Safety-Review": result}
    if stream:
        return StreamingResponse(
            iter(safety.blocked_stream(protocol, model, reply, request_id, usage)),
            media_type="text/event-stream",
            headers={
                **headers,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(
        status_code=200,
        content=safety.blocked_response(protocol, model, reply, request_id, usage),
        headers=headers,
    )


def client_status(code: int) -> int:
    """上游 HTTP 状态码 → 网关对外（以及日志 http_status）用的状态码。

    4xx / 5xx 原样透传：调用方需要区分「模型在该分组没渠道(503)」「请求参数错(400)」
    「被限流(429)」「上游网关挂了(502)」。历史实现把所有上游异常折叠成 502，
    导致客户端只看到一个笼统的 502，必须登录后台翻日志才知道真实原因。
    1xx / 3xx / 非法值对客户端没有可传达语义，仍统一回 502。
    """
    return code if 400 <= code <= 599 else 502


def _classify_exc(e: Exception, timeout_s: float | None = None) -> tuple[str, str]:
    """把传输层异常归类为 (error_type, 人类可读描述)。"""
    if isinstance(e, httpx.TimeoutException):
        limit = f"，超时阈值 {timeout_s:g}s" if timeout_s else ""
        kind = "连接超时" if isinstance(e, httpx.ConnectTimeout) else "读取超时"
        return ERROR_UPSTREAM_TIMEOUT, f"上游{kind}（{type(e).__name__}{limit}）"
    if isinstance(e, httpx.ConnectError):
        return ERROR_UPSTREAM_CONNECT, f"无法连接上游（{type(e).__name__}）"
    if isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)):
        return ERROR_STREAM_INTERRUPTED, f"上游连接中断（{type(e).__name__}）"
    return ERROR_UPSTREAM_CONNECT, f"上游请求失败（{type(e).__name__}）"


def _pick_model(db: Session, model_name: str) -> Model | None:
    return (
        db.query(Model)
        .filter(Model.name == model_name, Model.is_active.is_(True))
        .order_by(Model.id)
        .first()
    )


def _compute_price(model: Model, tokens_in: int, tokens_out: int) -> float:
    return round(
        (tokens_in / 1000.0) * (model.input_price or 0.0)
        + (tokens_out / 1000.0) * (model.output_price or 0.0),
        8,
    )


def _log(
    db: Session,
    *,
    key: ApiKey,
    provider: Provider | None,
    model: Model | None,
    protocol: str,
    tokens_in: int,
    tokens_out: int,
    cost: float,
    ok: bool,
    error_message: str,
    latency_ms: int,
    client_ip: str,
    http_status: int | None = None,
    error_type: str = "",
    upstream_status: int | None = None,
    upstream_body: str = "",
    upstream_url: str = "",
    request_id: str = "",
    request_body: str = "",
    response_body: str = "",
    model_name: str | None = None,
    safety_result: str = "",
    safety_policy: str = "",
    safety_detail: str = "",
) -> None:
    record(
        db,
        service="llm",
        endpoint="/v1/messages" if protocol == "anthropic" else "/v1/chat/completions",
        key=key,
        provider=provider,
        model=(model.name if model else (model_name or "")),
        mode="chat",
        status="success" if ok else "error",
        http_status=http_status if http_status is not None else (200 if ok else 502),
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        currency=(model.currency if model else settings.default_currency),
        error_message=error_message,
        ip=client_ip,
        error_type=error_type,
        upstream_status=upstream_status,
        upstream_body=upstream_body,
        upstream_url=upstream_url,
        request_id=request_id,
        request_body=request_body,
        response_body=response_body,
        safety_result=safety_result,
        safety_policy=safety_policy,
        safety_detail=safety_detail,
    )


def _fail(
    db: Session | None,
    *,
    key: ApiKey,
    protocol: str,
    error_type: str,
    message: str,
    status_code: int = 502,
    provider: Provider | None = None,
    model: Model | None = None,
    model_name: str = "",
    latency_ms: int = 0,
    upstream_status: int | None = None,
    upstream_body: str = "",
    upstream_url: str = "",
    client_ip: str = "",
    request_id: str = "",
    request_body: str = "",
    response_body: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> JSONResponse:
    """记录一条失败日志并返回错误响应。所有失败出口都应走这里。"""
    if db is not None:
        try:
            _log(
                db,
                key=key,
                provider=provider,
                model=model,
                protocol=protocol,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost=cost,
                ok=False,
                error_message=message,
                latency_ms=latency_ms,
                client_ip=client_ip,
                http_status=status_code,
                error_type=error_type,
                upstream_status=upstream_status,
                upstream_body=upstream_body,
                upstream_url=upstream_url,
                request_id=request_id,
                request_body=request_body,
                response_body=response_body or upstream_body,
                model_name=model_name or (model.name if model else ""),
            )
        except Exception:  # 记账失败不能影响错误响应
            logger.exception("写入失败日志时出错（已忽略）")
    logger.warning(
        "LLM 请求失败 type=%s status=%s model=%s provider=%s rid=%s detail=%s",
        error_type, status_code, model_name or (model.name if model else "-"),
        provider.name if provider else "-", request_id, message[:300],
    )
    # 失败即摘除实时条目：_fail 是所有失败路径的唯一出口，把摘除放在这里，
    # 任何提前返回（上游非 2xx、连接失败、参数非法…）都不会留下僵死条目。
    # 与 handle_chat 的 finally、wrapped() 的 finally 重复调用也没关系（finish 幂等）。
    live.finish(request_id)
    # error_type 一并透出：客户端据此区分 upstream_status / upstream_timeout /
    # upstream_connect 等归因，不必登录后台猜。未分类时退回通用错误类型。
    return _err(
        protocol, status_code, message,
        err_type=error_type or "invalid_request_error",
        request_id=request_id,
    )


async def handle_chat(protocol: str, api_key: ApiKey, request: Request, client_ip: str):
    start = time.time()
    rid = uuid.uuid4().hex
    db = SessionLocal()
    provider: Provider | None = None
    model: Model | None = None
    model_name = ""
    req_text = ""  # 客户端原始请求体（脱敏截断），详情页用
    stream_handed_off = False  # 流式响应交给 StreamingResponse 后，由它负责移除实时条目

    # 一进站就登记，后台「实时监控」立刻能看到（此时还不知道模型/厂商，解析后补）
    live.start(
        rid,
        service="llm",
        endpoint="/v1/messages" if protocol == "anthropic" else "/v1/chat/completions",
        key_id=api_key.id,
        key_name=api_key.name,
        client_ip=client_ip,
        user_agent=request.headers.get("user-agent", ""),
    )

    def fail(**kw) -> JSONResponse:
        """本请求内的统一失败出口：自动带上请求体，省得每处都传。"""
        kw.setdefault("request_body", req_text)
        return _fail(
            db, key=api_key, protocol=protocol, client_ip=client_ip,
            request_id=rid, **kw,
        )

    try:
        try:
            body = await request.json()
        except Exception:
            # 非法 JSON 也留证：原始报文往往正是排障的关键
            try:
                req_text = dump_body((await request.body()))
            except Exception:
                req_text = ""
            return fail(error_type=ERROR_INVALID_REQUEST,
                        message="请求体必须是合法的 JSON", status_code=400,
                        latency_ms=int((time.time() - start) * 1000))
        if not isinstance(body, dict) or not body.get("model"):
            req_text = dump_body(body)
            return fail(error_type=ERROR_INVALID_REQUEST,
                        message="缺少 model 字段", status_code=400,
                        latency_ms=int((time.time() - start) * 1000))

        req_text = dump_body(body)
        model_name = str(body["model"])

        # 指定渠道（按服务）：该 Key 对 llm 服务钉定了某上游厂商，仅允许走此渠道
        llm_channel = key_channel(api_key, "llm")
        if llm_channel:
            provider = (
                db.query(Provider)
                .filter(Provider.id == llm_channel, Provider.service == "llm")
                .first()
            )
            if provider is None or not provider.is_enabled:
                return fail(error_type=ERROR_CHANNEL_UNAVAILABLE,
                            message="该 Key 指定的渠道不可用或已停用", status_code=502,
                            provider=provider, model_name=model_name,
                            latency_ms=int((time.time() - start) * 1000))
            model = (
                db.query(Model)
                .filter(
                    Model.name == model_name,
                    Model.provider_id == provider.id,
                    Model.is_active.is_(True),
                )
                .first()
            )
            if model is None:
                return fail(error_type=ERROR_MODEL_NOT_FOUND,
                            message=f"模型 '{model_name}' 不在该 Key 指定的渠道中",
                            status_code=404, provider=provider, model_name=model_name,
                            latency_ms=int((time.time() - start) * 1000))
        else:
            model = _pick_model(db, model_name)
            if model is None:
                return fail(error_type=ERROR_MODEL_NOT_FOUND,
                            message=f"模型 '{model_name}' 不存在或未启用",
                            status_code=404, model_name=model_name,
                            latency_ms=int((time.time() - start) * 1000))

            provider = db.query(Provider).filter(Provider.id == model.provider_id).first()
            if provider is None or not provider.is_enabled or provider.service != "llm":
                return fail(error_type=ERROR_PROVIDER_UNAVAILABLE,
                            message=f"模型 '{model_name}' 的上游供应商不可用",
                            status_code=502, provider=provider, model=model,
                            model_name=model_name,
                            latency_ms=int((time.time() - start) * 1000))

        internal = anthropic_to_openai_request(body) if protocol == "anthropic" else body
        stream = bool(internal.get("stream"))

        # ---- 输入预审查：必须在发出任何上游请求之前完成 ----
        safety_policy = safety.resolve_policy(db, api_key)
        input_safety_result = ""
        input_safety_detail = ""
        if safety_policy is not None:
            live.update(rid, stage="reviewing")
            try:
                if safety_policy.input_enabled:
                    sres = safety.review_texts(
                        safety.extract_request_texts(internal),
                        safety_policy,
                        safety.DIRECTION_INPUT,
                        db=db,
                    )
                    if sres.blocked:
                        reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                        safety.record_review(
                            db, request_id=rid, key=api_key, policy=safety_policy,
                            direction=safety.DIRECTION_INPUT, action="blocked",
                            result=sres, model=model.name, endpoint=(
                                "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                            ),
                        )
                        _log(
                            db, key=api_key, provider=provider, model=model, protocol=protocol,
                            tokens_in=0, tokens_out=0, cost=0.0, ok=True, error_message="",
                            latency_ms=int((time.time() - start) * 1000), client_ip=client_ip,
                            http_status=200, request_id=rid, request_body=req_text,
                            response_body=reply, safety_result=safety.RESULT_BLOCKED_INPUT,
                            safety_policy=safety_policy.name, safety_detail=sres.detail,
                        )
                        live.finish(rid)
                        return _safety_response(
                            protocol, model.name, reply, rid, stream,
                            safety.RESULT_BLOCKED_INPUT,
                        )
                    if sres.action == safety.ACTION_SANITIZE:
                        # 脱敏后继续：把请求体里的命中片段替换为掩码再发往上游
                        internal = safety.sanitize_obj(internal, sres.hit_terms)
                        safety.record_review(
                            db, request_id=rid, key=api_key, policy=safety_policy,
                            direction=safety.DIRECTION_INPUT, action="sanitized",
                            result=sres, model=model.name, endpoint=(
                                "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                            ),
                        )
                        input_safety_result = safety.RESULT_SANITIZED_INPUT
                        input_safety_detail = sres.detail
                    elif sres.action == safety.ACTION_REVIEW:
                        safety.record_review(
                            db, request_id=rid, key=api_key, policy=safety_policy,
                            direction=safety.DIRECTION_INPUT, action="review",
                            result=sres, model=model.name, endpoint=(
                                "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                            ),
                        )
                        input_safety_result = safety.RESULT_REVIEW_INPUT
                        input_safety_detail = sres.detail
                    else:
                        input_safety_result = safety.RESULT_PASSED
                else:
                    input_safety_result = safety.RESULT_SKIPPED
            except Exception as e:  # noqa: BLE001
                logger.exception("输入安全审查异常 rid=%s（已忽略，不阻断请求）", rid)
                input_safety_result = safety.RESULT_ERROR
                input_safety_detail = f"审查异常：{type(e).__name__}: {e}"[:500]
                if safety_policy.fail_closed:
                    reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                    err_res = safety.ReviewResult(error=input_safety_detail)
                    safety.record_review(
                        db, request_id=rid, key=api_key, policy=safety_policy,
                        direction=safety.DIRECTION_INPUT, action="error", result=err_res,
                        model=model.name, endpoint=(
                            "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                        ),
                    )
                    _log(
                        db, key=api_key, provider=provider, model=model, protocol=protocol,
                        tokens_in=0, tokens_out=0, cost=0.0, ok=True, error_message="",
                        latency_ms=int((time.time() - start) * 1000), client_ip=client_ip,
                        http_status=200, request_id=rid, request_body=req_text,
                        response_body=reply, safety_result=safety.RESULT_ERROR,
                        safety_policy=safety_policy.name, safety_detail=err_res.detail,
                    )
                    live.finish(rid)
                    return _safety_response(
                        protocol, model.name, reply, rid, stream, safety.RESULT_ERROR,
                    )

        url, headers, outbound_body, upstream_protocol = dispatch.build_outbound(
            provider, model, internal
        )

        live.update(
            rid,
            model=model.name if model else model_name,
            provider_name=provider.name if provider else "",
            provider_type=provider.type if provider else "",
            stage="connecting",
            stream=stream,
        )

        if stream:
            resp = await _stream_response(
                api_key, provider, model, protocol, upstream_protocol,
                url, headers, outbound_body, client_ip, start, rid, req_text,
                safety_policy=safety_policy, input_safety_result=input_safety_result,
                input_safety_detail=input_safety_detail,
            )
            # 只有真的交出了 StreamingResponse，摘除责任才转移给 wrapped() 的 finally。
            # 上游非 2xx / 连接失败时这里返回的是普通 JSONResponse，摘除仍归 handle_chat。
            if isinstance(resp, StreamingResponse):
                stream_handed_off = True
            return resp
        return await _nonstream_response(
            api_key, db, provider, model, protocol, upstream_protocol,
            url, headers, outbound_body, client_ip, start, rid, req_text,
            safety_policy=safety_policy, input_safety_result=input_safety_result,
            input_safety_detail=input_safety_detail,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("LLM 转发未预期异常 rid=%s", rid)
        return fail(error_type=ERROR_INTERNAL,
                    message=f"网关内部错误: {type(e).__name__}: {e}"[:500], status_code=500,
                    provider=provider, model=model, model_name=model_name,
                    latency_ms=int((time.time() - start) * 1000))
    finally:
        if not stream_handed_off:
            live.finish(rid)
        db.close()


async def _nonstream_response(
    api_key, db, provider, model, protocol, upstream_protocol,
    url, headers, outbound_body, client_ip, start, request_id, req_text="",
    safety_policy=None, input_safety_result="", input_safety_detail="",
) -> JSONResponse:
    timeout_s = resolve_timeout(provider)
    try:
        resp = await get_client().post(
            url, headers=headers, json=outbound_body, timeout=httpx_timeout(provider)
        )
    except httpx.HTTPError as e:
        err_type, desc = _classify_exc(e, timeout_s)
        return _fail(db, key=api_key, protocol=protocol, error_type=err_type,
                     message=desc, status_code=502, provider=provider, model=model,
                     latency_ms=int((time.time() - start) * 1000), upstream_url=url,
                     client_ip=client_ip, request_id=request_id, request_body=req_text)

    latency = int((time.time() - start) * 1000)
    up_text = _redact(resp.text)
    live.update(request_id, stage="writing", upstream_status=resp.status_code)

    if resp.status_code >= 300:
        body = up_text[:2000]
        return _fail(db, key=api_key, protocol=protocol, error_type=ERROR_UPSTREAM_STATUS,
                     message=f"上游返回错误 {resp.status_code}: {body[:500]}",
                     status_code=client_status(resp.status_code),
                     provider=provider, model=model, latency_ms=latency,
                     upstream_status=resp.status_code, upstream_body=body,
                     upstream_url=url, client_ip=client_ip, request_id=request_id,
                     request_body=req_text, response_body=up_text)

    try:
        up = resp.json()
    except Exception:
        body = up_text[:2000]
        return _fail(db, key=api_key, protocol=protocol, error_type=ERROR_UPSTREAM_NON_JSON,
                     message="上游返回非 JSON 内容", status_code=502,
                     provider=provider, model=model, latency_ms=latency,
                     upstream_status=resp.status_code, upstream_body=body,
                     upstream_url=url, client_ip=client_ip, request_id=request_id,
                     request_body=req_text, response_body=up_text)

    usage = up.get("usage") or {}
    if upstream_protocol == "anthropic":
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
    else:
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
    cost = _compute_price(model, tokens_in, tokens_out)

    # ---- 输出审查：在协议转换、返回客户端之前完成 ----
    safety_result = input_safety_result
    safety_detail = input_safety_detail
    if safety_policy is not None and safety_policy.output_enabled:
        try:
            sres = safety.review_texts(
                safety.extract_response_texts(up, upstream_protocol),
                safety_policy, safety.DIRECTION_OUTPUT, db=db,
            )
            if sres.blocked:
                reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                safety.record_review(
                    db, request_id=request_id, key=api_key, policy=safety_policy,
                    direction=safety.DIRECTION_OUTPUT, action="blocked", result=sres,
                    model=model.name, endpoint=(
                        "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                    ),
                )
                _log(
                    db, key=api_key, provider=provider, model=model, protocol=protocol,
                    tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, ok=True,
                    error_message="", latency_ms=latency, client_ip=client_ip,
                    http_status=200, upstream_status=resp.status_code,
                    upstream_body=limit_text(up_text), upstream_url=url,
                    request_id=request_id, request_body=req_text, response_body=reply,
                    safety_result=safety.RESULT_BLOCKED_OUTPUT,
                    safety_policy=safety_policy.name, safety_detail=sres.detail,
                )
                return JSONResponse(
                    status_code=200,
                    content=safety.blocked_response(
                        protocol, model.name, reply, request_id,
                        {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                    ),
                    headers={
                        "X-Request-Id": request_id,
                        "X-Safety-Review": safety.RESULT_BLOCKED_OUTPUT,
                    },
                )
            if sres.action == safety.ACTION_SANITIZE:
                # 脱敏后放行：把上游响应里的命中片段替换为掩码再返回客户端
                up = safety.sanitize_obj(up, sres.hit_terms)
                up_text = json.dumps(up, ensure_ascii=False)
                safety.record_review(
                    db, request_id=request_id, key=api_key, policy=safety_policy,
                    direction=safety.DIRECTION_OUTPUT, action="sanitized", result=sres,
                    model=model.name, endpoint=(
                        "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                    ),
                )
                safety_result = safety.RESULT_SANITIZED_OUTPUT
                safety_detail = sres.detail
            elif sres.action == safety.ACTION_REVIEW:
                safety.record_review(
                    db, request_id=request_id, key=api_key, policy=safety_policy,
                    direction=safety.DIRECTION_OUTPUT, action="review", result=sres,
                    model=model.name, endpoint=(
                        "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                    ),
                )
                safety_result = safety.RESULT_REVIEW_OUTPUT
                safety_detail = sres.detail
            elif safety_result in ("", safety.RESULT_PASSED, safety.RESULT_SKIPPED):
                # 不把输入阶段已记录的「脱敏/复核」降级成 passed
                safety_result = safety.RESULT_PASSED
        except Exception as e:  # noqa: BLE001
            logger.exception("输出安全审查异常 rid=%s（已忽略）", request_id)
            safety_result = safety.RESULT_ERROR
            safety_detail = f"审查异常：{type(e).__name__}: {e}"[:500]
            if safety_policy.fail_closed:
                reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                err_res = safety.ReviewResult(error=safety_detail)
                safety.record_review(
                    db, request_id=request_id, key=api_key, policy=safety_policy,
                    direction=safety.DIRECTION_OUTPUT, action="error", result=err_res,
                    model=model.name, endpoint=(
                        "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                    ),
                )
                _log(
                    db, key=api_key, provider=provider, model=model, protocol=protocol,
                    tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, ok=True,
                    error_message="", latency_ms=latency, client_ip=client_ip,
                    http_status=200, upstream_status=resp.status_code,
                    upstream_body=limit_text(up_text), upstream_url=url,
                    request_id=request_id, request_body=req_text, response_body=reply,
                    safety_result=safety.RESULT_ERROR,
                    safety_policy=safety_policy.name, safety_detail=safety_detail,
                )
                return JSONResponse(
                    status_code=200,
                    content=safety.blocked_response(
                        protocol, model.name, reply, request_id,
                        {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                    ),
                    headers={"X-Request-Id": request_id, "X-Safety-Review": safety.RESULT_ERROR},
                )

    if protocol == upstream_protocol:
        client_resp = up
    elif protocol == "openai" and upstream_protocol == "anthropic":
        client_resp = anthropic_to_openai_response(up)
    elif protocol == "anthropic" and upstream_protocol in ("openai", "generic"):
        client_resp = openai_to_anthropic_response(up)
    else:
        client_resp = up

    _log(db, key=api_key, provider=provider, model=model, protocol=protocol,
         tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, ok=True,
         error_message="", latency_ms=latency, client_ip=client_ip,
         http_status=200, upstream_status=resp.status_code, upstream_url=url,
         request_id=request_id, request_body=req_text,
         response_body=limit_text(up_text),
         safety_result=safety_result,
         safety_policy=(safety_policy.name if safety_policy is not None else ""),
         safety_detail=safety_detail)
    headers_out = {"X-Request-Id": request_id}
    if safety_result:
        headers_out["X-Safety-Review"] = safety_result
    return JSONResponse(status_code=200, content=client_resp,
                        headers=headers_out)


async def _stream_response(
    api_key, provider, model, protocol, upstream_protocol,
    url, headers, outbound_body, client_ip, start, request_id, req_text="",
    safety_policy=None, input_safety_result="", input_safety_detail="",
):
    client = get_client()
    timeout_s = resolve_timeout(provider)
    # 按厂商设超时：非流式看整段响应，流式看「两个 chunk 之间的间隔」
    req = client.build_request(
        "POST", url, headers=headers, json=outbound_body, timeout=httpx_timeout(provider)
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        err_type, desc = _classify_exc(e, timeout_s)
        db = SessionLocal()
        try:
            return _fail(db, key=api_key, protocol=protocol, error_type=err_type,
                         message=desc, status_code=502, provider=provider, model=model,
                         latency_ms=int((time.time() - start) * 1000), upstream_url=url,
                         client_ip=client_ip, request_id=request_id,
                         request_body=req_text)
        finally:
            db.close()

    upstream_status = upstream.status_code
    upstream_ctype = (upstream.headers.get("content-type") or "").lower()
    latency_first = int((time.time() - start) * 1000)
    live.update(request_id, upstream_status=upstream_status)

    # ⚠️ 上游直接拒绝时必须在此拦截。否则拿到的是 JSON 错误体，
    #    下面 aiter_lines() 的每行都不以 "data:" 开头而被跳过，
    #    客户端会收到 HTTP 200 + 空 SSE 流，日志还会记成成功。
    if upstream_status >= 300:
        try:
            raw = await upstream.aread()
            up_text = _redact(raw.decode("utf-8", "replace"))
            body = up_text[:2000]
        except Exception as e:  # noqa: BLE001
            body = f"(读取上游响应失败: {type(e).__name__})"
            up_text = body
        finally:
            await upstream.aclose()
        db = SessionLocal()
        try:
            return _fail(db, key=api_key, protocol=protocol, error_type=ERROR_UPSTREAM_STATUS,
                         message=f"上游返回错误 {upstream_status}: {body[:500]}",
                         status_code=client_status(upstream_status),
                         provider=provider, model=model,
                         latency_ms=latency_first, upstream_status=upstream_status,
                         upstream_body=body, upstream_url=url,
                         client_ip=client_ip, request_id=request_id,
                         request_body=req_text, response_body=limit_text(up_text))
        finally:
            db.close()

    tokens_in = tokens_out = 0
    ok = True
    error_message = ""
    error_type = ""
    error_body = ""
    done = False
    saw_data = False
    # 累积上游 SSE 原文供详情查看；超过上限后自动停止拼接，不影响转发速度
    collector = BodyCollector()

    # 输出审查：词表非空且规则开启输出方向时才缓冲整段流；否则保持原样直通。
    review_active = bool(safety_policy is not None and safety_policy.output_enabled)
    buffered: list[str] = []
    assistant_text: list[str] = []
    buffered_len = 0
    review_overflow = False
    # 单次流式输出最多缓冲 200 万字符；超过后不再持有原文，直接按超限拦截，
    # 避免异常长流把进程内存吃满，也避免「无法完整审查却继续转发」。
    max_review_buffer = 2_000_000
    safety_result = input_safety_result
    safety_detail = input_safety_detail
    blocked_reply = ""

    def _mark_error(etype: str, msg: str, body: str = "") -> None:
        nonlocal ok, error_message, error_type, error_body
        if ok:  # 只保留第一个错误，避免被后续噪声覆盖
            ok = False
            error_type = etype
            error_message = msg
            error_body = body

    async def event_gen():
        nonlocal tokens_in, tokens_out, done, saw_data
        nonlocal safety_result, safety_detail, blocked_reply, review_active
        nonlocal buffered_len, review_overflow

        def _buf(s: str) -> None:
            nonlocal buffered_len, review_overflow
            if not review_overflow and buffered_len + len(s) > max_review_buffer:
                review_overflow = True
                buffered.clear()
                return
            if not review_overflow:
                buffered.append(s)
                buffered_len += len(s)

        def _text(parts: list[str]) -> None:
            nonlocal buffered_len, review_overflow
            for t in parts:
                if not t:
                    continue
                if not review_overflow and buffered_len + len(t) > max_review_buffer:
                    review_overflow = True
                    assistant_text.clear()
                    continue
                if not review_overflow:
                    assistant_text.append(t)

        live.update(request_id, stage="streaming")
        try:
            if protocol == upstream_protocol:
                # 直通：原样转发 SSE，同时解析 usage 记账
                async for line in upstream.aiter_lines():
                    collector.add(line + "\n")
                    s = _format_line(line)
                    if review_active:
                        _buf(s)
                    else:
                        yield s
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    saw_data = True
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and (obj.get("error") or obj.get("type") == "error"):
                        detail = _redact(json.dumps(obj.get("error") or obj, ensure_ascii=False))
                        _mark_error(ERROR_UPSTREAM_STREAM,
                                    f"上游流内返回错误: {detail[:500]}", detail[:2000])
                        continue
                    _text(safety.extract_stream_delta(obj, upstream_protocol))
                    if upstream_protocol == "anthropic" and obj.get("type") == "message_delta":
                        u = obj.get("usage") or {}
                        tokens_in = int(u.get("input_tokens") or tokens_in or 0)
                        tokens_out = int(u.get("output_tokens") or tokens_out or 0)
                    elif obj.get("usage"):
                        u = obj["usage"]
                        tokens_in = int(u.get("prompt_tokens") or 0)
                        tokens_out = int(u.get("completion_tokens") or 0)
            elif upstream_protocol in ("openai", "generic"):
                state: dict = {}
                async for line in upstream.aiter_lines():
                    collector.add(line + "\n")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        continue
                    saw_data = True
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(chunk, dict) and chunk.get("error"):
                        detail = _redact(json.dumps(chunk, ensure_ascii=False))
                        _mark_error(ERROR_UPSTREAM_STREAM,
                                    f"上游流内返回错误: {detail[:500]}", detail[:2000])
                        continue
                    _text(safety.extract_stream_delta(chunk, "openai"))
                    events, usage = openai_chunk_to_anthropic(chunk, state)
                    if usage:
                        tokens_in = usage["prompt_tokens"]
                        tokens_out = usage["completion_tokens"]
                    for ev in events:
                        s = _format_anthropic_event(ev)
                        if review_active:
                            _buf(s)
                        else:
                            yield s
                if not done:
                    s = _format_anthropic_event({"type": "message_stop"})
                    if review_active:
                        _buf(s)
                    else:
                        yield s
            else:  # 上游 anthropic -> 下游 openai
                state = {}
                async for line in upstream.aiter_lines():
                    collector.add(line + "\n")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    saw_data = True
                    try:
                        event = json.loads(payload)
                    except Exception:
                        continue
                    if isinstance(event, dict) and event.get("type") == "error":
                        detail = _redact(json.dumps(event.get("error") or event, ensure_ascii=False))
                        _mark_error(ERROR_UPSTREAM_STREAM,
                                    f"上游流内返回错误: {detail[:500]}", detail[:2000])
                        continue
                    _text(safety.extract_stream_delta(event, "anthropic"))
                    chunks, usage = anthropic_event_to_openai(event, state)
                    if usage:
                        tokens_in = usage["prompt_tokens"]
                        tokens_out = usage["completion_tokens"]
                    for ch in chunks:
                        s = _format_openai_chunk(ch)
                        if review_active:
                            _buf(s)
                        else:
                            yield s

            # ---- 输出审查：整段缓冲后统一判定，命中则丢弃原文改发默认回复 ----
            if review_active:
                if not ok:
                    # 上游已报错，优先保留错误语义；把已生成内容原样送出后由 finalize 记失败
                    for s in buffered:
                        yield s
                    buffered.clear()
                    review_active = False
                    if safety_result == safety.RESULT_PASSED:
                        safety_result = safety.RESULT_SKIPPED
                elif review_overflow:
                    # 输出超过缓冲上限：无法完整审查，按超限拦截并记录
                    blocked_reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                    safety_result = safety.RESULT_BLOCKED_OVERFLOW
                    safety_detail = f"输出超过审查缓冲上限 {max_review_buffer} 字符，按超限拦截"
                    sdb = SessionLocal()
                    try:
                        safety.record_review(
                            sdb, request_id=request_id, key=api_key, policy=safety_policy,
                            direction=safety.DIRECTION_OUTPUT, action="overflow",
                            result=safety.ReviewResult(error=safety_detail),
                            model=model.name,
                            endpoint=("/v1/messages" if protocol == "anthropic"
                                      else "/v1/chat/completions"),
                            is_stream=True,
                        )
                        sdb.commit()
                    except Exception:  # noqa: BLE001
                        sdb.rollback()
                        logger.exception("写入超限审查记录失败 rid=%s（已忽略）", request_id)
                    finally:
                        sdb.close()
                    buffered.clear()
                    saw_data = True
                    for s in safety.blocked_stream(
                        protocol, model.name, blocked_reply, request_id,
                        {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                    ):
                        yield s
                elif not saw_data:
                    # 200 但没有任何 data 事件，交给 finalize 记 empty_stream
                    buffered.clear()
                    review_active = False
                else:
                    sdb = SessionLocal()
                    try:
                        sres = safety.review_texts(
                            ["".join(assistant_text)], safety_policy,
                            safety.DIRECTION_OUTPUT, db=sdb,
                        )
                        if sres.blocked:
                            blocked_reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                            safety_result = safety.RESULT_BLOCKED_OUTPUT
                            safety_detail = sres.detail
                            safety.record_review(
                                sdb, request_id=request_id, key=api_key, policy=safety_policy,
                                direction=safety.DIRECTION_OUTPUT, action="blocked",
                                result=sres, model=model.name,
                                endpoint=("/v1/messages" if protocol == "anthropic"
                                          else "/v1/chat/completions"),
                                is_stream=True,
                            )
                            sdb.commit()
                            buffered.clear()
                            saw_data = True
                            for s in safety.blocked_stream(
                                protocol, model.name, blocked_reply, request_id,
                                {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                            ):
                                yield s
                        elif sres.action == safety.ACTION_SANITIZE:
                            # 流式脱敏：已发出的增量无法回收，改为丢弃原缓冲，
                            # 用脱敏后的整段文本重发一个合法 SSE 流。
                            sanitized_text = safety.sanitize_obj(
                                "".join(assistant_text), sres.hit_terms
                            )
                            safety_result = safety.RESULT_SANITIZED_OUTPUT
                            safety_detail = sres.detail
                            safety.record_review(
                                sdb, request_id=request_id, key=api_key, policy=safety_policy,
                                direction=safety.DIRECTION_OUTPUT, action="sanitized",
                                result=sres, model=model.name,
                                endpoint=("/v1/messages" if protocol == "anthropic"
                                          else "/v1/chat/completions"),
                                is_stream=True,
                            )
                            sdb.commit()
                            buffered.clear()
                            saw_data = True
                            # 复用默认回复的 SSE 结构承载脱敏文本（仅内容不同）
                            for s in safety.blocked_stream(
                                protocol, model.name, sanitized_text, request_id,
                                {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                            ):
                                yield s
                        elif sres.action == safety.ACTION_REVIEW:
                            safety_result = safety.RESULT_REVIEW_OUTPUT
                            safety_detail = sres.detail
                            safety.record_review(
                                sdb, request_id=request_id, key=api_key, policy=safety_policy,
                                direction=safety.DIRECTION_OUTPUT, action="review",
                                result=sres, model=model.name,
                                endpoint=("/v1/messages" if protocol == "anthropic"
                                          else "/v1/chat/completions"),
                                is_stream=True,
                            )
                            sdb.commit()
                            for s in buffered:
                                yield s
                            buffered.clear()
                        else:
                            if safety_result in ("", safety.RESULT_PASSED, safety.RESULT_SKIPPED):
                                # 不把输入阶段的「脱敏/复核」标记降级成 passed
                                safety_result = safety.RESULT_PASSED
                            for s in buffered:
                                yield s
                            buffered.clear()
                    except Exception as e:  # noqa: BLE001
                        logger.exception("流式输出审查异常 rid=%s（已忽略）", request_id)
                        safety_result = safety.RESULT_ERROR
                        safety_detail = f"审查异常：{type(e).__name__}: {e}"[:500]
                        if safety_policy.fail_closed:
                            blocked_reply = safety_policy.default_reply or safety.DEFAULT_REPLY
                            err_res = safety.ReviewResult(error=safety_detail)
                            try:
                                safety.record_review(
                                    sdb, request_id=request_id, key=api_key, policy=safety_policy,
                                    direction=safety.DIRECTION_OUTPUT, action="error",
                                    result=err_res, model=model.name,
                                    endpoint=("/v1/messages" if protocol == "anthropic"
                                              else "/v1/chat/completions"),
                                    is_stream=True,
                                )
                                sdb.commit()
                            except Exception:  # noqa: BLE001
                                sdb.rollback()
                            buffered.clear()
                            saw_data = True
                            for s in safety.blocked_stream(
                                protocol, model.name, blocked_reply, request_id,
                                {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
                            ):
                                yield s
                        else:
                            for s in buffered:
                                yield s
                            buffered.clear()
                    finally:
                        sdb.close()
        except Exception as e:  # noqa: BLE001
            et, desc = _classify_exc(e, timeout_s)
            _mark_error(et if et != ERROR_UPSTREAM_CONNECT else ERROR_STREAM_INTERRUPTED,
                        f"流式传输中断: {desc}: {e}"[:500])
            if review_active:
                for s in buffered:
                    yield s
                buffered.clear()
                review_active = False
                if safety_result == safety.RESULT_PASSED:
                    safety_result = safety.RESULT_SKIPPED
            # 按入站协议发终止事件，让客户端能正常收尾（真实原因已在日志里）
            if protocol == "anthropic":
                yield _format_anthropic_event({"type": "message_stop"})
            else:
                yield _format_openai_chunk(
                    {"id": "", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                )
        finally:
            await upstream.aclose()

    async def finalize():
        latency = int((time.time() - start) * 1000)
        cost = _compute_price(model, tokens_in, tokens_out)
        # 200 但一个 data 事件都没有：上游忽略 stream、协议不匹配或静默失败
        failed = (not ok) or (not saw_data)
        et = error_type or (ERROR_EMPTY_STREAM if failed else "")
        em = error_message or (
            f"上游返回 200 但未产生任何 data 事件（content-type={upstream_ctype or '-'}）；"
            "常见原因：上游忽略 stream 参数、协议不匹配或静默失败"
            if failed else ""
        )
        db2 = SessionLocal()
        try:
            upstream_text = collector.text()
            _log(db2, key=api_key, provider=provider, model=model, protocol=protocol,
                 tokens_in=tokens_in, tokens_out=tokens_out, cost=cost, ok=not failed,
                 error_message=em, latency_ms=latency, client_ip=client_ip,
                 http_status=200 if not failed else 502,
                 error_type=et, upstream_status=upstream_status,
                 upstream_body=(error_body or (limit_text(upstream_text) if blocked_reply else "")),
                 upstream_url=url, request_id=request_id,
                 request_body=req_text,
                 response_body=(blocked_reply or upstream_text),
                 safety_result=safety_result,
                 safety_policy=(safety_policy.name if safety_policy is not None else ""),
                 safety_detail=safety_detail)
        finally:
            db2.close()

    async def wrapped():
        try:
            async for chunk in event_gen():
                yield chunk
            await finalize()
        finally:
            # 客户端提前断开时也要把实时条目摘掉，否则会一直挂在「实时监控」里
            live.finish(request_id)

    return StreamingResponse(
        wrapped(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


def _format_line(line: str) -> str:
    if line == "":
        return "\n"
    return line + "\n\n" if not line.endswith("\n\n") else line


def _format_openai_chunk(chunk: dict) -> str:
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


def _format_anthropic_event(event: dict) -> str:
    return (
        "event: " + event.get("type", "message") + "\n"
        + "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    )
