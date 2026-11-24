"""LLM 对外接口：OpenAI 兼容 + Anthropic 兼容（含流式）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import api_key_auth, get_client_ip
from app.core.models import ApiKey, Model, Provider
from app.services.llm import proxy

router = APIRouter(tags=["LLM"])


@router.post("/v1/chat/completions", summary="对话补全（OpenAI 兼容）")
@router.post("/chat/completions", include_in_schema=False)
async def chat_completions(
    request: Request,
    key: ApiKey = Depends(api_key_auth("llm")),
):
    return await proxy.handle_chat("openai", key, request, get_client_ip(request))


@router.post("/v1/messages", summary="消息接口（Anthropic 兼容）")
@router.post("/messages", include_in_schema=False)
async def anthropic_messages(
    request: Request,
    key: ApiKey = Depends(api_key_auth("llm")),
):
    return await proxy.handle_chat("anthropic", key, request, get_client_ip(request))


def _estimate_tokens(body: dict) -> int:
    """粗略估算 input token 数（无第三方依赖）。

    Claude Code 等客户端会调 count_tokens 做上下文预算，只要量级正确即可：
    CJK/全角字符按 1 token，其余按 4 字符 ≈ 1 token。
    """
    pieces: list[str] = []

    sys_val = body.get("system")
    if isinstance(sys_val, str):
        pieces.append(sys_val)
    elif isinstance(sys_val, list):
        for part in sys_val:
            if isinstance(part, dict):
                pieces.append(str(part.get("text") or ""))

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    pieces.append(str(part.get("text") or ""))
                elif ptype == "tool_use":
                    pieces.append(json.dumps(part.get("input") or {}, ensure_ascii=False))
                elif ptype == "tool_result":
                    inner = part.get("content")
                    if isinstance(inner, str):
                        pieces.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                pieces.append(str(sub.get("text") or ""))

    total = 0
    for piece in pieces:
        wide = sum(1 for ch in piece if ord(ch) > 0x2E80)
        total += wide + (len(piece) - wide + 3) // 4
    return max(total, 1)


@router.post("/v1/messages/count_tokens", summary="预估输入 token 数（Anthropic 兼容）")
@router.post("/messages/count_tokens", include_in_schema=False)
async def count_tokens(
    request: Request,
    key: ApiKey = Depends(api_key_auth("llm")),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return {"input_tokens": _estimate_tokens(body)}


@router.get("/v1/models", summary="可用模型列表（OpenAI 风格）")
@router.get("/models", include_in_schema=False)
def list_models(
    key: ApiKey = Depends(api_key_auth("llm")),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Model)
        .join(Provider, Provider.id == Model.provider_id)
        .filter(Model.is_active.is_(True), Provider.is_enabled.is_(True), Provider.service == "llm")
        .order_by(Model.name)
        .all()
    )
    return {
        "object": "list",
        "data": [
            {
                "id": m.name,
                "object": "model",
                "created": int(m.created_at.timestamp()) if m.created_at else 0,
                "owned_by": m.provider.name if m.provider else "",
                "context_window": m.context_window or 0,
            }
            for m in rows
        ],
    }
