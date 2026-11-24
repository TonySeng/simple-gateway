"""上游失败归因记录测试（离线 MockTransport）。

覆盖的关键回归：
1. 非流式上游非 2xx → 日志记录真实 upstream_status / 上游响应体 / error_type
2. **流式上游非 2xx** → 必须抛出错误而不是 200 + 空流，且必须落日志
   （旧实现直接吞进 event_gen，非 "data:" 行全被跳过，日志还记 success）
3. 上游 200 但无 data 事件 → empty_stream
4. 模型不存在 / 参数非法 → 也要落日志（旧实现直接 return _err，一条不留）
5. 上游响应体里的密钥必须脱敏
6. X-Request-Id 与日志 request_id 一致，可据此反查

运行：.venv\\Scripts\\python.exe -m tests.test_error_logging
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_error_logging.db")
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


STATE = {"mode": "ok"}

OK_JSON = {
    "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def _handler(request: httpx.Request) -> httpx.Response:
    mode = STATE["mode"]
    sse = {"content-type": "text/event-stream"}
    if mode == "unauthorized":
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key: sk-abcdef123456789", "type": "auth_error"}},
        )
    if mode == "rate_limit":
        return httpx.Response(429, json={"error": {"message": "quota exceeded, retry later"}})
    if mode == "not_json":
        return httpx.Response(200, text="<html><body>502 Bad Gateway</body></html>")
    if mode == "connect_error":
        raise httpx.ConnectError("connection refused", request=request)
    if mode == "timeout":
        raise httpx.ReadTimeout("read timed out", request=request)
    if mode == "stream_unauthorized":
        return httpx.Response(401, json={"error": {"message": "stream auth failed"}})
    if mode == "empty_stream":
        return httpx.Response(200, headers=sse, content=b": keep-alive\n\n")
    if mode == "stream_ok":
        chunk = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "hi"}}]}
        return httpx.Response(
            200, headers=sse,
            content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
        )
    if mode == "stream_error_event":
        return httpx.Response(
            200, headers=sse,
            content=b'data: {"error": {"message": "upstream boom"}}\n\n',
        )
    return httpx.Response(200, json=OK_JSON)


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
    expect(r.status_code == 201, "新增模型 m1 成功", r.text)

    r = client.post("/api/admin/keys", headers=H, json={"name": "k1", "allowed_services": ["llm"]})
    expect(r.status_code == 201, "创建 Key 成功", r.text)
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    def last_log(**filters):
        qs = "&".join(f"{k}={v}" for k, v in filters.items())
        rr = client.get(f"/api/admin/logs?page_size=1&{qs}", headers=H)
        expect(rr.status_code == 200, "日志查询可用", rr.text)
        items = rr.json().get("items") or []
        expect(bool(items), "存在至少一条日志", rr.text)
        row = items[0]
        # 列表接口只给摘要，大字段在详情接口里；测试里合并回来看
        dr = client.get(f"/api/admin/logs/{row['id']}", headers=H)
        expect(dr.status_code == 200, "日志详情可用", dr.text)
        return {**row, **dr.json()}

    BODY = {"model": "m1", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}

    # ---------- 1. 非流式：上游 401 ----------
    print("\n== 非流式上游 401 ==")
    STATE["mode"] = "unauthorized"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 401, "上游 401 原样透传（不再折叠成 502）", r.text)
    rid = r.headers.get("X-Request-Id", "")
    expect(len(rid) >= 16, "响应头带 X-Request-Id", rid)

    log = last_log()
    expect(log["status"] == "error", "日志状态 error", json.dumps(log, ensure_ascii=False))
    expect(log["error_type"] == "upstream_status", "error_type=upstream_status", log["error_type"])
    expect(log["upstream_status"] == 401, "记录真实上游状态码 401", str(log["upstream_status"]))
    expect("invalid api key" in log["upstream_body"], "记录上游响应体原文", log["upstream_body"])
    expect("sk-***" in log["upstream_body"] and "sk-abcdef" not in log["upstream_body"],
           "上游响应体中的密钥已脱敏", log["upstream_body"])
    expect(log["request_id"] == rid, "日志 request_id 与响应头一致", f"{log['request_id']} vs {rid}")
    expect("http://mock.local/v1/chat/completions" in log["upstream_url"],
           "记录实际上游地址", log["upstream_url"])
    expect(log["provider_name"] == "mock-upstream", "记录厂商名", log["provider_name"])

    # ---------- 2. 流式：上游 401（旧实现会变成 200 空流） ----------
    print("\n== 流式上游 401（核心回归） ==")
    STATE["mode"] = "stream_unauthorized"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 401, "流式鉴权失败必须返回 401，而不是 200 空流", f"{r.status_code} {r.text[:200]}")
    expect("error" in r.text.lower(), "响应体是错误 JSON", r.text[:200])

    log = last_log()
    expect(log["status"] == "error", "流式失败已落日志（旧实现记成 success）",
           json.dumps(log, ensure_ascii=False))
    expect(log["error_type"] == "upstream_status", "流式 error_type=upstream_status", log["error_type"])
    expect(log["upstream_status"] == 401, "流式记录真实状态码 401", str(log["upstream_status"]))
    expect("stream auth failed" in log["upstream_body"], "流式记录上游响应体", log["upstream_body"])

    # ---------- 3. 流式：200 但无任何 data 事件 ----------
    print("\n== 空流式响应 ==")
    STATE["mode"] = "empty_stream"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 200, "空流仍是 200（客户端已开始读流）", r.status_code)
    log = last_log()
    expect(log["status"] == "error", "空流被记为失败", log["status"])
    expect(log["error_type"] == "empty_stream", "error_type=empty_stream", log["error_type"])
    expect("content-type" in log["error_message"], "错误信息含 content-type 线索", log["error_message"])

    # ---------- 4. 流式正常 ----------
    print("\n== 流式正常 ==")
    STATE["mode"] = "stream_ok"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 200 and "data:" in r.text, "正常流式可用", r.text[:120])
    log = last_log()
    expect(log["status"] == "success", "正常流式记 success", log["status"])
    expect(log["error_type"] == "", "成功记录 error_type 为空", log["error_type"])
    expect(log["upstream_status"] == 200, "成功记录上游 200", str(log["upstream_status"]))

    # ---------- 5. 流内 error 事件 ----------
    print("\n== 流内错误事件 ==")
    STATE["mode"] = "stream_error_event"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 200, "流内错误仍是 200", r.status_code)
    log = last_log()
    expect(log["status"] == "error", "流内错误被记为失败", log["status"])
    expect(log["error_type"] == "upstream_stream_error", "error_type=upstream_stream_error", log["error_type"])
    expect("upstream boom" in log["error_message"], "错误信息含上游原文", log["error_message"])

    # ---------- 6. 上游 200 但非 JSON ----------
    print("\n== 上游非 JSON ==")
    STATE["mode"] = "not_json"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 502, "非 JSON 返回 502", r.status_code)
    log = last_log()
    expect(log["error_type"] == "upstream_non_json", "error_type=upstream_non_json", log["error_type"])
    expect("502 Bad Gateway" in log["upstream_body"], "记录上游原文", log["upstream_body"])

    # ---------- 7. 连接失败 / 超时 ----------
    print("\n== 传输层异常 ==")
    STATE["mode"] = "connect_error"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 502, "连接失败返回 502", r.status_code)
    log = last_log()
    expect(log["error_type"] == "upstream_connect", "error_type=upstream_connect", log["error_type"])

    STATE["mode"] = "timeout"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 502, "超时返回 502", r.status_code)
    log = last_log()
    expect(log["error_type"] == "upstream_timeout", "error_type=upstream_timeout", log["error_type"])

    # ---------- 8. 模型不存在也要留痕 ----------
    print("\n== 模型不存在 ==")
    STATE["mode"] = "ok"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "model": "no-such-model"})
    expect(r.status_code == 404, "模型不存在返回 404", r.status_code)
    log = last_log(status_filter="error")
    expect(log["status"] == "error", "404 也落日志（旧实现一条不留）", json.dumps(log, ensure_ascii=False))
    expect(log["error_type"] == "model_not_found", "error_type=model_not_found", log["error_type"])
    expect(log["model"] == "no-such-model", "记录请求的模型名", log["model"])

    # ---------- 9. 非法请求体 ----------
    print("\n== 非法请求体 ==")
    r = client.post("/v1/chat/completions", headers={**KEY, "Content-Type": "application/json"},
                    content=b"{bad json")
    expect(r.status_code == 400, "非法 JSON 返回 400", r.status_code)
    log = last_log(status_filter="error")
    expect(log["error_type"] == "invalid_request", "error_type=invalid_request", log["error_type"])

    # ---------- 10. 按 error_type 过滤 ----------
    print("\n== 过滤 ==")
    rr = client.get("/api/admin/logs?page_size=50&error_type=upstream_status", headers=H)
    items = rr.json().get("items") or []
    expect(len(items) >= 2, "按 error_type 过滤可用", str(len(items)))
    expect(all(i["error_type"] == "upstream_status" for i in items), "过滤结果类型一致")

    rr = client.get(f"/api/admin/logs?page_size=10&request_id={rid}", headers=H)
    expect(rr.json().get("total") == 1, "按 request_id 精确反查", rr.text[:200])

print(f"\n✅ 全部通过：{len(PASSED)} 项")
