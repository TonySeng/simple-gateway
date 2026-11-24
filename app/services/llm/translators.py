"""Protocol translators between OpenAI Chat Completions and Anthropic Messages.

The gateway's *internal* canonical format is the OpenAI chat-completions request.
Inbound Anthropic requests are converted to OpenAI; outbound requests to an
Anthropic-type provider are converted back. Streaming SSE is translated chunk-by-chunk.
"""
from __future__ import annotations

import time
from typing import Any

# ---------- helpers ----------
def _join_text(blocks: Any) -> str:
    """Join a list of Anthropic content blocks (or OpenAI content parts) into text."""
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image_url":
                # keep as a placeholder; images handled separately
                parts.append("[image]")
    return "\n".join(p for p in parts if p)


def _map_finish_to_anthropic(finish: str | None) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "content_filter",
    }.get(finish or "", "end_turn")


def _map_stop_to_openai(stop: str | None) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop or "", "stop")


# ---------- request conversion ----------
def anthropic_to_openai_request(req: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages request -> OpenAI chat-completions request."""
    messages: list[dict[str, Any]] = []
    system = req.get("system")
    if system:
        messages.append({"role": "system", "content": _join_text(system)})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            oai_content: list[dict[str, Any]] = []
            for block in content:
                if block.get("type") == "text":
                    oai_content.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        oai_content.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                                },
                            }
                        )
                    elif src.get("type") == "url":
                        oai_content.append({"type": "image_url", "image_url": {"url": src.get("url", "")}})
            if len(oai_content) == 1 and oai_content[0]["type"] == "text":
                messages.append({"role": role, "content": oai_content[0]["text"]})
            else:
                messages.append({"role": role, "content": oai_content})
        else:
            messages.append({"role": role, "content": str(content)})

    out: dict[str, Any] = {
        "model": req.get("model"),
        "messages": messages,
        "max_tokens": req.get("max_tokens"),
        "temperature": req.get("temperature"),
        "top_p": req.get("top_p"),
        "stop": req.get("stop_sequences"),
        "stream": req.get("stream", False),
    }
    return {k: v for k, v in out.items() if v is not None}


def openai_to_anthropic_request(req: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completions request -> Anthropic Messages request."""
    anth_messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            system_parts.append(_join_text(content))
            continue
        a_role = "assistant" if role == "assistant" else "user"
        if isinstance(content, str):
            anth_messages.append({"role": a_role, "content": content})
        elif isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for part in content:
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        header, data = url.split(",", 1)
                        media_type = header.split(";")[0].split(":")[1]
                        blocks.append(
                            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
                        )
                    else:
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            anth_messages.append({"role": a_role, "content": blocks})
        else:
            anth_messages.append({"role": a_role, "content": str(content)})

    out: dict[str, Any] = {
        "model": req.get("model"),
        "messages": anth_messages,
        "max_tokens": req.get("max_tokens", 4096),
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if "temperature" in req:
        out["temperature"] = req["temperature"]
    if "top_p" in req:
        out["top_p"] = req["top_p"]
    if "stop" in req:
        out["stop_sequences"] = req["stop"]
    if req.get("stream"):
        out["stream"] = True
    return out


# ---------- response conversion (non-streaming) ----------
def openai_to_anthropic_response(resp: dict[str, Any]) -> dict[str, Any]:
    choices = resp.get("choices", []) or []
    content = ""
    finish = None
    if choices:
        message = choices[0].get("message", {}) or {}
        content = message.get("content") or ""
        finish = choices[0].get("finish_reason")
    usage = resp.get("usage") or {}
    return {
        "id": resp.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": resp.get("model", ""),
        "content": [{"type": "text", "text": content or ""}],
        "stop_reason": _map_finish_to_anthropic(finish),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def anthropic_to_openai_response(resp: dict[str, Any]) -> dict[str, Any]:
    usage = resp.get("usage") or {}
    content = ""
    for block in resp.get("content", []) or []:
        if block.get("type") == "text":
            content += block.get("text", "")
    return {
        "id": resp.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": _map_stop_to_openai(resp.get("stop_reason")),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


# ---------- streaming SSE translation ----------
def openai_chunk_to_anthropic(chunk: dict[str, Any], state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict | None]:
    """Convert one parsed OpenAI streaming chunk into Anthropic SSE events.

    Returns (events, usage). `usage` is non-None only on the terminal chunk.
    """
    events: list[dict[str, Any]] = []
    usage: dict | None = None

    if not state.get("started"):
        state["started"] = True
        state["id"] = chunk.get("id", f"msg_{int(time.time())}")
        events.append(
            {
                "type": "message_start",
                "message": {
                    "id": state["id"],
                    "type": "message",
                    "role": "assistant",
                    "model": chunk.get("model", state.get("model", "")),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }
        )
        events.append({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})

    if "model" in chunk:
        state["model"] = chunk["model"]

    for choice in chunk.get("choices", []) or []:
        delta = choice.get("delta", {}) or {}
        if delta.get("content") is not None:
            events.append(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                }
            )
        finish = choice.get("finish_reason")
        if finish:
            state["finish"] = finish

    if chunk.get("usage"):
        u = chunk["usage"]
        events.append({"type": "content_block_stop", "index": 0})
        events.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": _map_finish_to_anthropic(state.get("finish")), "stop_sequence": None},
                "usage": {"output_tokens": u.get("completion_tokens", 0)},
            }
        )
        events.append({"type": "message_stop"})
        usage = {
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
        }

    return events, usage


def anthropic_event_to_openai(event: dict[str, Any], state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict | None]:
    """Convert one parsed Anthropic SSE event into OpenAI streaming chunks.

    Returns (chunks, usage). `usage` is non-None only on the terminal event.
    """
    chunks: list[dict[str, Any]] = []
    usage: dict | None = None
    etype = event.get("type")

    if etype == "message_start":
        msg = event.get("message", {})
        state["id"] = msg.get("id", f"chatcmpl-{int(time.time())}")
        state["model"] = msg.get("model", "")
        chunks.append(
            {
                "id": state["id"],
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": state["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
    elif etype == "content_block_start":
        pass
    elif etype == "content_block_delta":
        delta = event.get("delta", {})
        if delta.get("type") == "text_delta":
            chunks.append(
                {
                    "id": state.get("id", ""),
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": state.get("model", ""),
                    "choices": [{"index": 0, "delta": {"content": delta.get("text", "")}, "finish_reason": None}],
                }
            )
    elif etype == "message_delta":
        d = event.get("delta", {})
        u = event.get("usage", {})
        chunks.append(
            {
                "id": state.get("id", ""),
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": state.get("model", ""),
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": _map_stop_to_openai(d.get("stop_reason"))}
                ],
            }
        )
        usage = {
            "prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0),
        }
    elif etype == "message_stop":
        pass

    return chunks, usage
