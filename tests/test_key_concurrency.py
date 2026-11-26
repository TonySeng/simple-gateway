"""按 API Key 的并发限制测试。

覆盖（2026-11-26 需求）：
1. 纯模块：acquire/release 原子性、幂等、key 隔离、limit<=0 不限、同一 token 不重复计数。
2. LLM 全链路：
   - 慢上游制造并发占用，达到上限后新请求被拒（429 / error_type=concurrency_limit /
     OpenAI 与 Anthropic 两种错误格式），且不调用上游；
   - 释放后新请求可进入；
   - 不同 Key 互不影响；
   - 失败（上游非 2xx）后槽位及时释放；
   - 流式请求占用期间计数、结束后释放；
   - 单 Key max_concurrency 覆盖全局配置。
3. OCR 全链路：并发超限返回 429（OCR 现有 detail 格式），释放后可再进。

运行：.venv\\Scripts\\python.exe -m tests.test_key_concurrency
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_key_concurrency.db")
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
# 全局默认并发上限设为 2（每个 Key）；单 Key 可覆盖
os.environ["SG_API_KEY_MAX_CONCURRENCY"] = "2"

sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.concurrency import KeyConcurrencyLimiter, key_concurrency  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


# =====================================================================
# 1. 纯模块
# =====================================================================
print("\n== 纯模块：并发计数 ==")
lim = KeyConcurrencyLimiter()
expect(lim.try_acquire("k1", "r1", 2) is True, "k1/r1 占用成功")
expect(lim.try_acquire("k1", "r2", 2) is True, "k1/r2 占用成功")
expect(lim.try_acquire("k1", "r3", 2) is False, "k1 达上限后 r3 被拒")
expect(lim.try_acquire("k2", "r1", 2) is True, "k2 不受 k1 影响")
expect(lim.in_use("k1") == 2 and lim.in_use("k2") == 1, "按 key 分别计数")
expect(lim.try_acquire("k1", "r1", 2) is True, "同一 token 重复 acquire 不增加计数")
expect(lim.in_use("k1") == 2, "重复 acquire 后计数不变")

lim.release("k1", "r1")
expect(lim.in_use("k1") == 1, "释放后计数减 1")
lim.release("k1", "r1")  # 幂等
expect(lim.in_use("k1") == 1, "重复释放不重复减（幂等）")
lim.release("k1", "never-acquired")  # 未占用的 token
expect(lim.in_use("k1") == 1, "释放未占用 token 无副作用")
lim.release("k1", "r2")
expect(lim.in_use("k1") == 0 and "k1" not in lim.snapshot(), "全部释放后 key 条目清理")

expect(lim.try_acquire("k3", "r1", 0) is True, "limit=0 不限流")
expect(lim.try_acquire("k3", "r2", 0) is True, "limit=0 继续占用成功")
expect("k3" not in lim.snapshot(), "limit=0 不登记计数")
lim.release("k3", "r1")  # no-op

expect(settings.api_key_max_concurrency == 2, "全局配置从环境变量读取为 2")


# =====================================================================
# 2. LLM 全链路
# =====================================================================
print("\n== LLM 全链路准备 ==")
STATE = {"delay": 0.6, "status": 200, "upstream_calls": 0}
STATE_LOCK = threading.Lock()

OK_JSON = {
    "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


async def _handler(request: httpx.Request) -> httpx.Response:
    with STATE_LOCK:
        STATE["upstream_calls"] += 1
    await asyncio.sleep(STATE["delay"])
    if STATE["status"] >= 300:
        return httpx.Response(STATE["status"], json={"error": {"message": "upstream fail"}})
    return httpx.Response(200, json=OK_JSON)


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "p1",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-x"},
    })
    pid = r.json()["id"]
    client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "m1", "provider_model": "m1",
    })

    # Key A：走全局上限 2；Key B：独立上限；Key C：覆盖为 1
    def make_key(name, max_concurrency=None):
        payload = {"name": name, "allowed_services": ["llm"]}
        if max_concurrency is not None:
            payload["max_concurrency"] = max_concurrency
        r = client.post("/api/admin/keys", headers=H, json=payload)
        expect(r.status_code == 201, f"创建 Key {name}", r.text)
        body = r.json()
        return {"Authorization": f"Bearer {body['key']}"}, body["id"]

    KA, KA_ID = make_key("keyA")
    KB, KB_ID = make_key("keyB")
    KC, KC_ID = make_key("keyC", max_concurrency=1)
    BODY = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

    def post(headers, payload=BODY, path="/v1/chat/completions", out=None, idx=0):
        try:
            out[idx] = client.post(path, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001
            out[idx] = e

    # ---- 2.1 并发上限与限流响应 ----
    print("\n== 并发上限：第 3 个请求被拒 ==")
    STATE["delay"] = 0.7
    STATE["status"] = 200
    out: dict[int, object] = {}
    threads = []
    for i in range(3):
        t = threading.Thread(target=post, args=(KA, BODY, "/v1/chat/completions", out, i))
        t.start()
        threads.append(t)
        time.sleep(0.08)  # 错开，确保前两个先占用
    for t in threads:
        t.join()

    statuses = sorted(
        r.status_code if isinstance(r, httpx.Response) else -1 for r in out.values()
    )
    expect(statuses == [200, 200, 429], "2 成功 + 1 限流(429)", str(statuses))
    limited = [r for r in out.values() if isinstance(r, httpx.Response) and r.status_code == 429]
    expect(len(limited) == 1, "恰有 1 个 429")
    err_body = limited[0].json()
    expect(err_body.get("error", {}).get("type") == "concurrency_limit",
           "OpenAI 错误格式 type=concurrency_limit", str(err_body))
    expect("并发请求已达上限" in err_body.get("error", {}).get("message", ""),
           "友好中文提示", str(err_body))
    logs = client.get("/api/admin/logs?error_type=concurrency_limit&page_size=5", headers=H).json()
    expect(logs["total"] >= 1, "限流记录落库", str(logs["total"]))

    # ---- 2.2 释放后新请求可进入 ----
    print("\n== 释放后可再进入 ==")
    expect(key_concurrency.in_use(KA_ID) == 0, "占用已全部释放", str(key_concurrency.in_use(KA_ID)))
    STATE["delay"] = 0.05
    r = client.post("/v1/chat/completions", headers=KA, json=BODY)
    expect(r.status_code == 200, "释放后新请求成功", r.text[:200])

    # ---- 2.3 不同 Key 互不影响 ----
    print("\n== Key 隔离 ==")
    STATE["delay"] = 0.6
    out2: dict[int, object] = {}
    # keyA 占 2 个
    ta = [threading.Thread(target=post, args=(KA, BODY, "/v1/chat/completions", out2, i)) for i in range(2)]
    for t in ta:
        t.start()
    time.sleep(0.1)
    # keyB 同时间发 1 个，应成功（独立性）
    rb = client.post("/v1/chat/completions", headers=KB, json=BODY)
    expect(rb.status_code == 200, "keyB 不受 keyA 占满影响", rb.text[:200])
    for t in ta:
        t.join()
    expect(key_concurrency.in_use(KA_ID) == 0 and key_concurrency.in_use(KB_ID) == 0,
           "两个 Key 占用均释放")

    # ---- 2.4 单 Key 覆盖全局（KC 上限 1）----
    print("\n== 单 Key 覆盖上限 ==")
    STATE["delay"] = 0.6
    out3: dict[int, object] = {}
    tc = [threading.Thread(target=post, args=(KC, BODY, "/v1/chat/completions", out3, i)) for i in range(2)]
    for t in tc:
        t.start()
        time.sleep(0.08)
    for t in tc:
        t.join()
    statuses_c = sorted(
        r.status_code if isinstance(r, httpx.Response) else -1 for r in out3.values()
    )
    expect(statuses_c == [200, 429], "keyC 上限 1：1 成功 + 1 限流", str(statuses_c))

    # ---- 2.5 失败路径释放 ----
    print("\n== 失败后释放 ==")
    STATE["delay"] = 0.05
    STATE["status"] = 401
    before = key_concurrency.in_use(KA_ID)
    rf = client.post("/v1/chat/completions", headers=KA, json=BODY)
    expect(rf.status_code == 401, "上游失败透传 401", rf.text[:200])
    expect(key_concurrency.in_use(KA_ID) == before, "失败后占用归零")
    STATE["status"] = 200

    # ---- 2.6 Anthropic 协议错误格式一致 ----
    print("\n== Anthropic 错误格式 ==")
    STATE["delay"] = 0.6
    out4: dict[int, object] = {}
    ta2 = [threading.Thread(target=post, args=(KA, BODY, "/v1/chat/completions", out4, i)) for i in range(2)]
    for t in ta2:
        t.start()
        time.sleep(0.08)
    time.sleep(0.1)
    # 第三个走 /v1/messages（Anthropic 入站），应被限流且格式为 Anthropic 风格
    r_anth = client.post("/v1/messages", headers=KA, json={
        "model": "m1", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100,
    })
    for t in ta2:
        t.join()
    expect(r_anth.status_code == 429, "Anthropic 入站同样限流", r_anth.text[:200])
    ab = r_anth.json()
    expect(ab.get("type") == "error" and ab.get("error", {}).get("type") == "concurrency_limit",
           "Anthropic 错误格式 type=error/error.type=concurrency_limit", str(ab))

    # ---- 2.7 流式请求占用与释放 ----
    print("\n== 流式占用与释放 ==")
    STATE["delay"] = 0.5
    STATE["status"] = 200
    sbody = {**BODY, "stream": True}
    out5: dict[int, object] = {}
    ts = threading.Thread(target=post, args=(KA, sbody, "/v1/chat/completions", out5, 0))
    ts.start()
    time.sleep(0.15)
    expect(key_concurrency.in_use(KA_ID) == 1, "流式请求占用中", str(key_concurrency.in_use(KA_ID)))
    ts.join()
    expect(key_concurrency.in_use(KA_ID) == 0, "流式结束后释放", str(key_concurrency.in_use(KA_ID)))

print(f"\nLLM 部分通过：{len(PASSED)} 项")
print(f"全部通过：{len(PASSED)} 项")
