"""LLM 服务定义：双协议入口、厂商表单元信息、配置即测、模型管理接口。"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.models import Provider
from app.core.providers import resolve_timeout
from app.core.registry import ProviderTypeSpec, ServiceSpec, register_service
from app.services.llm.dispatch import build_models_request
from app.services.llm.proxy import close_client, get_client
from app.services.llm.router import router
from app.services.llm.schemas import fetch_router, router as models_router

_COMMON_SETTINGS = [
    {"name": "chat_path", "label": "对话接口路径（相对 Base URL）", "placeholder": "chat/completions"},
    {"name": "models_path", "label": "模型列表路径（相对 Base URL）", "placeholder": "models"},
    {"name": "auth_scheme", "label": "鉴权头前缀", "placeholder": "Bearer"},
]

PROVIDER_TYPE_SPECS: list[ProviderTypeSpec] = [
    ProviderTypeSpec(
        type="openai",
        label="OpenAI 兼容供应商",
        description="任何兼容 /v1/chat/completions 的服务：OpenAI、DeepSeek、通义、硅基流动、vLLM、Ollama 等。",
        fields=[
            {"name": "api_key", "label": "API Key（私有化可留空）", "secret": True, "required": False},
        ],
        setting_fields=_COMMON_SETTINGS,
        hint="Base URL 请填到 /v1 层级，例如 https://api.openai.com/v1 、https://api.deepseek.com/v1",
    ),
    ProviderTypeSpec(
        type="anthropic",
        label="Anthropic",
        description="原生 Anthropic Messages 协议，出站自动做 OpenAI ↔ Anthropic 转换。",
        fields=[
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "anthropic_version", "label": "anthropic-version", "placeholder": "2023-06-01"},
        ],
        hint="Base URL 例如 https://api.anthropic.com/v1",
    ),
    ProviderTypeSpec(
        type="generic",
        label="通用（自定义）",
        description="自定义路径 / 请求头 / 鉴权前缀，适合各种私有化或代理网关。",
        fields=[
            {"name": "api_key", "label": "API Key（可留空）", "secret": True, "required": False},
        ],
        setting_fields=_COMMON_SETTINGS,
        hint="额外的请求头可写进「高级设置 JSON」：{\"headers\":{\"X-Org\":\"abc\"}}",
    ),
    ProviderTypeSpec(
        type="xfyun-cc",
        label="讯飞CC（星火开放平台 One API）",
        description="讯飞星火开放平台 / One API 网关，OpenAI 兼容协议，内部代理 Claude 等模型。"
                    "网关会自动把 OpenAI / Anthropic 两种入站协议统一翻译为 OpenAI 格式再转发。",
        fields=[
            {"name": "api_key", "label": "API 令牌（One API / 讯飞开放平台令牌）", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "chat_path", "label": "对话接口路径（相对 Base URL）", "placeholder": "chat/completions"},
            {"name": "models_path", "label": "模型列表路径（相对 Base URL）", "placeholder": "models"},
            {"name": "auth_scheme", "label": "鉴权头前缀", "placeholder": "Bearer"},
            {
                "name": "upstream_protocol",
                "label": "上游协议（openai / anthropic）",
                "placeholder": "openai",
            },
            {"name": "anthropic_version", "label": "anthropic-version（仅 anthropic 上游）", "placeholder": "2023-06-01"},
        ],
        hint="Base URL 填 https://one.iflytek.com/api/llm/console/chat/v1（含 /v1）。"
             "默认按 OpenAI 兼容（Bearer 鉴权）出站；若你的 讯飞CC 实例仅接受 Anthropic，"
             "可在高级设置把 upstream_protocol 改为 anthropic。两种入站协议（/v1/chat/completions 与 /v1/messages）均支持。",
    ),
]


async def test_provider(
    db: Session,
    provider: Provider,
    file_bytes: bytes | None = None,
    mime: str | None = None,
    params: dict | None = None,
) -> dict:
    """用已保存的凭证访问上游「模型列表」接口，验证连通性与鉴权。"""
    params = params or {}
    url, headers = build_models_request(provider)
    if not url or url.startswith("/"):
        return {"ok": False, "message": "未配置 Base URL，请先填写接入地址", "latency_ms": 0, "detail": {}}

    t0 = time.monotonic()
    try:
        # 连通性测试不必等满厂商的长超时，但不能比厂商配置更宽松
        probe_timeout = min(15.0, resolve_timeout(provider))
        resp = await get_client().get(url, headers=headers, timeout=probe_timeout)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code != 200:
            return {
                "ok": False,
                "message": f"HTTP {resp.status_code}: {resp.text[:300]}",
                "latency_ms": latency,
                "detail": {"url": url},
            }
        data = resp.json()
        items = data.get("data", []) if isinstance(data, dict) else []
        names = [str(i.get("id") or i.get("name") or "") for i in items if isinstance(i, dict)]
        names = [n for n in names if n]
        return {
            "ok": True,
            "message": f"连接成功，上游返回 {len(names)} 个模型",
            "latency_ms": latency,
            "detail": {"url": url, "models_count": len(names), "models": names[:20]},
        }
    except Exception as e:
        return {
            "ok": False,
            "message": str(e),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "detail": {"url": url},
        }


register_service(
    ServiceSpec(
        key="llm",
        label="大模型对话",
        description="统一对接 OpenAI 兼容与 Anthropic 协议的上游模型，对外同时提供 "
                    "/v1/chat/completions 与 /v1/messages（含 SSE 流式），按模型配置单价计费。",
        endpoints=[
            {"method": "POST", "path": "/v1/chat/completions", "summary": "OpenAI 兼容对话（支持 stream）"},
            {"method": "POST", "path": "/v1/messages", "summary": "Anthropic 兼容消息（支持 stream）"},
            {"method": "GET", "path": "/v1/models", "summary": "可用模型列表"},
        ],
        provider_types=PROVIDER_TYPE_SPECS,
        router=router,
        test_provider=test_provider,
        supports_models=True,
        admin_routers=[models_router, fetch_router],
        usage_units="tokens",
        order=20,
        on_shutdown=close_client,
    )
)
