"""脱敏 / 复核动作的代理链路端到端测试（离线 MockTransport）。

验证：
* 输入脱敏：发往上游的请求体里敏感信息已被掩码替换，且日志标记不降级
* 输出脱敏：返回客户端的响应体已掩码，并带 X-Safety-Review: sanitized_output
* 复核动作：请求放行、上游被调用，标记 review_input
* 拦截动作：仍不调用上游（回归）

运行：.venv\\Scripts\\python.exe -m tests.test_safety_sanitize_proxy
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_safety_sanitize.db")
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
STATE = {"reply": "正常回复", "calls": 0, "last_body": None}


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


def _handler(request: httpx.Request) -> httpx.Response:
    STATE["calls"] += 1
    try:
        STATE["last_body"] = request.content.decode("utf-8")
    except Exception:
        STATE["last_body"] = ""
    return httpx.Response(200, json={
        "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": STATE["reply"]},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录成功", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "mock-upstream",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-provider-key"},
    })
    expect(r.status_code == 201, "新建厂商成功", r.text)
    pid = r.json()["id"]
    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "m1", "provider_model": "m1",
    })
    expect(r.status_code == 201, "新增模型成功", r.text)

    # 脱敏词库：手机号正则，动作 sanitize
    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "脱敏测试库", "terms": "re:(?<!\\d)1[3-9]\\d{9}(?!\\d)",
        "risk_level": "medium", "hit_action": "sanitize",
    })
    expect(r.status_code == 201 and r.json()["hit_action"] == "sanitize", "创建脱敏词库", r.text[:200])
    sanitize_lib = r.json()["id"]
    r = client.post("/api/admin/safety/policies", headers=H, json={
        "name": "脱敏规则", "library_ids": [sanitize_lib],
        "input_enabled": True, "output_enabled": True,
    })
    expect(r.status_code == 201, "创建脱敏规则", r.text[:200])
    sanitize_policy = r.json()["id"]
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "脱敏Key", "allowed_services": ["llm"], "safety_policy_id": sanitize_policy,
    })
    expect(r.status_code == 201, "创建脱敏 Key", r.text[:200])
    KEY_SAN = {"Authorization": f"Bearer {r.json()['key']}"}

    # 复核词库
    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "复核测试库", "terms": "待复核词", "risk_level": "low", "hit_action": "review",
    })
    review_lib = r.json()["id"]
    r = client.post("/api/admin/safety/policies", headers=H, json={
        "name": "复核规则", "library_ids": [review_lib],
        "input_enabled": True, "output_enabled": False,
    })
    review_policy = r.json()["id"]
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "复核Key", "allowed_services": ["llm"], "safety_policy_id": review_policy,
    })
    KEY_REV = {"Authorization": f"Bearer {r.json()['key']}"}

    # 拦截词库（回归）
    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "拦截测试库", "terms": "违规拦截词", "risk_level": "high", "hit_action": "block",
    })
    block_lib = r.json()["id"]
    r = client.post("/api/admin/safety/policies", headers=H, json={
        "name": "拦截规则", "library_ids": [block_lib],
        "input_enabled": True, "output_enabled": False, "default_reply": "已拦截。",
    })
    block_policy = r.json()["id"]
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "拦截Key", "allowed_services": ["llm"], "safety_policy_id": block_policy,
    })
    KEY_BLK = {"Authorization": f"Bearer {r.json()['key']}"}

    BODY = {"model": "m1", "messages": [{"role": "user", "content": "你好"}]}

    print("\n== 输出脱敏 ==")
    STATE["reply"] = "你的手机号是13800138000，请查收"
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_SAN, json=BODY)
    content = r.json()["choices"][0]["message"]["content"]
    expect(r.status_code == 200, "输出脱敏返回 200", r.text[:200])
    expect("13800138000" not in content and "***" in content, "响应体已脱敏", content)
    expect(STATE["calls"] == before + 1, "脱敏不阻断上游调用", str(STATE["calls"] - before))
    expect(r.headers.get("X-Safety-Review") == "sanitized_output",
           "响应头标注输出脱敏", r.headers.get("X-Safety-Review", ""))

    print("\n== 输入脱敏（发往上游前改写）==")
    STATE["reply"] = "收到"
    r = client.post("/v1/chat/completions", headers=KEY_SAN,
                    json={**BODY, "messages": [{"role": "user", "content": "我电话13800138000"}]})
    expect(r.status_code == 200, "输入脱敏返回 200", r.text[:200])
    sent = json.loads(STATE["last_body"] or "{}")
    sent_text = json.dumps(sent, ensure_ascii=False)
    expect("13800138000" not in sent_text and "***" in sent_text,
           "上游收到的请求体已脱敏", sent_text[:300])
    expect(r.headers.get("X-Safety-Review") == "sanitized_input",
           "输入脱敏标记不被输出审查降级", r.headers.get("X-Safety-Review", ""))

    print("\n== 复核动作 ==")
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_REV,
                    json={**BODY, "messages": [{"role": "user", "content": "这里有个待复核词"}]})
    expect(r.status_code == 200 and STATE["calls"] == before + 1,
           "复核动作放行并调用上游", r.text[:200])
    expect(r.headers.get("X-Safety-Review") == "review_input",
           "复核标记正确", r.headers.get("X-Safety-Review", ""))

    print("\n== 拦截动作回归 ==")
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_BLK,
                    json={**BODY, "messages": [{"role": "user", "content": "这是违规拦截词"}]})
    expect(r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "已拦截。",
           "拦截动作返回默认回复", r.text[:200])
    expect(STATE["calls"] == before, "拦截动作不调用上游", str(STATE["calls"] - before))
    expect(r.headers.get("X-Safety-Review") == "blocked_input",
           "拦截标记正确", r.headers.get("X-Safety-Review", ""))

    print("\n== 审查记录动作落库 ==")
    r = client.get("/api/admin/safety/records?page_size=50", headers=H)
    actions = {x["action"] for x in r.json()["items"]}
    expect({"sanitized", "review", "blocked"} <= actions,
           "审查记录包含脱敏/复核/拦截三类动作", str(actions))

print(f"\n✅ 脱敏与复核代理链路测试通过：{len(PASSED)} 项断言")
