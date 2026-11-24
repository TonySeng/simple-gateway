"""讯飞CC 兼容类型 + API Key 指定渠道 的单元测试（无需真实厂商密钥）。

运行：.venv\\Scripts\\python.exe -m tests.test_xfyun_cc
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_xfyun_cc.db")
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

from app.core.models import Model, Provider  # noqa: E402
from app.core.providers import set_credentials  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm import dispatch, proxy as llm_proxy  # noqa: E402
from app.services.ocr.providers import base as ocr_base  # noqa: E402

PASSED: list[str] = []


def ok(label: str) -> None:
    PASSED.append(label)
    print(f"  [PASS] {label}")


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    ok(label)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    body = json.loads(request.content or b"{}")
    if body.get("stream"):
        sse = (
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"}}]}\n\n'
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock", "object": "chat.completion", "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    ocr_base._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))

    # ---------- 1. 讯飞CC 类型已注册 ----------
    print("\n== 讯飞CC 类型 ==")
    r = client.get("/api/admin/config")
    types = [t["type"] for s in r.json()["services"] if s["key"] == "llm" for t in s["provider_types"]]
    expect("xfyun-cc" in types, "llm 服务已注册 xfyun-cc 类型", str(types))
    xf = next(t for s in r.json()["services"] if s["key"] == "llm" for t in s["provider_types"] if t["type"] == "xfyun-cc")
    expect(xf["label"].startswith("讯飞CC"), "类型标签正确")
    expect(any(f["name"] == "api_key" for f in xf["fields"]), "凭证字段含 api_key")

    # ---------- 2. build_outbound：xfyun-cc 默认 OpenAI 出站 ----------
    print("\n== 出站协议 ==")
    p_open = Provider(service="llm", type="xfyun-cc", name="cc",
                      base_url="https://one.iflytek.com/api/llm/console/chat/v1")
    set_credentials(p_open, {"api_key": "sk-x"})
    m = Model(name="claude-x", provider_model="claude-x")
    url, headers, _body, proto = dispatch.build_outbound(p_open, m, {"model": "claude-x", "messages": [], "stream": False})
    expect(proto == "openai", "xfyun-cc 默认出站协议为 openai", proto)
    expect(url.endswith("/chat/completions"), "OpenAI 出站路径为 /chat/completions", url)
    expect(headers.get("Authorization", "").startswith("Bearer "), "OpenAI 出站带 Authorization: Bearer")

    # 切到 anthropic 出站（高级设置 upstream_protocol）
    p_anth = Provider(service="llm", type="xfyun-cc", name="cc2",
                      base_url="https://one.iflytek.com/api/llm/console/chat/v1",
                      settings={"upstream_protocol": "anthropic"})
    set_credentials(p_anth, {"api_key": "sk-x"})
    url2, headers2, _b2, proto2 = dispatch.build_outbound(p_anth, m, {"model": "claude-x", "messages": []})
    expect(proto2 == "anthropic", "upstream_protocol=anthropic 时出站协议为 anthropic", proto2)
    expect(url2.endswith("/messages"), "Anthropic 出站路径为 /messages", url2)
    expect("x-api-key" in headers2, "Anthropic 出站带 x-api-key")

    # ---------- 3. 管理员登录 ----------
    print("\n== 登录 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "正确口令登录成功", r.text)
    token = r.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    # ---------- 4. 创建讯飞CC 厂商 + 模型 ----------
    print("\n== 厂商/模型 ==")
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "xfyun-cc", "name": "xfyun-cc-1",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-x"},
    })
    expect(r.status_code == 201, "新建 xfyun-cc 厂商成功", r.text)
    cc_pid = r.json()["id"]
    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": cc_pid, "name": "claude-x", "provider_model": "claude-x",
    })
    expect(r.status_code == 201, "为 xfyun-cc 新增模型成功", r.text)

    # ---------- 5. API Key 指定渠道：校验 + 路由 ----------
    print("\n== API Key 指定渠道 ==")
    # 5a 合法渠道
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "pinned", "allowed_services": ["llm"], "channel": cc_pid,
    })
    expect(r.status_code == 201, "创建绑定渠道的 Key 成功", r.text)
    expect(r.json().get("channel") == cc_pid, "Key 回显 channel 正确")
    key_pin = r.json()["key"]
    key_pin_id = r.json()["id"]

    # 5b 不存在的渠道 → 400
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "bad", "allowed_services": ["llm"], "channel": "deadbeef" * 4,
    })
    expect(r.status_code == 400, "不存在的渠道被拦截(400)")

    # 5c 渠道服务不在授权范围内 → 400
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "scope", "allowed_services": ["ocr"], "channel": cc_pid,
    })
    expect(r.status_code == 400, "渠道服务超出 Key 授权范围被拦截(400)")

    # 5d 渠道约束路由：模型不在渠道内 → 404
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key_pin}"}, json={
        "model": "not-in-channel", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5,
    })
    expect(r.status_code == 404 and "指定的渠道" in r.text, "渠道绑定后越界模型被 404 拦截", r.text)

    # 5e 渠道约束路由：OpenAI 入口走渠道 → 200
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key_pin}"}, json={
        "model": "claude-x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5,
    })
    expect(r.status_code == 200 and "你好" in r.text, "OpenAI 入口经渠道命中上游成功", r.text)

    # 5f 渠道约束路由：Anthropic 入口走渠道 → 200（网关转 OpenAI 出站）
    r = client.post("/v1/messages", headers={"Authorization": f"Bearer {key_pin}"}, json={
        "model": "claude-x", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}],
    })
    expect(r.status_code == 200 and r.json().get("type") == "message", "Anthropic 入口经渠道命中上游成功", r.text)

    # 5g 编辑 Key 清除渠道
    r = client.put(f"/api/admin/keys/{key_pin_id}", headers=H, json={"channel": None})
    expect(r.status_code == 200 and r.json().get("channel") is None, "可清除 Key 的渠道绑定")

    print(f"\n✅ 全部通过：{len(PASSED)} 项")
