"""OCR 厂商注册表与路由降级。

* ``PROVIDER_CLASSES``：type -> 适配器类
* ``PROVIDER_TYPE_SPECS``：供管理端动态渲染表单的元信息
* ``route_recognize``：按「默认标记 → weight 大 → priority 小」选择厂商，
  指定 provider 时失败不降级，未指定时自动降级到下一个可用厂商。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.models import Provider
from app.core.providers import missing_required, select_providers
from app.core.registry import ProviderTypeSpec
from app.services.ocr.providers.aliyun import AliyunProvider
from app.services.ocr.providers.baidu import BaiduProvider
from app.services.ocr.providers.generic import GenericProvider
from app.services.ocr.providers.huawei import HuaweiProvider
from app.services.ocr.providers.iflytek import IflytekProvider
from app.services.ocr.providers.paddle import PaddleProvider
from app.services.ocr.providers.tencent import TencentProvider
from app.services.ocr.providers.volcano import VolcanoProvider

logger = logging.getLogger("gateway.ocr")

PROVIDER_CLASSES = {
    "tencent": TencentProvider,
    "aliyun": AliyunProvider,
    "baidu": BaiduProvider,
    "iflytek": IflytekProvider,
    "huawei": HuaweiProvider,
    "volcano": VolcanoProvider,
    "paddle": PaddleProvider,
    "generic": GenericProvider,
}

PROVIDER_TYPE_SPECS: list[ProviderTypeSpec] = [
    ProviderTypeSpec(
        type="tencent",
        label="腾讯云 OCR",
        description="TC3-HMAC-SHA256 签名，支持通用/高精度/手写/身份证/营业执照/银行卡/车牌/表格等。",
        fields=[
            {"name": "secret_id", "label": "SecretId", "secret": True, "required": True},
            {"name": "secret_key", "label": "SecretKey", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "region", "label": "地域（默认 ap-guangzhou）", "placeholder": "ap-guangzhou"},
            {"name": "action", "label": "指定 Action（留空按 engine 映射）", "placeholder": "GeneralBasicOCR"},
        ],
        hint="engine 支持：general / accurate / handwriting / idcard / bizlicense / bankcard / vin / plate / formula / table / waybill",
    ),
    ProviderTypeSpec(
        type="aliyun",
        label="阿里云 OCR",
        description="RPC HMAC-SHA1 签名；host / version / action 可在接入设置中覆盖。",
        fields=[
            {"name": "access_key_id", "label": "AccessKeyId", "secret": True, "required": True},
            {"name": "access_key_secret", "label": "AccessKeySecret", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "host", "label": "接口域名", "placeholder": "ocr-api.cn-hangzhou.aliyuncs.com"},
            {"name": "version", "label": "API 版本", "placeholder": "2021-07-07"},
            {"name": "action", "label": "Action", "placeholder": "RecognizeGeneral"},
        ],
    ),
    ProviderTypeSpec(
        type="baidu",
        label="百度智能云 OCR",
        description="access_token + form 调用，支持通用/高精度/手写/身份证；可配置完整接口地址。",
        fields=[
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "secret_key", "label": "Secret Key", "secret": True, "required": True},
            {"name": "app_id", "label": "AppID（可选，仅记录）", "secret": False, "required": False},
        ],
        setting_fields=[
            {"name": "url", "label": "OCR 接口地址（完整 URL，留空按引擎自动构造）",
             "placeholder": "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"},
            {"name": "path", "label": "接口路径（url 为空时使用）", "placeholder": "accurate_basic"},
        ],
    ),
    ProviderTypeSpec(
        type="iflytek",
        label="讯飞开放平台 OCR",
        description="私有 HTTP API：鉴权参数置于 URL 查询串，请求体为 JSON。服务ID决定能力。",
        fields=[
            {"name": "api_key", "label": "APIKey", "secret": True, "required": True},
            {"name": "api_secret", "label": "APISecret", "secret": True, "required": True},
            {"name": "app_id", "label": "APPID（控制台应用ID）", "secret": False, "required": True},
        ],
        setting_fields=[
            {"name": "url", "label": "接口地址（完整 URL）",
             "placeholder": "https://api.xf-yun.com/v1/private/s00b65163"},
            {"name": "category", "label": "语种类别（留空按服务ID自动匹配）", "placeholder": "mix0"},
        ],
        hint="s00b65163 = 印刷文字识别（多语种，category=mix0）；sf8e6aca1 = 通用文字识别",
    ),
    ProviderTypeSpec(
        type="huawei",
        label="华为云 OCR",
        description="SDK-HMAC-SHA256 / SIGv1，需填写 endpoint 与 host。",
        fields=[
            {"name": "access_key", "label": "AccessKey", "secret": True, "required": True},
            {"name": "secret_key", "label": "SecretKey", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "endpoint", "label": "接口地址", "placeholder": "https://ocr.cn-north-4.myhuaweicloud.com/ocr/general-text"},
            {"name": "host", "label": "Host", "placeholder": "ocr.cn-north-4.myhuaweicloud.com"},
        ],
    ),
    ProviderTypeSpec(
        type="volcano",
        label="火山引擎 OCR",
        description="HMAC-SHA256（SIGv4 风格），需填写 action / version。",
        fields=[
            {"name": "access_key", "label": "AccessKey", "secret": True, "required": True},
            {"name": "secret_key", "label": "SecretKey", "secret": True, "required": True},
        ],
        setting_fields=[
            {"name": "action", "label": "Action", "placeholder": "OCRNormal"},
            {"name": "version", "label": "版本", "placeholder": "2020-08-26"},
        ],
    ),
    ProviderTypeSpec(
        type="paddle",
        label="私有化 PaddleOCR",
        description="对接本地/内网 PaddleOCR 服务，配置地址与字段即可。",
        fields=[],
        setting_fields=[
            {"name": "url", "label": "服务地址（完整 URL）",
             "placeholder": "http://127.0.0.1:8866/predict/ocr_system"},
            {"name": "path", "label": "接口路径（url 不含具体接口时）", "placeholder": "ocr"},
        ],
    ),
    ProviderTypeSpec(
        type="generic",
        label="通用 HTTP OCR（兼容）",
        description="适配任意自定义/私有 HTTP OCR，支持多种传图与鉴权方式。",
        fields=[],
        setting_fields=[
            {"name": "url", "label": "服务地址（完整 URL，必填）",
             "placeholder": "https://your-ocr.example.com/api/recognize"},
            {"name": "method", "label": "请求方法（GET/POST）", "placeholder": "POST"},
            {"name": "image_mode", "label": "传图方式（json/form/multipart/body_base64）",
             "placeholder": "json"},
            {"name": "image_field", "label": "图片字段名", "placeholder": "image"},
            {"name": "extra_fields", "label": "附加字段（JSON 对象）", "json": True,
             "placeholder": "{\"category\":\"\",\"trackId\":\"gw\"}"},
            {"name": "content_type", "label": "Content-Type（json/form 模式生效）",
             "placeholder": "application/json"},
            {"name": "auth_type", "label": "鉴权方式（none/bearer/header/basic）", "placeholder": "bearer"},
            {"name": "auth_token", "label": "鉴权令牌", "placeholder": "xxxx"},
            {"name": "text_path", "label": "返回文本抽取路径", "placeholder": "data.text"},
        ],
        hint="multipart/form 模式会以文件或表单上传；extra_fields 里的字段会一并提交（如图聆云 UOCR 需 category、trackId）。",
    ),
]


# ---- 实例缓存（配置变更后由管理端调用 invalidate 清理）----
_cache: dict[str, OCRProvider] = {}


def invalidate() -> None:
    _cache.clear()


def get_instance(provider: Provider) -> OCRProvider:
    inst = _cache.get(provider.id)
    if inst is None:
        cls = PROVIDER_CLASSES.get(provider.type)
        if not cls:
            raise ValueError(f"未知的 OCR 厂商类型：{provider.type}")
        inst = cls(provider)
        _cache[provider.id] = inst
    return inst


def provider_type_spec(type_name: str) -> ProviderTypeSpec | None:
    return next((t for t in PROVIDER_TYPE_SPECS if t.type == type_name), None)


def missing_credentials(provider: Provider) -> list[str]:
    """返回该配置缺失的必填凭证字段名（供列表提示与保存前校验）。"""
    spec = provider_type_spec(provider.type)
    if not spec:
        return []
    return missing_required(provider, spec.fields)


def _ocr_error(detail: str, error_type: str) -> HTTPException:
    """构造带归因分类的 HTTPException，供日志按 error_type 聚合。

    错误分类与 LLM 侧命名保持一致（见 app/services/llm/proxy.py 的 ERROR_*）。
    """
    exc = HTTPException(status.HTTP_502_BAD_GATEWAY, detail=detail)
    exc.error_type = error_type  # type: ignore[attr-defined]
    return exc


async def route_recognize(
    db: Session,
    image: bytes,
    mime: str,
    engine: Optional[str],
    options: dict,
    provider_hint: Optional[str],
    allowed_types: list[str] | None = None,
    channel: Optional[str] = None,
) -> tuple[dict, Provider]:
    """选择厂商并调用；未指定 provider 时按默认/权重/优先级降级。"""
    candidates = select_providers(db, "ocr", provider_hint, allowed_types)
    if channel:
        # 指定渠道（Key 定点绑定）：仅允许走该厂商，且必须是启用中的 OCR 厂商
        candidates = [p for p in candidates if p.id == channel]
        if not candidates:
            raise _ocr_error(
                "该 Key 指定的渠道不可用或不属于 OCR 服务",
                "channel_unavailable",
            )
    if not candidates:
        raise _ocr_error(
            "没有可用且被当前 API Key 允许的 OCR 厂商，请到管理后台检查配置",
            "provider_unavailable",
        )

    explicit = bool(provider_hint)
    last_err: str | None = None
    for provider in candidates:
        try:
            inst = get_instance(provider)
            result = await inst.recognize(image, mime, engine, options)
            return result, provider
        except HTTPException:
            raise
        except Exception as e:  # 降级
            last_err = str(e)
            logger.warning("OCR 厂商 %s(%s) 调用失败：%s", provider.name, provider.type, e)
            if explicit:
                raise _ocr_error(
                    f"指定厂商 {provider.type}/{provider.name} 调用失败：{last_err}",
                    "upstream_error",
                )
            continue

    raise _ocr_error(f"所有可用厂商均调用失败，最后错误：{last_err}", "upstream_error")
