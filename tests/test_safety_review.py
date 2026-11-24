"""安全审查回归测试（离线 MockTransport）。

覆盖：
1. 多词库 + 规则 + Key 绑定/关闭；
2. 输入命中（OpenAI / Anthropic）不调用上游，返回协议合法的默认内容；
3. 非流式输出命中：保留真实 tokens/费用，日志标注 blocked_output；
4. 流式输出命中：原文零泄露，返回默认内容 SSE；
5. 流式干净输出：原样转发，日志标注 passed；
6. 独立审查记录可按方向/Key/规则查询；
7. 请求日志直接带 safety_result，并支持过滤；
8. 后台词库/规则/测试接口可用。

运行：.venv\\Scripts\\python.exe -m tests.test_safety_review
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_safety_review.db")
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


STATE = {"mode": "ok", "calls": 0}
SSE = {"content-type": "text/event-stream"}

OK_JSON = {
    "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "正常回复"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def _chunk(text: str, finish=None):
    return {
        "id": "c1", "object": "chat.completion.chunk", "model": "m1",
        "choices": [{"index": 0, "delta": ({"content": text} if text else {}),
                     "finish_reason": finish}],
    }


def _handler(request: httpx.Request) -> httpx.Response:
    STATE["calls"] += 1
    mode = STATE["mode"]
    if mode == "output_bad":
        return httpx.Response(200, json={
            **OK_JSON,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "这是违规词，不能输出"},
                         "finish_reason": "stop"}],
        })
    if mode == "stream_clean":
        body = (
            "data: " + json.dumps(_chunk("正常", None), ensure_ascii=False) + "\n\n"
            + "data: " + json.dumps(_chunk("回复", "stop") | {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}, ensure_ascii=False) + "\n\n"
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers=SSE, content=body.encode())
    if mode == "stream_bad":
        # 关键词被拆到多个 chunk，验证流式聚合审查
        body = (
            "data: " + json.dumps(_chunk("这是违", None), ensure_ascii=False) + "\n\n"
            + "data: " + json.dumps(_chunk("规词，不能输出", "stop") | {"usage": {"prompt_tokens": 4, "completion_tokens": 3}}, ensure_ascii=False) + "\n\n"
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers=SSE, content=body.encode())
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

    # 两套词库，验证规则可组合多个词库
    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "通用违规词", "terms": "# 注释\n违规词\n敏感\nre:bad\\d+",
        "description": "测试词库",
    })
    expect(r.status_code == 201, "新建词库成功", r.text)
    lib1 = r.json()
    expect(lib1["term_count"] == 3, "词条计数排除注释", str(lib1["term_count"]))

    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "业务专有词", "terms": "内部代号",
    })
    expect(r.status_code == 201, "新建第二套词库成功", r.text)
    lib2 = r.json()

    # 内置基线已占默认位，这里显式抢回默认以延续原有断言场景
    r = client.post("/api/admin/safety/policies", headers=H, json={
        "name": "默认规则",
        "is_default": True,
        "library_ids": [lib1["id"], lib2["id"]],
        "input_enabled": True,
        "output_enabled": True,
        "threshold": 1,
        "default_reply": "内容未通过安全审查。",
    })
    expect(r.status_code == 201, "新建审查规则成功", r.text)
    policy = r.json()
    expect(policy["is_default"] is True, "显式设为默认规则成功", json.dumps(policy, ensure_ascii=False))
    expect(len(policy["library_names"]) == 2, "规则引用两套词库", str(policy["library_names"]))

    r = client.post("/api/admin/keys", headers=H, json={
        "name": "继承默认", "allowed_services": ["llm"],
    })
    expect(r.status_code == 201, "创建继承默认 Key", r.text)
    KEY_DEFAULT = {"Authorization": f"Bearer {r.json()['key']}"}

    r = client.post("/api/admin/keys", headers=H, json={
        "name": "绑定规则", "allowed_services": ["llm"], "safety_policy_id": policy["id"],
    })
    expect(r.status_code == 201, "创建绑定规则 Key", r.text)
    KEY_BOUND = {"Authorization": f"Bearer {r.json()['key']}"}
    expect(r.json()["safety_policy_id"] == policy["id"], "Key 回显绑定规则", r.text)

    r = client.post("/api/admin/keys", headers=H, json={
        "name": "关闭审查", "allowed_services": ["llm"], "safety_policy_id": "",
    })
    expect(r.status_code == 201, "创建关闭审查 Key", r.text)
    KEY_OFF = {"Authorization": f"Bearer {r.json()['key']}"}

    def last_log(**filters):
        qs = "&".join(f"{k}={v}" for k, v in filters.items())
        rr = client.get(f"/api/admin/logs?page_size=1&{qs}", headers=H)
        expect(rr.status_code == 200, "日志查询可用", rr.text)
        items = rr.json().get("items") or []
        expect(bool(items), "存在至少一条日志", rr.text)
        row = items[0]
        dr = client.get(f"/api/admin/logs/{row['id']}", headers=H)
        expect(dr.status_code == 200, "日志详情可用", dr.text)
        return {**row, **dr.json()}

    BODY = {"model": "m1", "max_tokens": 8, "messages": [{"role": "user", "content": "你好"}]}

    # ---------- 1. 输入命中：OpenAI ----------
    print("\n== 输入命中（OpenAI） ==")
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_DEFAULT,
                    json={**BODY, "messages": [{"role": "user", "content": "帮我发一条违规词"}]})
    expect(r.status_code == 200, "输入命中返回 200 默认内容", r.text)
    expect(r.json()["choices"][0]["message"]["content"] == "内容未通过安全审查。", "返回配置的默认回复", r.text)
    expect(r.headers.get("X-Safety-Review") == "blocked_input", "响应头标注输入拦截", r.headers.get("X-Safety-Review", ""))
    expect(STATE["calls"] == before, "输入命中不调用上游", str(STATE["calls"] - before))

    log = last_log()
    expect(log["safety_result"] == "blocked_input", "请求日志标注输入拦截", log["safety_result"])
    expect(log["safety_policy"] == "默认规则", "日志记录规则名", log["safety_policy"])
    expect(log["tokens_in"] == 0 and log["tokens_out"] == 0, "输入拦截不计 tokens", str(log["tokens_in"]))

    # 流式请求的输入命中必须返回 SSE，不能返回普通 JSON
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_DEFAULT,
                    json={**BODY, "stream": True,
                          "messages": [{"role": "user", "content": "违规词"}]})
    expect(r.status_code == 200 and "data:" in r.text, "流式输入命中返回 SSE", r.text[:200])
    expect("内容未通过安全审查。" in r.text, "流式输入命中返回默认内容", r.text[:200])
    expect(STATE["calls"] == before, "流式输入命中不调用上游", str(STATE["calls"] - before))

    # ---------- 2. 输入命中：Anthropic 协议 ----------
    print("\n== 输入命中（Anthropic） ==")
    r = client.post("/v1/messages", headers=KEY_BOUND, json={
        "model": "m1", "max_tokens": 8,
        "messages": [{"role": "user", "content": "内部代号是什么"}],
    })
    expect(r.status_code == 200, "Anthropic 输入命中返回 200", r.text)
    expect(r.json().get("type") == "message", "Anthropic 响应结构合法", r.text[:200])
    expect(r.json()["content"][0]["text"] == "内容未通过安全审查。", "Anthropic 默认回复正确", r.text[:300])

    # ---------- 3. 非流式输出命中 ----------
    print("\n== 非流式输出命中 ==")
    STATE["mode"] = "output_bad"
    r = client.post("/v1/chat/completions", headers=KEY_BOUND, json=BODY)
    expect(r.status_code == 200, "输出命中返回 200", r.text)
    expect(r.json()["choices"][0]["message"]["content"] == "内容未通过安全审查。", "输出替换为默认回复", r.text)
    log = last_log()
    expect(log["safety_result"] == "blocked_output", "日志标注输出拦截", log["safety_result"])
    expect(log["tokens_in"] == 10 and log["tokens_out"] == 5, "输出拦截保留真实 tokens", f"{log['tokens_in']}/{log['tokens_out']}")
    expect(
        "违规词" in (log["upstream_body"] or "") or "\\u8fdd\\u89c4" in (log["upstream_body"] or ""),
        "上游原文留证", (log["upstream_body"] or "")[:100],
    )
    expect("违规词" not in (log["response_body"] or ""), "响应日志不记命中原文", (log["response_body"] or "")[:100])

    # ---------- 4. 流式输出命中 ----------
    print("\n== 流式输出命中 ==")
    STATE["mode"] = "stream_bad"
    r = client.post("/v1/chat/completions", headers=KEY_BOUND, json={**BODY, "stream": True})
    expect(r.status_code == 200, "流式命中返回 200", r.status_code)
    expect("违规" not in r.text, "流式响应零泄露命中原文", r.text[:200])
    expect("内容未通过安全审查。" in r.text, "流式返回默认回复", r.text[:200])
    expect("data: [DONE]" in r.text, "OpenAI 流以 [DONE] 收尾", r.text[-80:])
    log = last_log()
    expect(log["safety_result"] == "blocked_output", "流式日志标注输出拦截", log["safety_result"])
    expect(log["tokens_in"] == 4 and log["tokens_out"] == 3, "流式保留真实 tokens", f"{log['tokens_in']}/{log['tokens_out']}")

    # ---------- 4b. Anthropic 流式输出命中 ----------
    print("\n== Anthropic 流式输出命中 ==")
    STATE["mode"] = "stream_bad"
    r = client.post("/v1/messages", headers=KEY_BOUND, json={
        "model": "m1", "max_tokens": 8, "stream": True,
        "messages": [{"role": "user", "content": "你好"}],
    })
    expect(r.status_code == 200, "Anthropic 流式命中返回 200", r.status_code)
    expect("违规" not in r.text, "Anthropic 流式零泄露", r.text[:200])
    expect("内容未通过安全审查。" in r.text, "Anthropic 流式返回默认回复", r.text[:200])
    expect("event: message_start" in r.text and "event: message_stop" in r.text,
           "Anthropic SSE 事件序列完整", r.text[:200])

    # ---------- 5. 流式干净输出 ----------
    print("\n== 流式干净输出 ==")
    STATE["mode"] = "stream_clean"
    r = client.post("/v1/chat/completions", headers=KEY_BOUND, json={**BODY, "stream": True})
    expect("正常" in r.text and "回复" in r.text, "干净流原样转发", r.text[:200])
    log = last_log()
    expect(log["safety_result"] == "passed", "流式干净日志标注通过", log["safety_result"])

    # ---------- 6. 未绑定 Key 关闭审查 ----------
    print("\n== Key 关闭审查 ==")
    STATE["mode"] = "ok"
    before = STATE["calls"]
    r = client.post("/v1/chat/completions", headers=KEY_OFF,
                    json={**BODY, "messages": [{"role": "user", "content": "违规词"}]})
    expect(r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "正常回复",
           "关闭审查的 Key 正常走上游", r.text)
    expect(STATE["calls"] == before + 1, "关闭审查确实调用上游", str(STATE["calls"] - before))
    log = last_log()
    expect(log["safety_result"] == "", "关闭审查的日志不标注结果", log["safety_result"])

    # ---------- 7. 独立审查记录 ----------
    print("\n== 独立审查记录 ==")
    rr = client.get("/api/admin/safety/records?page_size=50", headers=H)
    expect(rr.status_code == 200, "审查记录接口可用", rr.text)
    items = rr.json()["items"]
    expect(len(items) >= 3, "输入/输出拦截均有独立记录", str(len(items)))
    expect(any(x["direction"] == "input" for x in items), "存在输入方向记录")
    expect(any(x["direction"] == "output" and x["is_stream"] for x in items), "存在流式输出记录")
    expect(all(x["matched_terms"] for x in items), "记录保留命中词", json.dumps(items[0], ensure_ascii=False)[:200])

    rr = client.get(f"/api/admin/safety/records?direction=output&policy_id={policy['id']}", headers=H)
    expect(rr.status_code == 200 and rr.json()["total"] >= 2, "按方向/规则筛选记录", rr.text[:200])

    # ---------- 8. 日志按审查结果过滤 ----------
    print("\n== 日志过滤 ==")
    rr = client.get("/api/admin/logs?page_size=50&safety_result=blocked_output", headers=H)
    expect(rr.status_code == 200, "日志按审查结果过滤可用", rr.text[:200])
    expect(all(x["safety_result"] == "blocked_output" for x in rr.json()["items"]), "过滤结果一致")

    # ---------- 9. 试审查接口 ----------
    print("\n== 试审查接口 ==")
    r = client.post("/api/admin/safety/test", headers=H,
                    json={"text": "这里有 bad123", "direction": "input", "policy_id": policy["id"]})
    expect(r.status_code == 200, "试审查接口可用", r.text)
    expect(r.json()["blocked"] is True, "正则词命中", r.text)
    expect(any(m["term"].startswith("re:") for m in r.json()["matched"]), "返回正则命中项", r.text)

    # ---------- 10. 词库被引用时禁止删除 ----------
    print("\n== 词库引用保护 ==")
    r = client.delete(f"/api/admin/safety/libraries/{lib1['id']}", headers=H)
    expect(r.status_code == 400, "被规则引用的词库禁止删除", f"{r.status_code} {r.text[:120]}")

print(f"\n✅ 全部通过：{len(PASSED)} 项")
