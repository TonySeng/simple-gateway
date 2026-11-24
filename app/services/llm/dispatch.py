"""构建发往上游的出站请求。

内部统一采用 OpenAI chat-completions 格式；本模块把它映射到厂商的线上格式并拼装 URL/Headers。
厂商凭证从 ``Provider.credentials_enc`` 解密后的 ``api_key`` 取，明文配置放 ``Provider.settings``。
"""
from __future__ import annotations

from typing import Any

from app.core.models import Model, Provider
from app.core.providers import get_credentials, provider_settings
from app.services.llm.translators import openai_to_anthropic_request


def _base(provider: Provider) -> str:
    base = (provider.base_url or "").rstrip("/")
    if not base:
        base = (provider_settings(provider).get("base_url") or "").rstrip("/")
    return base


def _api_key(provider: Provider) -> str:
    creds = get_credentials(provider)
    return str(creds.get("api_key") or creds.get("apiKey") or "")


def is_anthropic_outbound(provider: Provider) -> bool:
    """该厂商出站是否使用 Anthropic 协议。

    默认仅 ``anthropic`` 类型走 Anthropic 协议；``xfyun-cc`` 等 OpenAI 兼容类型
    可用高级设置 ``upstream_protocol=anthropic`` 切到 Anthropic 出站，以兼容
    某些同时暴露两种协议的 One API 风格网关。
    """
    cfg = provider_settings(provider)
    if str(cfg.get("upstream_protocol") or "").lower() == "anthropic":
        return True
    return provider.type == "anthropic"


def resolve_body(
    provider: Provider, model: Model, internal_body: dict[str, Any]
) -> dict[str, Any]:
    body = dict(internal_body)
    body["model"] = model.provider_model or model.name
    if is_anthropic_outbound(provider):
        return openai_to_anthropic_request(body)
    return body


def build_outbound(
    provider: Provider,
    model: Model,
    internal_body: dict[str, Any],
) -> tuple[str, dict[str, str], dict[str, Any], str]:
    """返回 (url, headers, outbound_body, upstream_protocol)。

    ``upstream_protocol`` 只会是 ``"openai"`` 或 ``"anthropic"``，供 proxy.py
    决定出站/入站之间的协议转换方式。
    """
    cfg = provider_settings(provider)
    base = _base(provider)
    anthropic = is_anthropic_outbound(provider)
    body = resolve_body(provider, model, internal_body)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = _api_key(provider)

    if anthropic:
        url = f"{base}/messages"
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = str(cfg.get("anthropic_version") or "2023-06-01")
        # 兼顾 One API 等中转：部分实现仅认 Bearer，故两种头都带
        if key:
            headers["Authorization"] = f"Bearer {key}"
    else:
        chat_path = str(cfg.get("chat_path") or "chat/completions")
        url = f"{base}/{chat_path.lstrip('/')}"
        scheme = str(cfg.get("auth_scheme") or "Bearer")
        if key:
            headers["Authorization"] = f"{scheme} {key}"
        # 尽量让上游在流式响应中上报 usage，便于计费
        if body.get("stream") and "stream_options" not in body:
            body["stream_options"] = {"include_usage": True}
        extra = cfg.get("headers") or {}
        if isinstance(extra, dict):
            headers.update({k: str(v) for k, v in extra.items()})

    return url, headers, body, "anthropic" if anthropic else "openai"


def build_models_request(provider: Provider) -> tuple[str, dict[str, str]]:
    """构造「拉取模型列表」的请求（OpenAI /v1/models 风格）。"""
    cfg = provider_settings(provider)
    base = _base(provider)
    anthropic = is_anthropic_outbound(provider)
    headers: dict[str, str] = {"Content-Type": "application/json"}
    key = _api_key(provider)

    if anthropic:
        url = f"{base}/v1/models"
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = str(cfg.get("anthropic_version") or "2023-06-01")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    else:
        models_path = str(cfg.get("models_path") or "models")
        url = f"{base}/{models_path.lstrip('/')}"
        scheme = str(cfg.get("auth_scheme") or "Bearer")
        if key:
            headers["Authorization"] = f"{scheme} {key}"
        extra = cfg.get("headers") or {}
        if isinstance(extra, dict):
            headers.update({k: str(v) for k, v in extra.items()})

    return url, headers
