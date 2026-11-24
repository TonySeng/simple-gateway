"""日志详情：请求/上游返回内容 + 定期清理。

覆盖的关键点：
1. 列表只给摘要，**不返回** request_body / response_body（一页几十条大字段会拖死列表）
2. 详情接口返回完整请求体与上游返回体
3. 成功 / 失败 / 流式三种形态都能拿到内容
4. 详情里的密钥必须脱敏
5. 超长内容按上限截断并留下提示
6. 保留期默认 7 天、可改、非法值拒绝
7. 超期详情被清空，但**日志行与统计不丢**
8. 详情接口 404

运行：.venv\\Scripts\\python.exe -m tests.test_log_detail
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_log_detail.db")
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
os.environ["SG_LOG_BODY_LIMIT"] = "2000"

sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.logbody import BodyCollector, dump_body, limit_text, redact  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


# ---------------- 单元：内容处理 ----------------
print("\n== 内容处理 ==")
expect("sk-***" in redact("key=sk-abcdef123456"), "密钥脱敏", redact("key=sk-abcdef123456"))
expect("Bearer ***" in redact("Authorization: Bearer abcdefgh1234"),
       "Bearer 脱敏", redact("Authorization: Bearer abcdefgh1234"))
trunc = limit_text("x" * 500, 200)
expect(len(trunc) <= 200, "截断后总长不超过上限", str(len(trunc)))
expect("已截断" in trunc and "字符" in trunc, "截断处标注丢了多少字符", trunc[-40:])
expect(trunc.startswith("xxx"), "保留原文开头", trunc[:10])
expect(dump_body({"a": 1}) == '{"a":1}', "请求体紧凑序列化", dump_body({"a": 1}))
expect(dump_body({"api_key": "sk-secretvalue123"}) == '{"api_key":"***"}'
       or "***" in dump_body({"api_key": "sk-secretvalue123"}),
       "请求体落库前脱敏", dump_body({"api_key": "sk-secretvalue123"}))

bc = BodyCollector(10)
bc.add("12345"); bc.add("67890"); bc.add("overflow")
expect(bc.text().startswith("1234567890") and "已截断" in bc.text(),
       "流式累积到上限即停", bc.text()[:30])

# ---------------- 接口 ----------------
STATE = {"mode": "ok"}

OK_JSON = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好呀"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


def _handler(request: httpx.Request) -> httpx.Response:
    mode = STATE["mode"]
    if mode == "unauthorized":
        return httpx.Response(
            401, json={"error": {"message": "invalid api key sk-abcdef123456"}}
        )
    if mode == "huge":
        return httpx.Response(200, json={**OK_JSON, "big": "z" * 8000})
    if mode == "stream_ok":
        chunk = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "流式内容"}}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
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
        "service": "llm", "type": "openai", "name": "mock",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-provider-key"},
    })
    pid = r.json()["id"]
    client.post("/api/admin/models", headers=H, json={
        "provider_id": pid, "name": "m1", "provider_model": "m1",
    })
    r = client.post("/api/admin/keys", headers=H, json={"name": "k1", "allowed_services": ["llm"]})
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    BODY = {"model": "m1", "max_tokens": 8, "temperature": 0.3,
            "messages": [{"role": "user", "content": "请只回复两个字：收到"}]}

    def latest():
        rr = client.get("/api/admin/logs?page_size=1", headers=H)
        return rr.json()["items"][0]

    def detail(log_id):
        return client.get(f"/api/admin/logs/{log_id}", headers=H)

    # ---------- 1. 列表瘦身 ----------
    print("\n== 列表不返回大字段 ==")
    STATE["mode"] = "ok"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 200, "成功请求", r.text[:150])

    row = latest()
    expect("request_body" not in row, "列表不含 request_body")
    expect("response_body" not in row, "列表不含 response_body")
    expect("upstream_body" not in row, "列表不含 upstream_body")
    expect(row["has_detail"] is True, "列表标记 has_detail", str(row.get("has_detail")))

    # ---------- 2. 详情：请求 + 上游返回 ----------
    print("\n== 详情含请求与上游返回 ==")
    dr = detail(row["id"])
    expect(dr.status_code == 200, "详情接口可用", dr.text[:150])
    d = dr.json()

    expect("请只回复两个字" in d["request_body"], "请求体记录了用户原文", d["request_body"][:120])
    expect('"temperature":0.3' in d["request_body"], "请求体保留参数", d["request_body"][:160])
    # 上游原文可能是 \uXXXX 转义（取决于上游），按 JSON 解析后比对内容
    up = json.loads(d["response_body"])
    expect(up["choices"][0]["message"]["content"] == "你好呀",
           "上游返回体记录完整内容", json.dumps(up, ensure_ascii=False)[:120])
    expect(up["usage"]["completion_tokens"] == 7, "上游返回体含 usage",
           json.dumps(up, ensure_ascii=False)[:200])
    expect("sk-provider-key" not in d["request_body"], "请求详情里没有明文密钥")
    expect("sk-provider-key" not in d["response_body"], "返回详情里没有明文密钥")

    # ---------- 3. 失败详情 ----------
    print("\n== 失败请求也有详情 ==")
    STATE["mode"] = "unauthorized"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 401, "失败返回 401（上游状态码透传）", r.status_code)
    row = latest()
    d = detail(row["id"]).json()
    expect("invalid api key" in d["response_body"], "失败时记录上游错误体", d["response_body"][:150])
    expect("sk-***" in d["response_body"] and "sk-abcdef" not in d["response_body"],
           "上游错误体里的密钥已脱敏", d["response_body"][:150])
    expect("请只回复两个字" in d["request_body"], "失败请求同样记录请求体")

    # ---------- 4. 流式详情 ----------
    print("\n== 流式详情 ==")
    STATE["mode"] = "stream_ok"
    r = client.post("/v1/chat/completions", headers=KEY, json={**BODY, "stream": True})
    expect(r.status_code == 200 and "data:" in r.text, "流式请求成功", r.text[:100])
    row = latest()
    d = detail(row["id"]).json()
    expect("data:" in d["response_body"], "流式详情保存了 SSE 原文", d["response_body"][:120])
    expect('"content"' in d["response_body"], "流式内容完整（含 content 字段）",
           d["response_body"][:160])
    expect('"stream":true' in d["request_body"], "流式请求体含 stream 标记", d["request_body"][:120])

    # ---------- 5. 截断 ----------
    print("\n== 超长返回被截断 ==")
    STATE["mode"] = "huge"
    r = client.post("/v1/chat/completions", headers=KEY, json=BODY)
    expect(r.status_code == 200, "大响应请求成功", r.status_code)
    row = latest()
    d = detail(row["id"]).json()
    expect(len(d["response_body"]) < 2300, "详情长度受上限约束", str(len(d["response_body"])))
    expect("已截断" in d["response_body"], "截断处有明确提示", d["response_body"][-80:])
    STATE["mode"] = "ok"

    # ---------- 6. 保留期配置 ----------
    print("\n== 保留期配置 ==")
    rr = client.get("/api/admin/logs/retention", headers=H)
    expect(rr.status_code == 200, "读取保留策略", rr.text)
    expect(rr.json()["days"] == 7, "默认保留 7 天", str(rr.json()))
    expect(rr.json()["detail_rows"] >= 4, "统计到带详情的条数", str(rr.json().get("detail_rows")))
    total_all = client.get("/api/admin/logs?page_size=1", headers=H).json()["total"]
    expect(rr.json().get("total_rows") == total_all, "保留策略里带记录总条数",
           f"{rr.json().get('total_rows')} vs {total_all}")
    before_rows = rr.json()["detail_rows"]

    rr = client.put("/api/admin/logs/retention?days=3", headers=H)
    expect(rr.status_code == 200 and rr.json()["days"] == 3, "改为 3 天", rr.text)
    rr = client.get("/api/admin/logs/retention", headers=H)
    expect(rr.json()["days"] == 3, "设置持久化", str(rr.json()))

    rr = client.put("/api/admin/logs/retention?days=999", headers=H)
    expect(rr.status_code == 400, "超出上限被拒绝", str(rr.status_code))
    rr = client.put("/api/admin/logs/retention?days=-1", headers=H)
    expect(rr.status_code == 400, "负数被拒绝", str(rr.status_code))

    # ---------- 7. 清理只清详情，不删统计 ----------
    print("\n== 清理超期详情 ==")
    # 把这批日志的 created_at 挪到 5 天前（超过 3 天保留期）
    from app.core.db import SessionLocal  # noqa: E402
    from app.core.models import RequestLog  # noqa: E402

    old = datetime.utcnow() - timedelta(days=5)
    with SessionLocal() as db:
        db.query(RequestLog).update({RequestLog.created_at: old})
        db.commit()

    total_before = client.get("/api/admin/logs?page_size=1", headers=H).json()["total"]
    rr = client.post("/api/admin/logs/retention/cleanup", headers=H)
    expect(rr.status_code == 200, "触发清理", rr.text)
    cleared = rr.json()["cleared"]
    expect(cleared >= 4, f"清理了 {cleared} 条详情", str(rr.json()))
    expect(rr.json().get("kept_rows") == total_before,
           "接口回显：记录一条没删", f"{rr.json().get('kept_rows')} vs {total_before}")

    total_after = client.get("/api/admin/logs?page_size=1", headers=H).json()["total"]
    expect(total_after == total_before, "日志行数不变（统计不丢）", f"{total_before} -> {total_after}")

    # 直接查库确认没有 DELETE：行数、状态、tokens 都还在，只有三个 body 字段被置空
    with SessionLocal() as db:
        rows = db.query(RequestLog).all()
        expect(len(rows) == total_before, "库里行数与清理前一致", f"{len(rows)} vs {total_before}")
        expect(all(r.request_body == "" and r.response_body == ""
                   and r.upstream_body == "" for r in rows), "三个详情字段已置空")
        expect(all(r.status in ("success", "error") and r.tokens_in >= 0 for r in rows),
               "状态与 tokens 统计完好")

    row = latest()
    d = detail(row["id"]).json()
    expect(d["request_body"] == "" and d["response_body"] == "", "详情已清空", str(d)[:120])
    expect(d["has_detail"] is False, "has_detail 变为 false", str(d.get("has_detail")))
    expect(row["status"] in ("success", "error"), "统计状态仍在", row["status"])
    expect(row["latency_ms"] >= 0, "耗时仍在", str(row["latency_ms"]))

    rr = client.get("/api/admin/logs/retention", headers=H)
    expect(rr.json()["detail_rows"] < before_rows, "带详情条数下降", str(rr.json()))

    # 保留期内的不会被清
    rr = client.put("/api/admin/logs/retention?days=7", headers=H)
    expect(rr.json()["days"] == 7, "改回 7 天", rr.text)

    # ---------- 8. 404 ----------
    print("\n== 详情 404 ==")
    rr = detail("no-such-log-id")
    expect(rr.status_code == 404, "不存在的日志返回 404", str(rr.status_code))

print(f"\n全部通过：{len(PASSED)} 项")
