"""OCR 服务定义：对外接口、厂商表单元信息、配置即测。

新服务若要接入本网关，照抄本文件的结构即可（见 app/core/registry.py 顶部说明）。
"""
from __future__ import annotations

import base64
import io
import os
import time

from sqlalchemy.orm import Session

from app.core.models import Provider
from app.core.registry import ServiceSpec, register_service
from app.services.ocr.providers.base import close_client
from app.services.ocr.routing import (
    PROVIDER_TYPE_SPECS,
    get_instance,
    invalidate,
    missing_credentials,
)
from app.services.ocr.router import router

# 打包的测试图（640x220，白底黑字 "OCR TEST 1234567890"）：
# 避免运行时依赖 Pillow，也避免 1x1 图被云厂商判为「image size error」。
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TEST_IMAGE_PATH = os.path.join(_APP_DIR, "static", "test_ocr.png")


def _default_test_image() -> tuple[bytes, str]:
    """优先使用打包测试图；缺失时用 Pillow 现场生成；再不行退回 1x1 白图。"""
    try:
        with open(_TEST_IMAGE_PATH, "rb") as f:
            return f.read(), "image/png"
    except OSError:
        pass
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.new("RGB", (640, 220), "white")
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=56)
        except TypeError:
            font = ImageFont.load_default()
        draw.text((30, 80), "OCR TEST 1234567890", fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        white_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        return white_1x1, "image/png"


async def test_provider(
    db: Session,
    provider: Provider,
    file_bytes: bytes | None = None,
    mime: str | None = None,
    params: dict | None = None,
) -> dict:
    """用该厂商已保存的真实凭证实际调用一次 OCR。"""
    params = params or {}
    if file_bytes:
        data, content_type = file_bytes, (mime or "application/octet-stream")
    else:
        data, content_type = _default_test_image()

    engine = params.get("engine") or None
    t0 = time.monotonic()
    try:
        inst = get_instance(provider)  # 会做必填凭证校验，必须放在 try 内
        result = await inst.recognize(data, content_type, engine, {})
        latency = int((time.monotonic() - t0) * 1000)
        text = result.get("text", "") or ""
        blocks = result.get("blocks") or []
        return {
            "ok": True,
            "message": f"调用成功，识别到 {len(text)} 个字符",
            "latency_ms": latency,
            "detail": {
                "text": text[:2000],
                "char_count": result.get("char_count", len(text)),
                "blocks_count": len(blocks),
                "sample_blocks": blocks[:5],
            },
        }
    except Exception as e:  # 配置/签名/网络错误一律回显，由前端展示
        return {
            "ok": False,
            "message": str(e),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "detail": {},
        }


register_service(
    ServiceSpec(
        key="ocr",
        label="OCR 文字识别",
        description="统一对接腾讯/阿里/百度/讯飞/华为/火山等云厂商 OCR 与私有化 PaddleOCR、通用 HTTP，"
                    "按权重自动降级；返回统一全文与结构化区块。",
        endpoints=[
            {"method": "POST", "path": "/v1/ocr/recognize", "summary": "JSON 识别（base64 / URL）"},
            {"method": "POST", "path": "/v1/ocr/recognize/file", "summary": "文件上传识别"},
            {"method": "GET", "path": "/v1/ocr/types", "summary": "支持的厂商类型"},
        ],
        provider_types=PROVIDER_TYPE_SPECS,
        router=router,
        test_provider=test_provider,
        invalidate=invalidate,
        usage_units="字符",
        order=10,
        on_shutdown=close_client,
    )
)

# 供管理端复用的缺失凭证检查
ocr_missing_credentials = missing_credentials
