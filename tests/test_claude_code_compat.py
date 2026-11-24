"""Claude Code 接入兼容性测试（离线 MockTransport）。

覆盖两类真实踩坑：

1. ``ANTHROPIC_BASE_URL`` 多带一层 ``/v1`` → 请求变成 ``/v1/v1/messages``。
   Anthropic SDK 会自己在 base_url 后拼 ``/v1/messages``，用户按 OpenAI 习惯
   把 base_url 写成 ``http://host:8020/v1`` 时必然 404，而 Claude Code 把这类
   404 显示成「模型可能不存在」。网关需要自动折叠重复的 ``/v1``。
2. Claude Code 会调用 ``POST /v1/messages/count_tokens`` 做上下文预算，
   该端点缺失时同样返回 404（也会被误报成模型不可用）。

运行：.venv\\Scripts\\python.exe -m tests.test_claude_code_compat
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_claude_code_compat.db")
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

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


def _mock_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content or b"{}")
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

    # ---------- 准备：管理员 + 厂商 + 模型 + Key ----------
    print("\n== 准备数据 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录成功", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "xfyun-cc", "name": "cc-cc",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-x"},
    })
    expect(r.status_code == 201, "新建厂商成功", r.text)
    pid = r.json()["id"]

    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "claude-opus-5", "provider_model": "claude-opus-5",
    })
    expect(r.status_code == 201, "新增模型 claude-opus-5 成功", r.text)

    r = client.post("/api/admin/keys", headers=H, json={"name": "cc", "allowed_services": ["llm"]})
    expect(r.status_code == 201, "创建 API Key 成功", r.text)
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    BODY = {"model": "claude-opus-5", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}

    # ---------- 1. 标准路径仍然可用（不能被中间件改坏） ----------
    print("\n== 标准路径 ==")
    r = client.post("/v1/messages", headers=KEY, json=BODY)
    expect(r.status_code == 200 and r.json().get("type") == "message", "/v1/messages 正常", r.text)

    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 200, "/v1/chat/completions 正常", r.text)

    # ---------- 2. 重复 /v1 自动折叠（Claude Code 报错的真正原因） ----------
    print("\n== 重复 /v1 折叠 ==")
    r = client.post("/v1/v1/messages", headers=KEY, json=BODY)
    expect(r.status_code == 200 and r.json().get("type") == "message",
           "POST /v1/v1/messages 被折叠后命中（不再 404）", r.text)

    r = client.post("/v1/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 200, "POST /v1/v1/chat/completions 被折叠后命中", r.text)

    r = client.get("/v1/v1/models", headers=KEY)
    expect(r.status_code == 200 and "data" in r.json(), "GET /v1/v1/models 被折叠后命中", r.text)

    # 连续多次重复也应折叠
    r = client.post("/v1/v1/v1/messages", headers=KEY, json=BODY)
    expect(r.status_code == 200, "/v1/v1/v1/messages 多次重复也能折叠", r.text)

    # 不存在的路径依旧 404（不能把 404 吞掉）
    r = client.post("/v1/v1/nonexistent", headers=KEY, json=BODY)
    expect(r.status_code == 404, "无效路径仍返回 404", r.text)

    # ---------- 3. count_tokens 端点 ----------
    print("\n== count_tokens ==")
    r = client.post("/v1/messages/count_tokens", headers=KEY, json=BODY)
    expect(r.status_code == 200, "POST /v1/messages/count_tokens 可用", r.text)
    n1 = r.json().get("input_tokens", 0)
    expect(isinstance(n1, int) and n1 > 0, "返回正整数 input_tokens", str(r.json()))

    r = client.post("/v1/v1/messages/count_tokens", headers=KEY, json=BODY)
    expect(r.status_code == 200 and r.json().get("input_tokens") == n1,
           "重复 /v1 下的 count_tokens 同样可用", r.text)

    # 更长/中文文本应估算出更多 token
    long_body = {"model": "claude-opus-5", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "请把下面这段话翻译成英文并解释其中的技术术语含义。" * 20}]},
    ], "system": "你是一个严谨的技术助手。"}
    r = client.post("/v1/messages/count_tokens", headers=KEY, json=long_body)
    expect(r.status_code == 200 and r.json()["input_tokens"] > n1 * 10,
           "长文本估算随内容增长", str(r.json()))

    # 畸形 body 不应 500
    r = client.post("/v1/messages/count_tokens",
                    headers={**KEY, "Content-Type": "application/json"}, content=b"not-json")
    expect(r.status_code in (200, 400, 422), "畸形 body 不返回 500", f"{r.status_code} {r.text}")

    # 未带 Key 应 401
    r = client.post("/v1/messages/count_tokens", json=BODY)
    expect(r.status_code == 401, "count_tokens 无 Key 时返回 401", r.text)

    print(f"\n✅ 全部通过：{len(PASSED)} 项")
