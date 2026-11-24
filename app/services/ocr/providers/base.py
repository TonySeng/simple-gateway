"""OCR 提供商适配器基类与共享 HTTP 客户端。

统一约定：适配器接收 :class:`app.core.models.Provider`（service="ocr"），
从加密凭证字段与明文 settings 中取配置。
"""
from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from app.core.models import Provider
from app.core.providers import get_credentials, httpx_timeout, provider_settings

# 共享异步客户端（应用关闭时 aclose）
_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class MissingCredentials(RuntimeError):
    """提供商缺少必填凭证（配置不完整）。"""


class OCRProvider(ABC):
    """所有 OCR 适配器必须实现的接口。

    ``recognize`` 返回统一结构::

        {
            "text": str,
            "blocks": [{"type","text","bbox","polygon","confidence","line_id","index"}],
            "pages": int,
            "char_count": int,
            "raw": dict | None,
        }
    """

    type: str = "base"
    label: str = "Base"
    # 必填凭证字段名（与管理后台表单一致）；空元组表示该类型无需凭证
    required_credentials: tuple[str, ...] = ()

    def __init__(self, provider: Provider):
        self.provider = provider
        self.config = provider  # 兼容旧代码
        self.name = provider.name
        self.is_default = provider.is_default
        self.priority = provider.priority
        self.base_url = provider.base_url or ""
        self.credentials = get_credentials(provider)
        self.settings = provider_settings(provider)
        self._validate_credentials()

    # ---- 凭证 ----
    def _cred(self, key: str):
        """取凭证值，兼容 SecretId / SECRET_ID / secretId 等不同书写。"""
        val = self.credentials.get(key)
        if val:
            return val
        norm = key.lower().replace("_", "")
        for k, v in (self.credentials or {}).items():
            if v and str(k).lower().replace("_", "") == norm:
                return v
        return None

    def _validate_credentials(self) -> None:
        """缺少必填凭证时立刻抛出可读错误，而不是让签名逻辑报 NoneType。"""
        if not self.required_credentials:
            return
        missing = [k for k in self.required_credentials if not self._cred(k)]
        if missing:
            raise MissingCredentials(
                f"{self.label} 未配置凭证：缺少 {'、'.join(missing)}。"
                "请到管理后台「服务与提供商 → 编辑」中填写后重新测试。"
            )

    @abstractmethod
    async def recognize(
        self, image: bytes, mime: str, engine: Optional[str], options: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    # ---- 通用工具 ----
    async def _request(self, method: str, url: str, **kwargs):
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("User-Agent", "Simple-Gateway/1.0")
        # 按厂商设超时（未配置则回落到全局 SG_PROXY_TIMEOUT）
        kwargs.setdefault("timeout", httpx_timeout(self.provider))
        resp = await get_client().request(method, url, headers=headers, **kwargs)
        # 统一拦截上游错误：否则 5xx/4xx 的错误页会被当成「识别成功但无文字」，
        # 既掩盖故障又让降级逻辑失效。
        if resp.status_code >= 400:
            snippet = " ".join((resp.text or "").split())[:300]
            raise RuntimeError(f"{self.label} 上游返回 HTTP {resp.status_code}：{snippet}")
        return resp

    def _b64(self, image: bytes) -> str:
        return base64.b64encode(image).decode("ascii")

    def _guess_engine(self, engine: Optional[str], default: str) -> str:
        return engine or self.settings.get("engine") or default
