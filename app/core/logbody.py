"""日志详情内容处理：脱敏、截断、序列化。

详情（请求体 / 上游返回体）会落库供排障，因此必须：

* **脱敏** —— 上游错误体里经常回显 api_key / Bearer token，不能原样进库；
* **截断** —— 一条超长响应（尤其流式）不该把库撑爆，也不该拖慢列表；
* **标记** —— 被截断时留下明确提示，避免排障时误以为上游只返回了这么多。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.config import settings

# 上游响应体脱敏：避免把 key 写进日志表
_REDACT_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{8,}"), r"\1***"),
    (re.compile(r'(?i)("(?:api_?key|secret|token|access_?key)"\s*:\s*")[^"]{6,}(")'), r"\1***\2"),
)


def redact(text: str) -> str:
    """抹掉文本里的密钥/令牌。"""
    if not text:
        return ""
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


_TRUNC_NOTE = "\n…（已截断 {n} 字符，可在 SG_LOG_BODY_LIMIT 调整上限）"


def limit_text(text: str, limit: int | None = None) -> str:
    """按字符数截断，并在末尾明确标注被截掉了多少。

    提示语本身也占用长度，保证返回值总长不超过上限 —— 否则调用方再截一刀
    会把提示切掉，排障时就分不清「上游只返回了这么多」还是「被网关截了」。
    """
    if not text:
        return ""
    cap = int(limit if limit is not None else settings.log_body_limit)
    if cap <= 0 or len(text) <= cap:
        return text
    note = _TRUNC_NOTE.format(n=len(text) - cap)
    if len(note) >= cap:
        # 上限小到连提示都放不下（极少见）：优先保证不超长
        return text[:cap]
    keep = max(0, cap - len(note))
    note = _TRUNC_NOTE.format(n=len(text) - keep)
    keep = max(0, cap - len(note))
    return f"{text[:keep]}{note}"


def dump_body(obj: Any, limit: int | None = None) -> str:
    """把请求体转成可读文本（JSON 紧凑单行），并脱敏截断。"""
    if obj is None:
        return ""
    if isinstance(obj, (bytes, bytearray)):
        try:
            obj = obj.decode("utf-8", "replace")
        except Exception:
            return "<二进制内容，已省略>"
    if isinstance(obj, str):
        return limit_text(redact(obj), limit)
    try:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(obj)
    return limit_text(redact(text), limit)


def pretty_body(text: str) -> str:
    """详情展示用：能当 JSON 解析就缩进美化，否则原样返回。"""
    if not text:
        return ""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        return json.dumps(json.loads(stripped), ensure_ascii=False, indent=2)
    except Exception:
        return text


class BodyCollector:
    """流式响应累积器：只保留前 N 个字符，超限后停止拼接。

    流式转发对延迟敏感，不能在每个 chunk 上做正则脱敏；这里只累计原文，
    落库前统一脱敏一次。
    """

    __slots__ = ("_parts", "_size", "_cap", "_truncated")

    def __init__(self, cap: int | None = None):
        self._parts: list[str] = []
        self._size = 0
        self._cap = int(cap if cap is not None else settings.log_body_limit)
        self._truncated = False

    def add(self, text: str) -> None:
        if not text or self._truncated:
            return
        if self._cap > 0 and self._size + len(text) > self._cap:
            remain = self._cap - self._size
            if remain > 0:
                self._parts.append(text[:remain])
                self._size += remain
            self._truncated = True
            return
        self._parts.append(text)
        self._size += len(text)

    def text(self) -> str:
        raw = "".join(self._parts)
        if self._truncated:
            raw += f"\n…（流式内容超过 {self._cap} 字符，已截断）"
        return redact(raw) if raw else ""
