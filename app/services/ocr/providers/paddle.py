"""私有化部署 OCR 适配器（以 PaddleOCR / PP-Structure 服务为例，可配置）。"""
import json
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.normalize import extract


class PaddleProvider(OCRProvider):
    type = "paddle"
    label = "私有化 PaddleOCR"

    def __init__(self, config):
        super().__init__(config)
        self.url = self.settings.get("url")
        self.image_field = self.settings.get("image_field", "images")
        self.method = self.settings.get("method", "POST").upper()
        self.content_type = self.settings.get("content_type", "application/json")
        self.mode = self.settings.get("mode", "json")  # json | multipart

    async def recognize(self, image, mime, engine, options):
        if not self.url:
            raise RuntimeError("PaddleOCR 缺少 settings.url")
        if self.mode == "multipart":
            files = {self.image_field: ("file", image, mime or "application/octet-stream")}
            resp = await self._request(self.method, self.url, files=files)
        else:
            body = json.dumps({self.image_field: [self._b64(image)]}, ensure_ascii=False)
            resp = await self._request(
                self.method, self.url, data=body,
                headers={"Content-Type": self.content_type},
            )
        try:
            return self._parse(resp.json())
        except Exception:
            return extract({"text": resp.text}, self.settings)

    def _parse(self, js):
        items = None
        if isinstance(js, list):
            first = js[0] if js else {}
            items = first.get("data") or first.get("res") or first
        elif isinstance(js, dict):
            items = js.get("data") or js.get("res") or js
        if not isinstance(items, list):
            return extract(js, self.settings)

        blocks = []
        lines = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            txt = it.get("text") or it.get("words") or ""
            region = it.get("text_region") or it.get("bbox")
            bbox = None
            if region and len(region) >= 2:
                xs = [p[0] for p in region]
                ys = [p[1] for p in region]
                bbox = [min(xs), min(ys), max(xs), max(ys)]
            blocks.append({
                "type": "text",
                "text": txt,
                "bbox": bbox,
                "confidence": it.get("confidence"),
                "index": i,
            })
            lines.append(txt)
        if not lines:
            return extract(js, self.settings)
        return {
            "text": "\n".join(lines),
            "blocks": blocks,
            "pages": 1,
            "char_count": sum(len(t) for t in lines),
        }
