"""百度智能云 OCR 适配器（access token + form 调用）。"""
import time
from typing import Any, Optional

from app.services.ocr.providers.base import OCRProvider

ENGINE_PATHS = {
    "general": "general_basic",
    "accurate": "accurate_basic",
    "idcard": "idcard",
    "bizlicense": "business_license",
    "bankcard": "bankcard",
    "handwriting": "handwriting",
    "plate": "license_plate",
    "invoice": "invoice",
    "table": "table",
    "form": "form",
}

# 简单 token 缓存（进程内；多实例应换共享缓存）
_token_cache: dict[str, tuple[float, str]] = {}


class BaiduProvider(OCRProvider):
    type = "baidu"
    label = "百度智能云 OCR"
    required_credentials = ("api_key", "secret_key")

    def __init__(self, config):
        super().__init__(config)
        self.api_key = self.credentials.get("api_key") or self.credentials.get("APIKey")
        self.secret_key = self.credentials.get("secret_key") or self.credentials.get("SecretKey")
        self.app_id = (
            self.credentials.get("app_id")
            or self.credentials.get("appid")
            or self.settings.get("app_id")
        )

    async def _get_token(self) -> str:
        cache_key = f"{self.api_key}:{self.secret_key}"
        now = time.time()
        if cache_key in _token_cache and _token_cache[cache_key][0] > now:
            return _token_cache[cache_key][1]
        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        }
        resp = await self._request("GET", url, params=params)
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError(f"Baidu token 获取失败: {resp.text[:200]}")
        _token_cache[cache_key] = (now + 3600 * 24, token)
        return token

    async def recognize(self, image, mime, engine, options):
        token = await self._get_token()
        # 支持管理页配置的完整接口地址（如 .../accurate_basic）；留空则按引擎自动构造
        url = self.settings.get("url")
        if url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={token}"
        else:
            path = self.settings.get("path") or ENGINE_PATHS.get(
                (engine or "").lower(), "general_basic"
            )
            url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/{path}?access_token={token}"
        data = {"image": self._b64(image)}
        if options.get("image_url"):
            data = {"url": options["image_url"]}
        elif options.get("id_card_side"):
            data["id_card_side"] = options["id_card_side"]
        # 注意：百度通过 access_token 识别应用，app_id 不放入请求体
        resp = await self._request(
            "POST", url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data_json = resp.json()
        if "error_code" in data_json:
            raise RuntimeError(f"Baidu OCR error: {data_json.get('error_msg')}")
        words = []
        blocks = []
        # 通用/手写等返回 words_result[{words}]
        if "words_result" in data_json and isinstance(data_json["words_result"], list):
            for i, w in enumerate(data_json["words_result"]):
                txt = w.get("words", "")
                words.append(txt)
                blocks.append({"type": "text", "text": txt, "index": i})
        # 表格/表单等可能返回 result 字符串
        elif "result" in data_json:
            txt = data_json["result"]
            if isinstance(txt, list):
                txt = "\n".join(str(t) for t in txt)
            words.append(str(txt))
            blocks.append({"type": "text", "text": str(txt), "index": 0})
        return {
            "text": "\n".join(words),
            "blocks": blocks,
            "pages": 1,
            "char_count": sum(len(b["text"]) for b in blocks),
        }
