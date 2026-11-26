"""出站请求参数归一化（兼容性护栏）。

背景（2026-11-26 生产故障）：调用方用 Anthropic 协议（Claude Code 等 agent）发来的请求里
带 ``thinking`` 对象和超大 ``max_tokens``（可到 200000）。网关内部统一成 OpenAI 格式后
原样转发给只兼容 OpenAI 参数的上游（如 tokenbom 中转的 deepseek-v4.1-flash），上游报：

* ``field MaxTokens invalid, should be in [1, 65536]``（400，非流 + 流内各一类）
* ``"thinking" is not supported on /v1/chat/completions ... Use "reasoning_effort"``（400）

生产 800 条近期日志里这类参数错误占 115 条，是 agent 调用失败的首要原因。

本模块在「入站 → 内部 OpenAI 格式 → 出站」链路中，对内部请求体做一次归一化：

1. ``max_tokens`` 裁剪到 ``[1, MAX_TOKENS_CAP]``，非法值丢弃；同时合并 Anthropic 的
   ``max_tokens`` 与 OpenAI 的 ``max_completion_tokens``，避免重复字段。
2. Anthropic 的 ``thinking`` 参数按出站协议处理：出站 Anthropic 保留（它原生支持），
   出站 OpenAI 时转成 ``reasoning_effort``（deepseek 等推理模型认这个），无法表达时剥离。
3. 丢弃上游明确不认的 Anthropic 专有字段，避免 400。

归一化只做「兼容性收敛」，不改变模型选择、消息内容或鉴权。
"""
from __future__ import annotations

from typing import Any

# 主流 OpenAI 兼容中转站（含 deepseek）对 max_tokens 的上限。留一点余量，
# 但若上游实际更低，可在 provider settings 里用 max_tokens_cap 覆盖。
DEFAULT_MAX_TOKENS_CAP = 65536

# Anthropic 专有、OpenAI 兼容上游不接受的请求字段。出站 OpenAI 时剥离。
# 注意：thinking 单独处理（尝试转 reasoning_effort），不在此列表。
_ANTHROPIC_ONLY_KEYS = (
    "metadata",          # Anthropic 的 client_id 元数据
    "service_tier",      # Anthropic 特有
    "container",         # code execution 容器
)

_THINKING_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}


def _as_positive_int(value: Any) -> int | None:
    """把值转成正整数；bool 不是合法整数。"""
    if isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _normalize_thinking(body: dict[str, Any], outbound_anthropic: bool) -> None:
    """处理 thinking 参数（原地修改 body）。

    * 出站 Anthropic：保留原样（原生支持 thinking 块）。
    * 出站 OpenAI：按 budget_tokens 推断 reasoning_effort，写入 body；无论能否
      映射都移除 thinking（OpenAI 兼容上游不认它）。
    """
    thinking = body.pop("thinking", None)
    if thinking is None or outbound_anthropic:
        if thinking is not None and outbound_anthropic:
            body["thinking"] = thinking
        return
    if not isinstance(thinking, dict):
        return
    # 只有显式 enabled 才尝试开启推理；disabled 直接丢弃
    if str(thinking.get("type") or "").lower() != "enabled":
        return
    budget = _as_positive_int(thinking.get("budget_tokens"))
    if budget is None:
        return
    # 若调用方已显式指定 reasoning_effort，尊重调用方
    if body.get("reasoning_effort") is not None:
        return
    if budget < 8000:
        effort = "low"
    elif budget < 24000:
        effort = "medium"
    else:
        effort = "high"
    body["reasoning_effort"] = _THINKING_EFFORT_MAP.get(effort, effort)


def _normalize_max_tokens(body: dict[str, Any], cap: int | None) -> None:
    """裁剪 max_tokens，并合并 max_completion_tokens（原地修改）。

    ``cap`` 为 None 时只丢弃非法值、不设上限（原生 Anthropic 出站支持更大的
    输出预算，不该被 65536 误伤）。
    """
    raw = body.get("max_tokens")
    alt = body.pop("max_completion_tokens", None)
    if raw is None:
        raw = alt
    n = _as_positive_int(raw)
    if n is None:
        # 非法值（0 / 负数 / 字符串）直接丢弃，让上游用默认值
        body.pop("max_tokens", None)
        return
    body["max_tokens"] = min(n, cap) if cap else n


def normalize_internal_request(
    body: dict[str, Any],
    *,
    outbound_anthropic: bool = False,
    max_tokens_cap: int | None = None,
) -> dict[str, Any]:
    """返回归一化后的内部请求体（不修改入参）。

    Args:
        body: 内部 OpenAI chat-completions 格式的请求体。
        outbound_anthropic: 本次出站是否走 Anthropic 协议。
        max_tokens_cap: max_tokens 上限；None / 非法时回落到默认值。
    """
    out = dict(body)
    # OpenAI 兼容上游普遍把 max_tokens 限在 65536；Anthropic 原生出站不设此限，
    # 仅丢弃非法值，避免误伤 Claude 等大输出预算的模型。
    cap = None if outbound_anthropic else (_as_positive_int(max_tokens_cap) or DEFAULT_MAX_TOKENS_CAP)
    _normalize_max_tokens(out, cap)
    _normalize_thinking(out, outbound_anthropic)
    if not outbound_anthropic:
        for key in _ANTHROPIC_ONLY_KEYS:
            out.pop(key, None)
    return out
