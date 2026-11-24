"""通用 HTTP OCR 适配器的私有化部署兼容性测试。

覆盖图聆云 UOCR（``/tuling/uocr/v2/recognize``）这类私有服务的三个坑：

* 只收 ``multipart/form-data``（用 JSON 调用会 415）；
* 必填 ``category`` / ``trackId``，且 ``category`` 传空串才是「自动识别」；
* 结果二次编码在 ``body`` 字符串里，真正的文本在 ``pages[].lines[].content``。

运行：.venv\\Scripts\\python.exe -m tests.test_generic_uocr
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services.ocr.providers import normalize  # noqa: E402
from app.services.ocr.providers.generic import GenericProvider  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if cond:
        PASSED.append(label)
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}\n     {detail}")
        raise SystemExit(1)


class FakeProvider:
    """够 GenericProvider 用的最小 Provider 替身。"""

    id = "p-generic"
    name = "移动云152OCR"
    type = "generic"
    service = "ocr"
    credentials_enc = None
    base_url = ""
    is_default = False
    priority = 100
    weight = 100

    def __init__(self, settings: dict):
        self.settings = settings


class FakeResp:
    def __init__(self, payload, text=""):
        self._payload = payload
        self.text = text
        self.status_code = 200

    def json(self):
        return self._payload


# 真实响应的结构（截取关键部分）：body 是二次编码的 JSON 字符串
INNER = {
    "category": "atlas.doc",
    "pages": [
        {
            "angle": 0.0,
            "width": 1500,
            "height": 1100,
            "blocks": [{"coord": [{"x": 1, "y": 2}], "id": 1, "line_ids": [0]}],
            "lines": [
                {
                    "conf": 0.97,
                    "content": "Simple Gateway v1.0.0",
                    "coord": [
                        {"x": 46, "y": 30},
                        {"x": 45, "y": 47},
                        {"x": 23, "y": 46},
                        {"x": 25, "y": 29},
                    ],
                    "type": "print",
                },
                {"conf": 0.91, "content": "新建Key", "coord": [{"x": 0, "y": 0}]},
            ],
        }
    ],
    "protoc": "2.3",
    "version": "4.1.2.4053",
}

TULING_RESP = {"state": {"code": 0, "success": True, "ok": 1, "failed": None}, "body": json.dumps(INNER, ensure_ascii=False)}

MULTIPART_SETTINGS = {
    "url": "http://223.111.149.152:8090/tuling/uocr/v2/recognize",
    "image_mode": "multipart",
    "image_field": "picFile",
    "extra_fields": '{"category": "", "trackId": "gw"}',
}


def main() -> None:
    print("[1] 解析二次编码的响应体（body 字符串 -> pages[].lines[].content）")
    out = normalize.extract(TULING_RESP)
    expect(out["text"] == "Simple Gateway v1.0.0\n新建Key", "文本按行拼接正确", repr(out["text"]))
    expect(len(out["blocks"]) == 2, "区块数与 lines 一致", str(len(out["blocks"])))
    expect(out["blocks"][0]["bbox"] == [23.0, 29.0, 46.0, 47.0], "坐标折算成 bbox", str(out["blocks"][0]))
    expect(out["blocks"][0]["confidence"] == 0.97, "置信度透传", str(out["blocks"][0]["confidence"]))
    expect(out["pages"] == 1, "页数取 pages 长度", str(out["pages"]))

    print("\n[2] category 非空导致 pages 为 null 时不虚构文本")
    empty = {"state": {"code": 0, "success": True}, "body": json.dumps({"pages": None})}
    out = normalize.extract(empty)
    expect(out["text"] == "", "无 lines 时文本为空", repr(out["text"]))
    expect(out["blocks"] == [], "无区块", str(out["blocks"]))

    print("\n[3] 不破坏既有厂商结构（百度 words_result）")
    out = normalize.extract({"words_result": [{"words": "甲"}, {"words": "乙"}]})
    expect(out["text"] == "甲\n乙", "百度结构仍可解析", repr(out["text"]))

    print("\n[4] text_path 优先级高于 pages 自动解析")
    out = normalize.extract(TULING_RESP, {"text_path": "state.ok"})
    expect(out["text"] == "1", "显式 text_path 生效", repr(out["text"]))

    print("\n[5] multipart 请求构造（文件名、附加字段、空 category）")
    captured: dict = {}

    async def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured.update(kwargs)
        return FakeResp(TULING_RESP)

    prov = GenericProvider(FakeProvider(dict(MULTIPART_SETTINGS)))
    prov._request = fake_request  # type: ignore[method-assign]
    res = asyncio.run(prov.recognize(b"\x89PNG\r\n\x1a\n", "image/png", None, {}))

    expect(captured["method"] == "POST", "使用 POST", str(captured.get("method")))
    expect(set(captured.get("files") or {}) == {"picFile"}, "图片字段名取自 image_field", str(captured.get("files", {}).keys()))
    expect("json" not in captured, "不再发送 JSON body", str(list(captured)))
    data = captured.get("data") or {}
    expect(data.get("category") == "", "category 为空串（自动语种）", repr(data.get("category")))
    expect(data.get("trackId") == "gw", "trackId 已提交", repr(data.get("trackId")))
    expect(prov.extra_fields == {"category": "", "trackId": "gw"}, "字符串形式的 extra_fields 被还原成对象", str(prov.extra_fields))
    expect(res["text"].startswith("Simple Gateway"), "识别文本可正常返回", repr(res["text"])[:80])

    print("\n[6] 外层 state 表示失败时必须抛错（否则被当成“成功但无文字”）")
    async def fake_fail(method, url, **kwargs):
        return FakeResp({"state": {"code": -1, "success": False, "ok": 0, "failed": "decode error"}})

    prov2 = GenericProvider(FakeProvider(dict(MULTIPART_SETTINGS)))
    prov2._request = fake_fail  # type: ignore[method-assign]
    try:
        asyncio.run(prov2.recognize(b"\x89PNG", "image/png", None, {}))
    except RuntimeError as e:
        expect("failed" in str(e) or "code" in str(e), "抛出可读的上游失败", str(e))
    else:
        expect(False, "应抛 RuntimeError", "未抛出，失败会被静默吞掉")

    print("\n[7] 扁平 auth_type/auth_token 兼容（后台表单写法）")
    prov3 = GenericProvider(
        FakeProvider({"url": "http://x/y", "auth_type": "bearer", "auth_token": "tk-123"})
    )
    expect(prov3.auth.get("type") == "bearer" and prov3.auth.get("token") == "tk-123",
           "auth_type/auth_token 组装成 auth 对象", str(prov3.auth))
    prov4 = GenericProvider(
        FakeProvider({"url": "http://x/y", "auth_type": "basic", "auth_token": "u:p"})
    )
    expect(prov4.auth.get("username") == "u" and prov4.auth.get("password") == "p",
           "basic 拆分用户名口令", str(prov4.auth))

    print(f"\n✅ 全部通过：{len(PASSED)} 项")


if __name__ == "__main__":
    main()
