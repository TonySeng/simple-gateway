"""讯飞开放平台 OCR 适配器。

支持两类接口（根据配置的接口地址自动选择）：

1) **私有 HTTP API**（通用文字识别 `sf8e6aca1`、印刷文字识别 `s00b65163` 等，推荐）
   - 鉴权参数放在 **URL 查询串**：`?host=&date=&authorization=`
   - 请求体 `application/json`：
       {"header":{"app_id":..,"status":3},
        "parameter":{"<服务ID>":{"category":..,"result":{...}}},
        "payload":{"<服务ID>_data_1":{"encoding":..,"status":3,"image":"<base64>"}}}
   - 响应 `payload.result.text` 为 base64，解码后是 pages/lines/words 结构

2) **旧版 AIUI 接口**（host/path + Authorization 头鉴权 + 表单体）
"""
import base64
import json
from typing import Any, Optional
from urllib.parse import urlsplit

from app.services.ocr.providers.base import OCRProvider
from app.services.ocr.providers.sign import iflytek_authorization, iflytek_private_authorization


def _normalize_url(u: str) -> str:
    """规范化接口地址。

    讯飞文档里地址写作 `http[s]://api.xf-yun.com/v1/private/xxx`，直接粘贴会带方括号，
    这里统一转成 https；同时容忍漏写协议头的情况（http 也统一升级为 https）。
    """
    u = (u or "").strip().replace(" ", "")
    for prefix in ("http[s]://", "http[s]:", "https[s]://"):
        if u.startswith(prefix):
            u = "https://" + u[len(prefix):]
            break
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    elif u and not u.startswith("http"):
        u = "https://" + u
    return u


# 讯飞各 OCR 能力的服务ID与其 category 取值一一对应（category 由服务端 schema 校验，写错报 10163）
SERVICE_CATEGORIES = {
    "sf8e6aca1": "ch_en_public_cloud",  # 通用文字识别（中英文）
    "s00b65163": "mix0",                 # 印刷文字识别（多语种）
}

# 常见错误码释义，便于直接给出可执行的处理建议
ERROR_HINTS = {
    10163: "请求参数不符合接口 schema（常见于 category 与服务ID不匹配）",
    10313: "appid 与 api_key 不匹配，或 appid 为空",
    11200: "功能未授权：该 APPID 下未添加此服务，请在讯飞控制台为该应用开通对应能力",
    11201: "授权额度不足：该 APPID 的每日调用次数已达上限，请次日再试，或在控制台提额/购买套餐",
}


class IflytekProvider(OCRProvider):
    type = "iflytek"
    label = "讯飞开放平台 OCR"
    required_credentials = ("api_key", "api_secret", "app_id")

    def __init__(self, config):
        super().__init__(config)
        self.api_key = self._cred("api_key")
        self.api_secret = self._cred("api_secret")
        self.app_id = self._cred("app_id")
        self.url = _normalize_url(self.settings.get("url"))
        if self.url:
            parts = urlsplit(self.url)
            self.host = parts.netloc
            self.path = parts.path or "/"
            # 服务ID：路径最后一段（/v1/private/{sid}）
            self.sid = self.path.rstrip("/").split("/")[-1]
            self.mode = "private"
        else:
            self.host = self.settings.get("host", "webapi.xfyun.cn")
            self.path = self.settings.get("path", "/v1/aiui/v1/image/ocr")
            self.sid = self.path.rstrip("/").split("/")[-1]
            self.mode = "legacy"
        # category 由服务端 schema 校验：已知服务ID用其唯一合法取值，其余取配置值
        self.category = (
            SERVICE_CATEGORIES.get(self.sid)
            or self.settings.get("category")
            or "ch_en_public_cloud"
        )

    async def recognize(self, image, mime, engine, options):
        if self.mode == "private":
            return await self._recognize_private(image, mime, engine, options)
        return await self._recognize_legacy(image, mime, engine, options)

    # ---------------- 私有 HTTP API ----------------
    async def _recognize_private(self, image, mime, engine, options):
        options = options or {}
        params = iflytek_private_authorization(
            self.api_key, self.api_secret, self.host, self.path
        )
        body = {
            "header": {"app_id": self.app_id, "status": 3},
            "parameter": {
                self.sid: {
                    "category": options.get("category") or self.category,
                    "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
                }
            },
            "payload": {
                f"{self.sid}_data_1": {
                    "encoding": self._img_encoding(mime),
                    "status": 3,
                    "image": self._b64(image),
                }
            },
        }
        # 业务参数透传（写入 parameter.<sid>）
        extra = {k: v for k, v in options.items() if k not in ("category", "app_id")}
        if extra:
            body["parameter"][self.sid].update(extra)
        if options.get("app_id"):
            body["header"]["app_id"] = options["app_id"]

        resp = await self._request(
            "POST", self.url, params=params, json=body, headers={"Accept": "application/json"}
        )
        js = self._json(resp)
        header = js.get("header") or {}
        code = header.get("code")
        if code not in (0, None):
            msg = header.get("message") or js.get("message")
            hint = ERROR_HINTS.get(code)
            raise RuntimeError(
                f"讯飞 OCR 错误 {code}：{msg}" + (f"（{hint}）" if hint else "")
            )
        result = ((js.get("payload") or {}).get("result")) or {}
        raw_text = result.get("text") or ""
        return self._parse_result_text(raw_text)

    def _parse_result_text(self, raw_text: str) -> dict:
        """解析 payload.result.text（base64 包裹的 pages/lines/words 结构）。"""
        inner = None
        if raw_text:
            try:
                inner = json.loads(base64.b64decode(raw_text).decode("utf-8"))
            except Exception:
                inner = None
        blocks: list[dict[str, Any]] = []
        texts: list[str] = []
        pages = 0
        if isinstance(inner, dict):
            for pg in inner.get("pages") or []:
                pages += 1
                for ln in pg.get("lines") or []:
                    line_conf = ln.get("conf")
                    for w in ln.get("words") or []:
                        content = w.get("content") or ""
                        if not content:
                            continue
                        texts.append(content)
                        blocks.append(
                            {
                                "type": "text",
                                "text": content,
                                "bbox": self._coord_to_bbox(w.get("coord")),
                                # 部分能力（如印刷文字识别）只在行级给 conf，词级回退到行级
                                "confidence": w.get("conf", line_conf),
                                "index": len(blocks),
                            }
                        )
        text = "".join(texts)
        return {
            "text": text,
            "blocks": blocks,
            "pages": pages or 1,
            "char_count": len(text),
        }

    @staticmethod
    def _coord_to_bbox(coord) -> Optional[list[float]]:
        """讯飞返回 4 个顶点坐标，转成 [x1,y1,x2,y2]。"""
        if not coord:
            return None
        xs = [p.get("x") for p in coord if isinstance(p, dict) and p.get("x") is not None]
        ys = [p.get("y") for p in coord if isinstance(p, dict) and p.get("y") is not None]
        if not xs or not ys:
            return None
        return [min(xs), min(ys), max(xs), max(ys)]

    @staticmethod
    def _img_encoding(mime: str) -> str:
        m = (mime or "").lower()
        if "png" in m:
            return "png"
        if "bmp" in m:
            return "bmp"
        if "gif" in m:
            return "gif"
        return "jpg"

    # ---------------- 旧版 AIUI ----------------
    async def _recognize_legacy(self, image, mime, engine, options):
        url = f"https://{self.host}{self.path}"
        headers = iflytek_authorization(self.api_key, self.api_secret, self.host, self.path)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.app_id:
            headers["X-Appid"] = self.app_id
        data = {"image": self._b64(image)}
        resp = await self._request("POST", url, data=data, headers=headers)
        js = self._json(resp)
        if js.get("code") not in (0, None) and "data" not in js:
            raise RuntimeError(f"Iflytek OCR error: {js.get('desc') or js.get('message')}")
        data_obj = js.get("data", {})
        text = data_obj.get("page_content") if isinstance(data_obj, dict) else None
        if text is None and isinstance(data_obj, list):
            text = "\n".join(str(d.get("content", "")) for d in data_obj)
        if text is None:
            text = str(data_obj)
        blocks = [{"type": "text", "text": text, "index": 0}] if text else []
        return {
            "text": text or "",
            "blocks": blocks,
            "pages": 1,
            "char_count": len(text or ""),
        }

    @staticmethod
    def _json(resp):
        """解析 JSON；非 JSON 时给出可读错误（而非 json 解析栈）。"""
        try:
            return resp.json()
        except Exception:
            body = (resp.text or "")[:200].replace("\n", " ")
            raise RuntimeError(
                f"讯飞返回非 JSON 响应（HTTP {resp.status_code}）：{body}"
            )
