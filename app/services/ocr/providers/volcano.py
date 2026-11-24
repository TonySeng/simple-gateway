"""火山引擎（字节）OCR 适配器（HMAC-SHA256 / SIGv4 风格）。

注：火山签名与 AWS SIGv4 近似，首次联调请以官方示例逐字段核对 X-Date、签名串。
"""
import datetime as dt
import hashlib
import hmac
import json
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.normalize import extract
from app.services.ocr.providers.sign import hmac_sha256, sha256_hex


def _volcano_authorization(ak, sk, host, body, region, service, x_date):
    payload_hash = sha256_hex(body)
    canonical_headers = (
        f"content-type:application/json\nhost:{host}\n"
        f"x-content-sha256:{payload_hash}\nx-date:{x_date}\n"
    )
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, payload_hash]
    )
    string_to_sign = "HMAC-SHA256\n" + x_date + "\n" + sha256_hex(canonical_request)
    date = x_date[:8]
    k_date = hmac_sha256(("HMAC-SHA256" + sk).encode(), date)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    k_signing = hmac_sha256(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"HMAC-SHA256 Credential={ak}/{date}/{region}/{service}/request, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    ), payload_hash


class VolcanoProvider(OCRProvider):
    type = "volcano"
    label = "火山引擎 OCR"
    required_credentials = ("access_key", "secret_key")

    def __init__(self, config):
        super().__init__(config)
        self.ak = self.credentials.get("access_key") or self.credentials.get("AccessKey")
        self.sk = self.credentials.get("secret_key") or self.credentials.get("SecretKey")
        self.host = self.settings.get("host", "visual.volcengineapi.com")
        self.region = self.settings.get("region", "cn-north-1")
        self.service = self.settings.get("service", "visual")
        self.action = self.settings.get("action", "OCRGeneral")
        self.version = self.settings.get("version", "2022-08-31")

    async def recognize(self, image, mime, engine, options):
        body = json.dumps({"image": self._b64(image)}, ensure_ascii=False)
        x_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        auth, _ = _volcano_authorization(
            self.ak, self.sk, self.host, body, self.region, self.service, x_date
        )
        url = f"https://{self.host}/?Action={self.action}&Version={self.version}"
        headers = {
            "Host": self.host,
            "X-Date": x_date,
            "Content-Type": "application/json",
            "Authorization": auth,
        }
        resp = await self._request("POST", url, data=body, headers=headers)
        return extract(resp.json(), self.settings)
