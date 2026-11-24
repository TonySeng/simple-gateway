"""LLM 代理安全审查引擎。

职责
----
* 解析某个 API Key 绑定的审查规则（未绑定时回落到全局默认规则）；
* 把一个规则引用的多个敏感词库编译成可复用的匹配器（带短 TTL 缓存）；
* 对请求/响应做文本抽取、归一化、关键词/正则匹配；
* 命中时生成与入站协议一致的默认回复（普通 JSON 或 SSE）；
* 把命中详情写入独立的 ``safety_reviews`` 表。

设计要点
--------
* 审查只做确定性的文本匹配，不调用外部模型，避免把可用性绑到第三方。
* 词库以多行文本存储，``re:`` 前缀表示正则；``#`` 开头为注释。
* 默认回复走正常 200 响应，不返回 403：调用方无需为「被审查」写额外异常分支。
* 输出审查在流式场景必须整段缓冲，否则已经发出去的 token 无法撤回。
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.models import ApiKey, SafetyLibrary, SafetyPolicy, SafetyReview
from app.services.llm.translators import (
    openai_chunk_to_anthropic,
    openai_to_anthropic_response,
)

logger = logging.getLogger("gateway.safety")

DEFAULT_REPLY = "抱歉，该内容未通过安全审查，无法为您生成回复。"

RESULT_PASSED = "passed"
RESULT_BLOCKED_INPUT = "blocked_input"
RESULT_BLOCKED_OUTPUT = "blocked_output"
RESULT_BLOCKED_OVERFLOW = "blocked_overflow"
RESULT_ERROR = "error"
RESULT_SKIPPED = "skipped"

DIRECTION_INPUT = "input"
DIRECTION_OUTPUT = "output"

# 词库/规则的编译缓存：后台保存时主动失效；TTL 只作为多进程场景的兜底。
_CACHE_TTL_S = 5.0
_MAX_REGEX_LEN = 500
_term_cache: dict[str, tuple[float, list["CompiledTerm"], list[str]]] = {}

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
_WS = re.compile(r"\s+")


@dataclass
class CompiledTerm:
    term: str
    library: str
    pattern: re.Pattern | None = None


@dataclass
class ReviewResult:
    blocked: bool = False
    matched: list[dict[str, str]] = field(default_factory=list)
    excerpt: str = ""
    truncated: bool = False
    detail: str = ""
    error: str = ""

    @property
    def library_names(self) -> list[str]:
        names: list[str] = []
        for m in self.matched:
            name = m.get("library") or ""
            if name and name not in names:
                names.append(name)
        return names


def invalidate_cache(policy_id: str | None = None) -> None:
    """后台保存词库/规则后调用；不传 id 时清空全部缓存。"""
    if policy_id:
        _term_cache.pop(policy_id, None)
    else:
        _term_cache.clear()


# ---------------------------------------------------------------- 规则解析
def resolve_policy(db: Session, api_key: ApiKey) -> SafetyPolicy | None:
    """确定该 Key 实际使用的审查规则。

    * ``safety_policy_id`` 为 ``None``：使用全局默认规则；
    * 为 ``""``：该 Key 明确关闭审查；
    * 为具体 id：使用该规则；规则被删除/停用且存在默认规则时回落默认，
      避免误删规则把 Key 变成无审查状态。
    """
    sid = getattr(api_key, "safety_policy_id", None)
    if sid == "":
        return None
    if sid:
        policy = (
            db.query(SafetyPolicy)
            .filter(SafetyPolicy.id == sid, SafetyPolicy.is_enabled.is_(True))
            .first()
        )
        if policy is not None:
            return policy
        logger.warning("Key %s 绑定的审查规则 %s 不存在或已停用，回落默认规则", api_key.name, sid)
    return (
        db.query(SafetyPolicy)
        .filter(SafetyPolicy.is_enabled.is_(True), SafetyPolicy.is_default.is_(True))
        .order_by(SafetyPolicy.updated_at.desc())
        .first()
    )


def _compile_library(name: str, terms: str, policy: SafetyPolicy) -> list[CompiledTerm]:
    out: list[CompiledTerm] = []
    for raw in (terms or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line[:3].lower() == "re:":
            pattern_text = line[3:].strip()
            if not pattern_text:
                continue
            if len(pattern_text) > _MAX_REGEX_LEN:
                logger.warning("词库 %s 的正则超过 %s 字符，已跳过", name, _MAX_REGEX_LEN)
                continue
            flags = 0 if policy.case_sensitive else re.IGNORECASE
            try:
                out.append(CompiledTerm(line, name, re.compile(pattern_text, flags)))
            except re.error as e:
                # 坏正则不能拖垮整条规则；后台保存时会先校验，这里只兜底。
                logger.warning("词库 %s 的正则无效，已跳过：%s（%s）", name, pattern_text, e)
            continue
        term = _normalize(line, policy)
        if term:
            out.append(CompiledTerm(term, name))
    return out


def _load_terms(db: Session, policy: SafetyPolicy) -> tuple[list[CompiledTerm], list[str]]:
    cached = _term_cache.get(policy.id)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1], cached[2]

    ids = [x for x in (policy.library_ids or []) if x]
    libs: list[SafetyLibrary] = []
    if ids:
        libs = (
            db.query(SafetyLibrary)
            .filter(SafetyLibrary.id.in_(ids), SafetyLibrary.is_enabled.is_(True))
            .all()
        )
    compiled: list[CompiledTerm] = []
    names: list[str] = []
    for lib in libs:
        names.append(lib.name)
        compiled.extend(_compile_library(lib.name, lib.terms, policy))
    _term_cache[policy.id] = (now + _CACHE_TTL_S, compiled, names)
    return compiled, names


# ---------------------------------------------------------------- 文本处理
def _normalize(text: str, policy: SafetyPolicy) -> str:
    if not policy.normalize:
        return text
    s = unicodedata.normalize("NFKC", text or "")
    s = _ZERO_WIDTH.sub("", s)
    s = _WS.sub(" ", s)
    if not policy.case_sensitive:
        s = s.casefold()
    return s


def _match_one(text: str, term: CompiledTerm, policy: SafetyPolicy) -> bool:
    if term.pattern is not None:
        return term.pattern.search(text) is not None
    if policy.match_mode == "word" and re.fullmatch(r"[A-Za-z0-9_\-]+", term.term):
        # 拉丁词按词边界匹配，避免 "kill" 命中 "skill"；中文仍按子串。
        pat = r"(?<![A-Za-z0-9_])" + re.escape(term.term) + r"(?![A-Za-z0-9_])"
        return re.search(pat, text) is not None
    return term.term in text


def _build_detail(matched: list[dict[str, str]], threshold: int) -> str:
    if not matched:
        return ""
    parts = []
    for m in matched[:10]:
        lib = m.get("library") or ""
        parts.append(f"{m.get('term', '')}（{lib}）" if lib else str(m.get("term", "")))
    more = f" 等 {len(matched)} 项" if len(matched) > 10 else ""
    return f"命中 {len(matched)} 个词，阈值 {threshold}：{'、'.join(parts)}{more}"


def review_texts(
    texts: list[str],
    policy: SafetyPolicy,
    direction: str = DIRECTION_INPUT,
    *,
    db: Session,
) -> ReviewResult:
    """对一组文本做审查；返回是否命中、命中词与原文摘录。"""
    if policy is None or not policy.is_enabled:
        return ReviewResult()
    if direction == DIRECTION_INPUT and not policy.input_enabled:
        return ReviewResult()
    if direction == DIRECTION_OUTPUT and not policy.output_enabled:
        return ReviewResult()

    raw = "\n".join(t for t in texts if t)
    if not raw.strip():
        return ReviewResult()

    limit = max(1000, int(policy.max_text_chars or 200000))
    truncated = len(raw) > limit
    clipped = raw[:limit]
    normalized = _normalize(clipped, policy)

    compiled, _lib_names = _load_terms(db, policy)
    matched: list[dict[str, str]] = []
    seen: set[str] = set()
    for term in compiled:
        if _match_one(normalized, term, policy):
            if term.term in seen:
                continue
            seen.add(term.term)
            matched.append({"term": term.term, "library": term.library})

    threshold = max(1, int(policy.threshold or 1))
    blocked = len(matched) >= threshold
    return ReviewResult(
        blocked=blocked,
        matched=matched,
        excerpt=clipped[:2000],
        truncated=truncated,
        detail=_build_detail(matched, threshold) if blocked else "",
    )


# ---------------------------------------------------------------- 文本抽取
def _content_text(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                out.append(str(part.get("text") or ""))
            elif ptype == "tool_use":
                out.append(json.dumps(part.get("input") or {}, ensure_ascii=False))
            elif ptype == "tool_result":
                out.extend(_content_text(part.get("content")))
            elif ptype == "function_call":
                out.append(str(part.get("arguments") or ""))
    return out


def extract_request_texts(body: dict) -> list[str]:
    """从 canonical OpenAI 请求中抽取所有可能承载文本的字段。

    Anthropic 入站已经在 proxy 里转成 canonical OpenAI，因此这里只需处理
    ``system`` / ``messages`` / tool 调用参数等 OpenAI 形态。
    """
    out: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        out.append(system)
    elif isinstance(system, list):
        out.extend(_content_text(system))

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        out.extend(_content_text(msg.get("content")))
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                out.append(str(fn.get("name") or ""))
                out.append(str(fn.get("arguments") or ""))
        fn = msg.get("function_call")
        if isinstance(fn, dict):
            out.append(str(fn.get("name") or ""))
            out.append(str(fn.get("arguments") or ""))
    return [x for x in out if x]


def extract_response_texts(resp: dict, upstream_protocol: str) -> list[str]:
    """从上游响应中抽取 assistant 文本与工具调用参数。"""
    out: list[str] = []
    if upstream_protocol == "anthropic":
        out.extend(_content_text(resp.get("content")))
        return [x for x in out if x]
    for choice in resp.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message") or {}
        out.extend(_content_text(msg.get("content")))
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                out.append(str(fn.get("name") or ""))
                out.append(str(fn.get("arguments") or ""))
        fn = msg.get("function_call")
        if isinstance(fn, dict):
            out.append(str(fn.get("name") or ""))
            out.append(str(fn.get("arguments") or ""))
    return [x for x in out if x]


def extract_stream_delta(obj: dict, upstream_protocol: str) -> list[str]:
    """从单个上游 SSE 事件中抽取增量文本（用于流式输出审查）。"""
    out: list[str] = []
    if upstream_protocol == "anthropic":
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                out.append(str(delta.get("text") or ""))
            elif delta.get("type") == "input_json_delta":
                out.append(str(delta.get("partial_json") or ""))
        return [x for x in out if x]
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if delta.get("content") is not None:
            out.append(str(delta.get("content")))
        for tc in delta.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                if fn.get("arguments"):
                    out.append(str(fn.get("arguments")))
        fn = delta.get("function_call")
        if isinstance(fn, dict) and fn.get("arguments"):
            out.append(str(fn.get("arguments")))
    return [x for x in out if x]


# ---------------------------------------------------------------- 响应合成
def blocked_response(
    protocol: str,
    model: str,
    reply: str,
    request_id: str,
    usage: dict[str, int] | None = None,
) -> dict:
    """构造与入站协议一致的「默认回复」响应体。"""
    u = usage or {"prompt_tokens": 0, "completion_tokens": 0}
    openai_body = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0),
        },
    }
    if protocol == "anthropic":
        return openai_to_anthropic_response(openai_body)
    return openai_body


def blocked_stream(protocol: str, model: str, reply: str, request_id: str,
                   usage: dict[str, int] | None = None) -> list[str]:
    """构造完整的默认回复 SSE 事件列表（OpenAI 或 Anthropic 协议）。"""
    u = usage or {"prompt_tokens": 0, "completion_tokens": 0}
    chunk_id = f"chatcmpl-{request_id}"
    created = int(time.time())
    base = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model or ""}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]},
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0),
            },
        },
    ]

    if protocol == "anthropic":
        state: dict = {}
        out: list[str] = []
        for chunk in chunks:
            events, _usage = openai_chunk_to_anthropic(chunk, state)
            for ev in events:
                out.append(
                    "event: " + str(ev.get("type", "message")) + "\n"
                    + "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                )
        return out

    out = []
    for chunk in chunks:
        out.append("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n")
    out.append("data: [DONE]\n\n")
    return out


# ---------------------------------------------------------------- 审查记录
def record_review(
    db: Session,
    *,
    request_id: str,
    key: ApiKey,
    policy: SafetyPolicy,
    direction: str,
    action: str,
    result: ReviewResult,
    model: str = "",
    endpoint: str = "",
    is_stream: bool = False,
) -> None:
    """写入一条独立审查记录（仅命中/异常时调用）。"""
    lib_names = result.library_names
    if not lib_names and policy is not None:
        ids = [x for x in (policy.library_ids or []) if x]
        if ids:
            libs = db.query(SafetyLibrary).filter(SafetyLibrary.id.in_(ids)).all()
            lib_names = [x.name for x in libs]
    row = SafetyReview(
        request_id=(request_id or "")[:32],
        api_key_id=key.id if key else None,
        key_name=key.name if key else "",
        policy_id=policy.id if policy else None,
        policy_name=policy.name if policy else "",
        library_names=lib_names,
        direction=direction,
        action=action,
        matched_terms=result.matched,
        text_excerpt=(result.excerpt or "")[:2000],
        model=(model or "")[:160],
        endpoint=(endpoint or "")[:96],
        is_stream=bool(is_stream),
        detail=(result.detail or result.error or "")[:2000],
    )
    db.add(row)


def result_detail(result: ReviewResult, fallback: str = "") -> str:
    return result.detail or result.error or fallback
