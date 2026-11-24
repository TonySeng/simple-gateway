"""通用 HTTP OCR 适配器——兼容任何自定义/私有 HTTP OCR 服务。

通过 settings 配置：
  url            必填，接入点
  method         GET/POST，默认 POST
  image_mode     json | form | multipart | body_base64
  image_field    图片字段名，默认 image
  content_type   默认 application/json
  extra_fields   固定附加字段（合并进 body/form）
  headers        静态请求头
  auth           {"type":"none|bearer|header|basic", ...}
  response_text_path  点分路径抽取文本（如 "data.text"）
"""
import base64
import json
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.normalize import extract


class GenericProvider(OCRProvider):
    type = "generic"
    label = "通用 HTTP OCR（兼容）"

    @staticmethod
    def _as_json(value, expect=dict):
        """管理后台的复杂字段以字符串提交，这里统一还原成对象。"""
        if isinstance(value, expect):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except Exception:
                return expect()
            return parsed if isinstance(parsed, expect) else expect()
        return expect()

    def __init__(self, config):
        super().__init__(config)
        self.url = self.settings.get("url")
        self.method = (self.settings.get("method") or "POST").upper()
        self.image_field = self.settings.get("image_field", "image")
        self.image_mode = self.settings.get("image_mode", "json")
        self.content_type = self.settings.get("content_type", "application/json")
        self.extra_fields = self._as_json(self.settings.get("extra_fields"))
        self.static_headers = self._as_json(self.settings.get("headers"))
        self.auth = self._as_json(self.settings.get("auth"))
        # 兼容后台表单的扁平写法 auth_type / auth_token
        if not self.auth and self.settings.get("auth_type"):
            kind = str(self.settings.get("auth_type") or "none").lower()
            token = str(self.settings.get("auth_token") or "")
            if kind == "bearer":
                self.auth = {"type": "bearer", "token": token}
            elif kind == "basic":
                user, _, pwd = token.partition(":")
                self.auth = {"type": "basic", "username": user, "password": pwd}
            elif kind == "header":
                self.auth = {
                    "type": "header",
                    "header_name": "Authorization",
                    "header_value": token,
                }
        self.response_text_path = self.settings.get("response_text_path")

    def _build_headers(self):
        headers = dict(self.static_headers)
        t = self.auth.get("type")
        if t == "bearer":
            headers["Authorization"] = f"Bearer {self.auth.get('token')}"
        elif t == "header":
            headers[self.auth.get("header_name", "Authorization")] = self.auth.get(
                "header_value", ""
            )
        elif t == "basic":
            raw = f"{self.auth.get('username','')}:{self.auth.get('password','')}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return headers

    async def recognize(self, image, mime, engine, options):
        if not self.url:
            raise RuntimeError("通用 OCR 缺少 settings.url")
        headers = self._build_headers()

        if self.image_mode == "body_base64":
            resp = await self._request(
                self.method, self.url, content=self._b64(image),
                headers={**headers, "Content-Type": "text/plain"},
            )
        elif self.image_mode == "form":
            data = {self.image_field: self._b64(image), **self.extra_fields}
            resp = await self._request(self.method, self.url, data=data, headers=headers)
        elif self.image_mode == "multipart":
            files = {self.image_field: ("file", image, mime or "application/octet-stream")}
            resp = await self._request(
                self.method, self.url, data=self.extra_fields, files=files, headers=headers
            )
        else:  # json
            payload = {self.image_field: self._b64(image), **self.extra_fields}
            if options.get("image_url"):
                payload["url"] = options["image_url"]
            resp = await self._request(
                self.method, self.url, json=payload,
                headers={**headers, "Content-Type": "application/json"},
            )

        try:
            js = resp.json()
        except Exception:
            js = {"text": resp.text}
        # 部分私有服务用外层 state 表达失败（如 {"state":{"success":false,"code":-1}}），
        # 若不拦截会被当成「识别成功但无文字」，既掩盖故障又让降级失效。
        if isinstance(js, dict):
            st = js.get("state")
            if isinstance(st, dict):
                code = st.get("code")
                if st.get("success") is False or (code not in (None, 0, 200)):
                    raise RuntimeError(
                        f"{self.label} 上游返回失败：code={code} failed={st.get('failed')}"
                    )
        if self.response_text_path:
            return extract(js, {"text_path": self.response_text_path})
        return extract(js, self.settings)
