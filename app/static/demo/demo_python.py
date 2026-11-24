#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OCR 中转服务 · Python 调用示例

依赖： pip install requests

用法：
    export OCR_BASE_URL=http://127.0.0.1:8010
    export OCR_API_KEY=ocr_sk_xxxxxxxxxxxxxxxx
    python demo_python.py ./test.png

说明：
    - API Key 在管理后台「API Key 管理」中创建，明文仅展示一次。
    - provider 留空则按「默认标记 → 降级权重 → 优先级」自动路由并自动降级。
    - engine 依提供商而定，如 general / accurate_basic / idcard / handwriting。
"""
import base64
import os
import sys

try:
    import requests
except ImportError:
    print("缺少依赖，请先执行： pip install requests")
    sys.exit(1)

BASE_URL = os.getenv("OCR_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
API_KEY = os.getenv("OCR_API_KEY", "").strip()
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def _handle(r):
    """统一处理返回：非 2xx 时抛出可读错误。"""
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"调用失败 HTTP {r.status_code}: {detail}")
    return r.json()


def recognize_file(path, provider=None, engine=None, options=None):
    """方式一（推荐）：文件上传。适合本地图片、扫描件。"""
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f.read(), "application/octet-stream")}
        data = {}
        if provider:
            data["provider"] = provider
        if engine:
            data["engine"] = engine
        if options:
            data["options"] = options  # 必须是 JSON 字符串
        r = requests.post(
            f"{BASE_URL}/v1/ocr/recognize/file",
            headers=HEADERS,
            files=files,
            data=data,
            timeout=60,
        )
    return _handle(r)


def recognize_base64(path, provider=None, engine=None, with_raw=False):
    """方式二：base64 JSON。适合图片已在内存/数据库中的场景。"""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {"image_base64": b64, "with_raw": with_raw}
    if provider:
        payload["provider"] = provider
    if engine:
        payload["engine"] = engine
    r = requests.post(
        f"{BASE_URL}/v1/ocr/recognize",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    return _handle(r)


def recognize_url(url, provider=None, engine=None):
    """方式三：传图片 URL，由中转服务代为下载后识别。"""
    payload = {"image_url": url}
    if provider:
        payload["provider"] = provider
    if engine:
        payload["engine"] = engine
    r = requests.post(
        f"{BASE_URL}/v1/ocr/recognize",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    return _handle(r)


def list_types():
    """列出当前服务支持的提供商类型（需带 API Key）。"""
    r = requests.get(f"{BASE_URL}/v1/ocr/types", headers=HEADERS, timeout=20)
    return _handle(r)


def show(tag, res):
    print(f"\n===== {tag} =====")
    print(f"provider={res.get('provider')}  engine={res.get('engine')}  "
          f"耗时={res.get('cost_ms')}ms  字符数={res.get('char_count')}  "
          f"请求ID={res.get('request_id')}")
    text = res.get("text", "")
    print("---- 识别文本（前 800 字）----")
    print(text[:800] if text else "(空)")
    blocks = res.get("blocks") or []
    if blocks:
        print("---- 区块样例（前 3 条）----")
        for b in blocks[:3]:
            print({k: b.get(k) for k in ("type", "text", "bbox", "confidence")})


def main():
    if not API_KEY:
        print("请先设置环境变量 OCR_API_KEY（在管理后台「API Key 管理」中创建）")
        sys.exit(1)
    path = sys.argv[1] if len(sys.argv) > 1 else "test.png"
    if not os.path.exists(path):
        print(f"图片不存在：{path}")
        sys.exit(1)

    print(f"服务地址：{BASE_URL}")
    show("方式一：文件上传", recognize_file(path))
    show("方式二：base64 JSON", recognize_base64(path))
    try:
        show("方式三：图片 URL", recognize_url("https://example.com/test.png"))
    except Exception as e:
        print(f"\n（方式三需外网可达的图片地址，本次跳过：{e}）")


if __name__ == "__main__":
    main()
