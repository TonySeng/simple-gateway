"""上游状态码透传测试。

背景（2026-11-10 真实故障）：讯飞CC 对 `claude-fable-5` 返回 503
「分组 IT-CC-L1-K 下模型 claude-fable-5 无可用渠道」，而网关把所有上游异常
一律折叠成 502，客户端只看到笼统 502，必须登后台翻日志才知道真实原因。

本测试锁死两条规则：
1. `client_status()` 纯函数边界：4xx/5xx 原样透传，其余统一 502。
2. 全链路：非流式与流式都透传；传输层异常（连不上/超时）仍为 502，
   因为那才是真正的「网关无法从上游取到响应」。

运行：.venv\\Scripts\\python.exe -m tests.test_upstream_status
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_upstream_status.db")
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


# 讯飞CC 真实返回体（new-api 风格），用于验证中文错误原样传给客户端
XF_ERROR = {
    "error": {
        "code": "model_not_found",
        "message": "分组 IT-CC-L1-K 下模型 claude-fable-5 无可用渠道（distributor）",
        "type": "new_api_error",
    }
}

STATE = {"mode": "ok", "code": 503}
OK_JSON = {
    "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


def _handler(request: httpx.Request) -> httpx.Response:
    mode = STATE["mode"]
    if mode == "upstream_error":
        # 手动编码：httpx 的 json= 会做 ensure_ascii 转义，看不出中文是否原样透传
        return httpx.Response(
            STATE["code"],
            content=json.dumps(XF_ERROR, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
    if mode == "connect_error":
        raise httpx.ConnectError("connection refused", request=request)
    return httpx.Response(200, json=OK_JSON)


# ---------- 1. 纯函数边界（无 IO，确定性） ----------
print("\n== client_status 纯函数边界 ==")
for code in (400, 401, 403, 404, 422, 429, 500, 502, 503, 504, 599):
    expect(llm_proxy.client_status(code) == code, f"上游 {code} 原样透传",
           str(llm_proxy.client_status(code)))
for code in (-1, 0, 100, 200, 204, 301, 304, 399, 600, 999):
    expect(llm_proxy.client_status(code) == 502, f"上游 {code} 折叠为 502",
           str(llm_proxy.client_status(code)))

client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    print("\n== 准备数据 ==")
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

    r = client.post("/api/admin/keys", headers=H, json={"name": "k1", "allowed_services": ["llm"]})
    expect(r.status_code == 201, "创建 Key 成功", r.text)
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    BODY = {"model": "m1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}

    def last_log():
        rr = client.get("/api/admin/logs?page_size=1", headers=H)
        items = rr.json().get("items") or []
        expect(bool(items), "存在至少一条日志", rr.text)
        row = items[0]
        dr = client.get(f"/api/admin/logs/{row['id']}", headers=H)
        return {**row, **dr.json()}

    # ---------- 2. 非流式上游 503 ----------
    print("\n== 非流式上游 503（讯飞CC 真实场景） ==")
    STATE.update(mode="upstream_error", code=503)
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 503, "客户端收到 503，而不是 502", f"{r.status_code} {r.text[:150]}")
    expect("无可用渠道" in r.text, "上游中文错误原文传给客户端", r.text[:200])
    err = (r.json().get("error") or {})
    expect(err.get("type") == "upstream_status",
           "响应体 error.type=upstream_status（供客户端归因）", json.dumps(err, ensure_ascii=False)[:200])

    log = last_log()
    expect(log["status"] == "error", "日志状态 error", json.dumps(log, ensure_ascii=False))
    expect(log["error_type"] == "upstream_status", "error_type=upstream_status", log["error_type"])
    expect(log["upstream_status"] == 503, "日志 upstream_status=503", str(log["upstream_status"]))
    expect(log["http_status"] == 503, "日志 http_status 与对外状态码一致", str(log["http_status"]))

    # ---------- 3. 流式上游 503 ----------
    print("\n== 流式上游 503 ==")
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 503, "流式同样透传 503", f"{r.status_code} {r.text[:150]}")
    log = last_log()
    expect(log["upstream_status"] == 503, "流式日志记录 503", str(log["upstream_status"]))
    expect(log["http_status"] == 503, "流式日志 http_status=503", str(log["http_status"]))

    # ---------- 4. 其他 4xx 透传 ----------
    print("\n== 其他状态码透传 ==")
    for code in (400, 404, 429):
        STATE.update(mode="upstream_error", code=code)
        r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
        expect(r.status_code == code, f"上游 {code} 透传给客户端", f"{r.status_code}")
        log = last_log()
        expect(log["upstream_status"] == code, f"日志记录 {code}", str(log["upstream_status"]))

    # ---------- 5. 传输层异常仍是 502 ----------
    print("\n== 连不上上游仍为 502 ==")
    STATE["mode"] = "connect_error"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 502, "连接失败返回 502（网关自身取不到响应）", r.status_code)
    log = last_log()
    expect(log["error_type"] == "upstream_connect", "error_type=upstream_connect", log["error_type"])

print(f"\n✅ 全部通过：{len(PASSED)} 项")
