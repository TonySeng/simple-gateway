"""华为云 OCR 适配器（SDK-HMAC-SHA256 / SIGv1）。"""
import datetime as dt
import json
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.normalize import extract
from app.services.ocr.providers.sign import huawei_authorization


class HuaweiProvider(OCRProvider):
    type = "huawei"
    label = "华为云 OCR"
    required_credentials = ("access_key", "secret_key")

    def __init__(self, config):
        super().__init__(config)
        self.ak = self.credentials.get("access_key") or self.credentials.get("AccessKey")
        self.sk = self.credentials.get("secret_key") or self.credentials.get("SecretKey")
        # 示例：https://ocr.cn-north-4.myhuaweicloud.com/v2/<project_id>/ocr/general-text
        self.endpoint = self.settings.get("endpoint")
        self.host = self.settings.get("host")
        if not self.host and self.endpoint:
            self.host = self.endpoint.split("//", 1)[-1].split("/", 1)[0]
        self.content_type = self.settings.get("content_type", "application/json")

    async def recognize(self, image, mime, engine, options):
        if not self.endpoint:
            raise RuntimeError("华为云 OCR 缺少 settings.endpoint")
        body = json.dumps({"image": self._b64(image)}, ensure_ascii=False)
        sdk_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Host": self.host,
            "X-Sdk-Date": sdk_date,
            "Content-Type": self.content_type,
        }
        signed_headers = ["Host", "X-Sdk-Date", "Content-Type"]
        auth = huawei_authorization(
            self.ak, self.sk, "POST", "/", "", headers, signed_headers, sdk_date, body
        )
        headers["Authorization"] = auth
        resp = await self._request("POST", self.endpoint, data=body, headers=headers)
        return extract(resp.json(), self.settings)
