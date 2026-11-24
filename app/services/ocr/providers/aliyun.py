"""阿里云 OCR 适配器（RPC HMAC-SHA1 签名，host/action 可配置）。

说明：阿里云视觉智能开放平台存在多套接口与签名风格，本适配器以经典 RPC 签名为基础，
host/version/action 通过 settings 指定；返回结构经通用归一化提取。上线前请用真实密钥联调。
"""
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.normalize import extract
from app.services.ocr.providers.sign import aliyun_signature


class AliyunProvider(OCRProvider):
    type = "aliyun"
    label = "阿里云 OCR"
    required_credentials = ("access_key_id", "access_key_secret")

    def __init__(self, config):
        super().__init__(config)
        self.ak = self.credentials.get("access_key_id") or self.credentials.get("AccessKeyId")
        self.sk = self.credentials.get("access_key_secret") or self.credentials.get("AccessKeySecret")
        self.host = self.settings.get("host", "ocr.cn-shanghai.aliyuncs.com")
        self.version = self.settings.get("version", "2019-12-30")
        self.action = self.settings.get("action", "RecognizeCharacter")
        self.region_id = self.settings.get("region_id", "cn-shanghai")
        self.path = self.settings.get("path", "/")

    async def recognize(self, image, mime, engine, options):
        if engine:
            self.action = engine
        params = {
            "AccessKeyId": self.ak,
            "Format": "JSON",
            "Version": self.version,
            "Action": self.action,
            "RegionId": self.region_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "Image": self._b64(image),
        }
        if options.get("image_url"):
            params["ImageURL"] = options["image_url"]
        params["Signature"] = aliyun_signature(self.sk, params)
        resp = await self._request(
            "GET", f"https://{self.host}{self.path}", params=params
        )
        return extract(resp.json(), self.settings)
