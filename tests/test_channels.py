"""按服务指定渠道（channels）的单元测试。

核心诉求：同一个 API Key 可以让「不同服务」分别钉定到「不同的上游厂商」，
例如 llm -> 讯飞CC、ocr -> 某个私有 OCR；各服务互不影响。

运行：.venv\\Scripts\\python.exe -m tests.test_channels
"""
from __future__ import annotations

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_channels.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB_PATH + suffix)
    except FileNotFoundError:
        pass
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

os.environ["SG_DATABASE_URL"] = "sqlite:///" + DB_PATH.replace("\\", "/")
os.environ["SG_ADMIN_USERNAME"] = "admin"
os.environ["SG_ADMIN_PASSWORD"] = "Test-Admin-2026!"
os.environ["SG_JWT_SECRET"] = "test-secret-0123456789-0123456789-0123"
os.environ["SG_FERNET_KEY"] = "8coHn0mS0Xk8mS3m1oQ0K3xH0k1m4r2y6V8b9N1c2Z0="

sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402
from app.services.ocr.providers import base as ocr_base  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


def _mock_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    if "/ocr" in p:
        return httpx.Response(200, json={"text": "HELLO OCR"})
    body = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    ocr_base._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))

    # ---------- 登录 ----------
    print("\n== 登录 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录成功", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # ---------- 建 LLM 厂商 + 模型 ----------
    print("\n== 建厂商 ==")
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "xfyun-cc", "name": "llm-chan",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-x"},
    })
    expect(r.status_code == 201, "建 LLM 厂商成功", r.text)
    llm_pid = r.json()["id"]
    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": llm_pid, "name": "claude-x", "provider_model": "claude-x",
    })
    expect(r.status_code == 201, "建 LLM 模型成功", r.text)

    # ---------- 建 OCR 厂商（generic，免凭证） ----------
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "generic", "name": "ocr-chan",
        "base_url": "http://mock.local/ocr",
        "settings": {"url": "http://mock.local/ocr", "text_path": "text"},
    })
    expect(r.status_code == 201, "建 OCR 厂商成功", r.text)
    ocr_pid = r.json()["id"]

    # ---------- 建 Key：两个服务分别钉不同渠道 ----------
    print("\n== 多服务渠道 Key ==")
    channels = {"llm": llm_pid, "ocr": ocr_pid}
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "multi", "allowed_services": ["llm", "ocr"], "channels": channels,
    })
    expect(r.status_code == 201, "创建多服务渠道 Key 成功", r.text)
    j = r.json()
    expect(j.get("channels") == channels, "channels 回显正确（llm/ocr 各自钉定）", str(j.get("channels")))
    expect(j.get("channel") is None, "多服务时旧 channel 字段为空", str(j.get("channel")))
    key, kid = j["key"], j["id"]

    # ---------- LLM 路由：命中 llm 渠道 ----------
    print("\n== 路由：LLM ==")
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
        "model": "claude-x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5,
    })
    expect(r.status_code == 200 and "你好" in r.text, "LLM 走 llm 渠道成功", r.text)

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
        "model": "no-such-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5,
    })
    expect(r.status_code == 404 and "指定的渠道" in r.text, "LLM 越界模型被渠道约束 404", r.text)

    # ---------- OCR 路由：命中 ocr 渠道（若误用 llm 渠道会失败） ----------
    print("\n== 路由：OCR ==")
    r = client.post(
        "/v1/ocr/recognize/file",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("a.png", PNG, "image/png")},
    )
    expect(r.status_code == 200 and "HELLO OCR" in r.text, "OCR 走 ocr 渠道成功（与 llm 渠道相互独立）", r.text)

    # ---------- 校验：渠道服务与 key 不匹配 ----------
    print("\n== 校验 ==")
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "bad", "allowed_services": ["llm", "ocr"], "channels": {"ocr": llm_pid},
    })
    expect(r.status_code == 400 and "不能指定给 ocr" in r.text, "把 LLM 厂商指定给 ocr 被拦截(400)", r.text)

    r = client.post("/api/admin/keys", headers=H, json={
        "name": "bad2", "allowed_services": ["ocr"], "channels": {"llm": llm_pid},
    })
    expect(r.status_code == 400 and "不在该 Key 授权的服务范围" in r.text, "渠道服务超出授权范围被拦截(400)", r.text)

    # ---------- 编辑：单独移除 ocr 渠道，保留 llm ----------
    print("\n== 编辑 ==")
    r = client.put(f"/api/admin/keys/{kid}", headers=H, json={"channels": {"llm": llm_pid}})
    expect(r.status_code == 200 and r.json().get("channels") == {"llm": llm_pid}, "可单独移除某服务渠道", r.text)
    expect(r.json().get("channel") == llm_pid, "单渠道时旧字段回显该值", r.text)

    r = client.put(f"/api/admin/keys/{kid}", headers=H, json={"channels": {}})
    expect(r.status_code == 200 and r.json().get("channels") == {}, "可清空全部渠道", r.text)

    print(f"\n✅ 全部通过：{len(PASSED)} 项")
