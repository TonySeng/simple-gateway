"""OCR 对外接口的数据契约。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class OCRRequest(BaseModel):
    image_base64: Optional[str] = Field(None, description="base64 图片（可带 data: 前缀）")
    image_url: Optional[str] = Field(None, description="图片 URL")
    provider: Optional[str] = Field(None, description="指定厂商 type 或配置名；留空走默认路由")
    engine: Optional[str] = Field(None, description="识别类型，如 general / accurate / idcard")
    options: dict[str, Any] = Field(default_factory=dict, description="透传给厂商的额外参数")
    with_raw: bool = Field(False, description="是否返回上游原始响应")


class OCRBlock(BaseModel):
    type: str = "text"
    text: str = ""
    bbox: Optional[list[float]] = None
    polygon: Optional[list[float]] = None
    confidence: Optional[float] = None
    line_id: Optional[int] = None
    index: Optional[int] = None


class OCRResult(BaseModel):
    request_id: str
    service: str = "ocr"
    provider: str
    provider_name: str = ""
    engine: Optional[str] = None
    text: str
    blocks: list[OCRBlock] = []
    pages: int = 1
    char_count: int = 0
    cost_ms: int = 0
    raw: Optional[dict] = None
