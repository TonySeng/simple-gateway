"""实时监控：进行中请求的可见性。

覆盖：
1. 请求处理中能在 /api/admin/live 看到（还没写进日志表）
2. 字段：服务、接口、Key 名、客户端 IP、UA、模型、厂商、阶段、已持续时长
3. 请求结束后立刻消失
4. 流式请求在转发阶段显示「流式接收中」
5. 失败请求（上游 401）同样能看到、结束后消失
6. 未授权访问被拒

运行：.venv\\Scripts\\python.exe -m tests.test_live_monitor
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_live_monitor.db")
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
os.environ["SG_LOG_DETAIL_DAYS"] = "7"

sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.live import live  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


OK_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}

STATE = {"delay": 0.8, "status": 200}


async def _handler(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(STATE["delay"])
    if STATE["status"] >= 300:
        return httpx.Response(STATE["status"], json={"error": {"message": "invalid api key"}})
    if "stream" in request.content.decode("utf-8", "replace"):
        chunk = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "hi"}}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=("data: " + __import__("json").dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
        )
    return httpx.Response(200, json=OK_JSON)


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "移动云152",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-provider-key"},
    })
    pid = r.json()["id"]
    client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "m1", "provider_model": "m1",
    })
    r = client.post("/api/admin/keys", headers=H, json={"name": "my-app", "allowed_services": ["llm"]})
    KEY = {"Authorization": f"Bearer {r.json()['key']}", "User-Agent": "MyApp/1.2.3"}
    BODY = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

    # ---------- 0. 未授权 ----------
    print("\n== 鉴权 ==")
    expect(client.get("/api/admin/live").status_code == 401, "未登录访问被拒")
    expect(live.count() == 0, "初始没有进行中的请求")

    # ---------- 1/2. 处理中可见 ----------
    print("\n== 请求处理中可见 ==")
    STATE["delay"] = 0.9
    err: list[Exception] = []

    def call(payload: dict) -> None:
        try:
            client.post("/v1/chat/completions", headers=KEY, json=payload)
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=call, args=(BODY,))
    t.start()
    time.sleep(0.35)

    snap = client.get("/api/admin/live", headers=H).json()
    expect(snap["total"] == 1, "进行中 1 个请求", str(snap["total"]))
    it = snap["items"][0]
    expect(it["service"] == "llm", "服务=llm", it["service"])
    expect(it["endpoint"] == "/v1/chat/completions", "接口路径", it["endpoint"])
    expect(it["key_name"] == "my-app", "请求来源（Key 名）", it["key_name"])
    expect(it["model"] == "m1", "模型已解析", it["model"])
    expect(it["provider_name"] == "移动云152", "上游厂商", it["provider_name"])
    expect(it["client_ip"] != "", "客户端 IP", it["client_ip"])
    expect(it["user_agent"].startswith("MyApp/"), "客户端 UA", it["user_agent"])
    expect(it["stage"] in ("connecting", "writing"), "阶段合理", it["stage"])
    expect(it["elapsed_ms"] >= 200, "已持续时长递增", str(it["elapsed_ms"]))
    expect(it["stream"] is False, "非流式标记", str(it["stream"]))
    expect(it["request_id"], "request_id 非空")

    # ---------- 3. 结束后消失 ----------
    print("\n== 结束后从实时列表消失 ==")
    t.join()
    expect(not err, "请求本身成功", str(err))
    snap = client.get("/api/admin/live", headers=H).json()
    expect(snap["total"] == 0, "实时列表已清空", str(snap["total"]))
    logs = client.get("/api/admin/logs?page_size=1", headers=H).json()
    expect(logs["total"] == 1, "记录已落到用量日志", str(logs["total"]))
    expect(logs["items"][0]["request_id"] == it["request_id"], "实时与日志同一条 request_id")

    # ---------- 4. 流式阶段 ----------
    print("\n== 流式请求 ==")
    t = threading.Thread(target=call, args=({**BODY, "stream": True},))
    t.start()
    time.sleep(0.4)
    snap = client.get("/api/admin/live", headers=H).json()
    expect(snap["total"] == 1, "流式请求被登记", str(snap["total"]))
    si = snap["items"][0]
    expect(si["stream"] is True, "标记为流式", str(si["stream"]))
    expect(si["stage"] in ("connecting", "streaming"), "阶段=等待上游/流式接收", si["stage"])
    expect(si["stage_label"] != "", "阶段有中文标签", si["stage_label"])
    t.join()
    expect(client.get("/api/admin/live", headers=H).json()["total"] == 0, "流式结束后消失")

    # ---------- 5. 失败请求 ----------
    print("\n== 失败请求 ==")
    STATE["status"] = 401
    t = threading.Thread(target=call, args=(BODY,))
    t.start()
    time.sleep(0.35)
    expect(client.get("/api/admin/live", headers=H).json()["total"] == 1, "失败请求处理中可见")
    t.join()
    expect(client.get("/api/admin/live", headers=H).json()["total"] == 0, "失败请求结束后消失")
    STATE["status"] = 200

    last = client.get("/api/admin/logs?page_size=1", headers=H).json()["items"][0]
    expect(last["status"] == "error" and last["error_type"] == "upstream_status",
           "失败记录照常落库", str(last["status"]))

    # ---------- 6. 流式上游非 2xx 必须摘除 ----------
    # 旧实现：handle_chat 在调用 _stream_response 之前就置 stream_handed_off=True，
    # 而上游非 2xx 时 _stream_response 返回的是普通 JSONResponse（不是 StreamingResponse），
    # wrapped() 从未创建 → live.finish 永不执行 → 条目在实时监控里滞留到 MAX_AGE_S(1800s)。
    # 线上实例：request_id 8aaeebfb… 在监控页挂了 27 分钟。
    print("\n== 流式失败不滞留（回归） ==")
    STATE["delay"] = 0.15
    STATE["status"] = 429
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 429, "流式上游 429 透传给客户端", r.status_code)
    expect(live.count() == 0, "流式失败后实时条目立即摘除",
           f"仍残留 {live.count()} 条")
    expect(client.get("/api/admin/live", headers=H).json()["total"] == 0,
           "实时接口立刻归零，不残留僵尸请求")
    STATE["status"] = 200

    log = client.get("/api/admin/logs?page_size=1", headers=H).json()["items"][0]
    expect(log["status"] == "error" and log["upstream_status"] == 429,
           "流式失败照样落库", f"{log['status']} {log['upstream_status']}")

    # ---------- 7. 僵尸判定的时间基准 ----------
    # 旧实现按 started_at 判定：一个正常跑了 30 分钟以上的流式请求会被当成僵尸清掉，
    # 监控里凭空消失。应改为「多久没更新」，只要有 update 就续命。
    print("\n== 僵尸判定基准 ==")
    from app.core.live import LiveRegistry  # noqa: E402

    reg = LiveRegistry()
    reg.start("alive", service="llm", endpoint="/v1/chat/completions")
    reg.start("dead", service="llm", endpoint="/v1/chat/completions")
    with reg._lock:  # 把两条都伪装成 1 小时前开始
        for rid in ("alive", "dead"):
            reg._items[rid]["started_at"] -= 3600
            reg._items[rid]["updated_at"] -= 3600
    reg.update("alive", stage="streaming")  # 活跃者仍在推进
    expect(reg.count() == 1, "长时间运行但仍在更新的请求不被误清", f"剩余 {reg.count()} 条")
    expect([r["request_id"] for r in reg.snapshot()] == ["alive"], "留下的是活跃那条")
    expect(reg.snapshot()[0]["elapsed_ms"] > 3500_000, "已持续时长仍按入口时间计算",
           str(reg.snapshot()[0]["elapsed_ms"]))

print(f"\n全部通过：{len(PASSED)} 项")
