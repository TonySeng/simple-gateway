"""腾讯云 OCR 适配器（TC3-HMAC-SHA256）。"""
import json
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.sign import tencent_authorization

# engine -> API Action 映射；未命中则用 settings.action 或默认 GeneralBasicOCR
ENGINE_ACTIONS = {
    "general": "GeneralBasicOCR",
    "general_basic": "GeneralBasicOCR",
    "accurate": "GeneralAccurateOCR",
    "general_accurate": "GeneralAccurateOCR",
    "handwriting": "GeneralHandwritingOCR",
    "idcard": "IDCardOCR",
    "bizlicense": "BizLicenseOCR",
    "bankcard": "BankCardOCR",
    "vin": "VinOCR",
    "plate": "LicensePlateOCR",
    "formula": "FormulaOCR",
    "table": "TableOCR",
    "waybill": "WaybillOCR",
}


class TencentProvider(OCRProvider):
    type = "tencent"
    label = "腾讯云 OCR"
    required_credentials = ("secret_id", "secret_key")

    def __init__(self, config):
        super().__init__(config)
        self.secret_id = self.credentials.get("secret_id") or self.credentials.get("SecretId")
        self.secret_key = self.credentials.get("secret_key") or self.credentials.get("SecretKey")
        self.region = self.settings.get("region", "ap-guangzhou")
        self.host = self.settings.get("host", "ocr.tencentcloudapi.com")

    async def recognize(self, image, mime, engine, options):
        action = (
            self.settings.get("action")
            or ENGINE_ACTIONS.get((engine or "").lower())
            or ENGINE_ACTIONS.get("general")
        )
        payload = {"ImageBase64": self._b64(image)}
        if options.get("image_url"):
            payload["ImageUrl"] = options["image_url"]
        extra = {k: v for k, v in options.items() if k not in ("image_url",)}
        payload.update(extra)
        body = json.dumps(payload, ensure_ascii=False)
        headers = tencent_authorization(
            self.secret_id, self.secret_key, self.host, action, body,
            service="ocr", region=self.region,
        )
        resp = await self._request("POST", f"https://{self.host}/", data=body, headers=headers)
        data = resp.json().get("Response", {})
        if "Error" in data:
            raise RuntimeError(f"Tencent OCR error: {data['Error'].get('Message')}")
        detections = data.get("TextDetections", [])
        blocks = []
        lines = []
        for i, d in enumerate(detections):
            txt = d.get("DetectedText", "")
            lines.append(txt)
            poly = d.get("ItemPolygon") or {}
            bbox = None
            if poly:
                x, y, w, h = poly.get("X", 0), poly.get("Y", 0), poly.get("Width", 0), poly.get("Height", 0)
                bbox = [x, y, x + w, y + h]
            blocks.append({
                "type": "text",
                "text": txt,
                "bbox": bbox,
                "confidence": d.get("Confidence"),
                "index": i,
            })
        return {
            "text": "\n".join(lines),
            "blocks": blocks,
            "pages": 1,
            "char_count": sum(len(b["text"]) for b in blocks),
        }
