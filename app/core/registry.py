"""服务注册表 —— 网关的「可插拔」核心。

任何一个中转服务只需要实现一个 :class:`ServiceSpec` 并调用 :func:`register_service`
即可获得：统一管理后台入口、统一 API Key 鉴权与配额、统一调用日志与统计、
统一厂商配置 CRUD 与「配置即测」。

新增服务的完整步骤::

    # app/services/asr/service.py
    from app.core.registry import ServiceSpec, ProviderTypeSpec, register_service

    register_service(ServiceSpec(
        key="asr",
        label="语音识别",
        description="……",
        endpoints=[{"method": "POST", "path": "/v1/asr/transcribe", "summary": "语音转写"}],
        provider_types=[ProviderTypeSpec(...)],
        router=router,
        test_provider=test_provider,
    ))

然后在 ``app/services/__init__.py`` 的 ``load_services()`` 里 import 该模块
（或使用 ``pkgutil`` 自动发现）。数据库、前端表单、审计日志会自动适配。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter

logger = logging.getLogger("gateway.registry")


@dataclass(frozen=True)
class ProviderTypeSpec:
    """一个厂商类型的表单元信息（前端据此动态渲染，无需为每种厂商写前端代码）。"""

    type: str
    label: str
    description: str = ""
    # 需要加密存储的凭证字段：[{name,label,secret,required,placeholder}]
    fields: list[dict] = field(default_factory=list)
    # 明文配置字段（url / engine / action ...），落到 Provider.settings
    setting_fields: list[dict] = field(default_factory=list)
    # 额外说明，展示在表单顶部
    hint: str = ""


@dataclass
class ServiceSpec:
    key: str
    label: str
    description: str = ""
    # 对外接口清单，用于「接入说明」页展示
    endpoints: list[dict] = field(default_factory=list)
    provider_types: list[ProviderTypeSpec] = field(default_factory=list)
    router: APIRouter | None = None
    # async (db, provider) -> dict ；返回 {ok, message, latency_ms, ...}
    test_provider: Optional[Callable[..., Awaitable[dict]]] = None
    # 清除该服务的厂商实例缓存（配置变更后调用）
    invalidate: Optional[Callable[[], None]] = None
    # 该服务是否需要「模型/单价」管理（目前仅 LLM）
    supports_models: bool = False
    # 服务专属的管理接口（如 LLM 的模型管理、拉取模型列表）
    admin_routers: list[APIRouter] = field(default_factory=list)
    # 用量单位说明，用于前端表头（如 chars / tokens）
    usage_units: str = ""
    # 展示顺序（小的在前），用于后台侧栏与服务标签排序
    order: int = 100
    # 应用关闭时的清理钩子（如关闭共享 HTTP 客户端）
    on_shutdown: Optional[Callable[[], Awaitable[None]]] = None

    def provider_type(self, type_name: str) -> ProviderTypeSpec | None:
        return next((t for t in self.provider_types if t.type == type_name), None)

    def type_label(self, type_name: str) -> str:
        spec = self.provider_type(type_name)
        return spec.label if spec else type_name


_SERVICES: dict[str, ServiceSpec] = {}


def register_service(spec: ServiceSpec) -> ServiceSpec:
    if spec.key in _SERVICES:
        logger.warning("服务 %s 重复注册，后者覆盖前者", spec.key)
    _SERVICES[spec.key] = spec
    return spec


def get_service(key: str) -> ServiceSpec | None:
    return _SERVICES.get(key)


def all_services() -> list[ServiceSpec]:
    return sorted(_SERVICES.values(), key=lambda s: (s.order, s.key))


def service_keys() -> list[str]:
    return list(_SERVICES.keys())


def service_meta() -> list[dict]:
    """供前端渲染的完整服务元信息（含厂商表单字段）。"""
    return [
        {
            "key": s.key,
            "label": s.label,
            "description": s.description,
            "endpoints": s.endpoints,
            "supports_models": s.supports_models,
            "usage_units": s.usage_units,
            "provider_types": [
                {
                    "type": t.type,
                    "label": t.label,
                    "description": t.description,
                    "fields": t.fields,
                    "setting_fields": t.setting_fields,
                    "hint": t.hint,
                }
                for t in s.provider_types
            ],
        }
        for s in all_services()
    ]


def invalidate_all() -> None:
    for s in _SERVICES.values():
        if s.invalidate:
            try:
                s.invalidate()
            except Exception:  # 缓存失效失败不应影响主流程
                logger.exception("服务 %s 缓存清理失败", s.key)


def load_services() -> None:
    """自动发现 app/services/*/service.py 并注册。

    约定：每个服务是 ``app.services.<key>`` 包，内部 ``service.py`` 模块在导入时
    完成 ``register_service`` 调用。
    """
    import app.services as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.ispkg:
            continue
        module_name = f"app.services.{info.name}.service"
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as e:
            if e.name == module_name:
                logger.debug("跳过未提供服务定义的包 %s", info.name)
                continue
            raise
