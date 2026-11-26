"""出站参数归一化测试（2026-11-26 生产故障回归）。

故障背景：agent（Claude Code 等）走 /v1/chat/completions 发来
``thinking`` + ``max_tokens``（可达 200000），网关原样转发给 OpenAI 兼容上游
（tokenbom / deepseek-v4.1-flash），上游报 400：
  * ``field MaxTokens invalid, should be in [1, 65536]``
  * ``"thinking" is not supported ... Use "reasoning_effort"``
近期生产日志 115 条因此失败。

本测试锁死：
1. 纯函数：max_tokens 裁剪、非法值丢弃、max_completion_tokens 合并、
   thinking→reasoning_effort 映射、Anthropic 出站保留 thinking。
2. 全链路：OpenAI 入站携带 thinking + 超大 max_tokens 时，上游实际收到的
   body 已被归一化，且客户端拿到 200 成功（离线 MockTransport）。

运行：.venv\\Scripts\\python.exe -m tests.test_param_normalize
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_param_normalize.db")
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
from app.services.llm.normalize import (  # noqa: E402
    DEFAULT_MAX_TOKENS_CAP,
    normalize_internal_request,
)

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


# =====================================================================
# 1. 纯函数边界
# =====================================================================
print("\n== 纯函数：max_tokens ==")
n = normalize_internal_request({"model": "m", "max_tokens": 200000})
expect(n["max_tokens"] == DEFAULT_MAX_TOKENS_CAP, "超大 max_tokens 裁剪到 65536", str(n))

n = normalize_internal_request({"model": "m", "max_tokens": 8192})
expect(n["max_tokens"] == 8192, "合规 max_tokens 原样保留", str(n))

n = normalize_internal_request({"model": "m", "max_tokens": 0})
expect("max_tokens" not in n, "max_tokens=0 非法值丢弃")

n = normalize_internal_request({"model": "m", "max_tokens": -5})
expect("max_tokens" not in n, "max_tokens 负数丢弃")

n = normalize_internal_request({"model": "m", "max_tokens": "abc"})
expect("max_tokens" not in n, "max_tokens 非数字丢弃")

n = normalize_internal_request({"model": "m", "max_tokens": True})
expect("max_tokens" not in n, "max_tokens=bool 丢弃（bool 不是合法整数）")

n = normalize_internal_request({"model": "m", "max_tokens": 100})
expect(n["max_tokens"] == 100, "cap=None(Anthropic 出站) 时大值不裁剪",
       str(normalize_internal_request({"max_tokens": 200000}, outbound_anthropic=True)))
expect(normalize_internal_request({"max_tokens": 200000}, outbound_anthropic=True)["max_tokens"] == 200000,
       "Anthropic 出站保留 200000 max_tokens")

print("\n== 纯函数：max_completion_tokens 合并 ==")
n = normalize_internal_request({"model": "m", "max_completion_tokens": 4096})
expect(n.get("max_tokens") == 4096 and "max_completion_tokens" not in n,
       "max_completion_tokens 合并为 max_tokens", str(n))

n = normalize_internal_request({"model": "m", "max_tokens": 1000, "max_completion_tokens": 9000})
expect(n.get("max_tokens") == 1000 and "max_completion_tokens" not in n,
       "两者同时存在时以 max_tokens 为准", str(n))

n = normalize_internal_request({"model": "m", "max_completion_tokens": 200000})
expect(n.get("max_tokens") == DEFAULT_MAX_TOKENS_CAP, "max_completion_tokens 超大也裁剪", str(n))

print("\n== 纯函数：thinking ==")
n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "enabled", "budget_tokens": 32000},
})
expect("thinking" not in n and n.get("reasoning_effort") == "high",
       "thinking budget=32000 → reasoning_effort=high", str(n))

n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "enabled", "budget_tokens": 5000},
})
expect(n.get("reasoning_effort") == "low", "thinking budget=5000 → low", str(n))

n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "enabled", "budget_tokens": 12000},
})
expect(n.get("reasoning_effort") == "medium", "thinking budget=12000 → medium", str(n))

n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "disabled"},
})
expect("thinking" not in n and "reasoning_effort" not in n, "thinking disabled 直接丢弃", str(n))

n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "enabled", "budget_tokens": 99999},
    "reasoning_effort": "low",
})
expect(n.get("reasoning_effort") == "low", "显式 reasoning_effort 优先，不被 thinking 覆盖", str(n))

n = normalize_internal_request({
    "model": "m",
    "thinking": {"type": "enabled", "budget_tokens": 32000},
}, outbound_anthropic=True)
expect(n.get("thinking", {}).get("type") == "enabled" and "reasoning_effort" not in n,
       "Anthropic 出站保留 thinking 原样", str(n))

print("\n== 纯函数：Anthropic 专有字段剥离 ==")
n = normalize_internal_request({
    "model": "m", "metadata": {"user_id": "u1"}, "service_tier": "auto",
})
expect("metadata" not in n and "service_tier" not in n,
       "OpenAI 出站剥离 metadata/service_tier", str(n))
n = normalize_internal_request({
    "model": "m", "metadata": {"user_id": "u1"},
}, outbound_anthropic=True)
expect("metadata" in n, "Anthropic 出站保留 metadata", str(n))

print("\n== 纯函数：不修改入参 ==")
orig = {"model": "m", "max_tokens": 200000, "thinking": {"type": "enabled", "budget_tokens": 1000}}
normalize_internal_request(orig)
expect(orig["max_tokens"] == 200000 and "thinking" in orig, "入参字典未被修改", str(orig))


# =====================================================================
# 2. 全链路：MockTransport 验证上游实际收到的 body
# =====================================================================
print("\n== 全链路：agent 请求归一化后转发 ==")
CAPTURED: dict = {}


def _handler(request: httpx.Request) -> httpx.Response:
    CAPTURED["url"] = str(request.url)
    CAPTURED["body"] = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock", "object": "chat.completion", "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录成功", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "ds-mock",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-x"},
    })
    expect(r.status_code == 201, "新建 OpenAI 兼容厂商成功", r.text)
    pid = r.json()["id"]

    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "deepseek-v4.1-flash", "provider_model": "deepseek-v4.1-flash",
    })
    expect(r.status_code == 201, "新增模型成功", r.text)

    r = client.post("/api/admin/keys", headers=H, json={"name": "agent", "allowed_services": ["llm"]})
    expect(r.status_code == 201, "创建 API Key 成功", r.text)
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    # agent 典型请求：thinking + 超大 max_tokens + metadata
    payload = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "你好"}],
        "max_tokens": 200000,
        "thinking": {"type": "enabled", "budget_tokens": 32000},
        "metadata": {"user_id": "agent-1"},
        "stream": False,
    }
    r = client.post("/v1/chat/completions", headers=KEY, json=payload)
    expect(r.status_code == 200, "归一化后请求成功返回 200", r.text[:300])

    sent = CAPTURED.get("body", {})
    expect(sent.get("max_tokens") == DEFAULT_MAX_TOKENS_CAP,
           "上游收到裁剪后的 max_tokens=65536", str(sent))
    expect("thinking" not in sent, "上游不再收到 thinking 字段", str(sent))
    expect(sent.get("reasoning_effort") == "high", "thinking 转为 reasoning_effort=high", str(sent))
    expect("metadata" not in sent, "上游不再收到 metadata 字段", str(sent))
    expect(sent.get("model") == "deepseek-v4.1-flash", "模型名正确透传", str(sent))

    # provider settings 自定义 cap
    r = client.put(f"/api/admin/providers/{pid}", headers=H, json={
        "settings": {"max_tokens_cap": 8192},
    })
    expect(r.status_code == 200, "设置自定义 max_tokens_cap 成功", r.text[:300])
    r = client.post("/v1/chat/completions", headers=KEY, json=payload)
    expect(r.status_code == 200, "自定义 cap 后请求仍成功", r.text[:300])
    expect(CAPTURED["body"].get("max_tokens") == 8192,
           "上游收到自定义裁剪上限 8192", str(CAPTURED["body"]))

print(f"\n全部通过：{len(PASSED)} 项")
