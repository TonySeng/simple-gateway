"""端到端冒烟测试（无需真实厂商密钥）。

覆盖：
* 启动 / 建表 / 登录
* 统一厂商 CRUD（OCR + LLM 同一张表、同一套接口）
* 统一 API Key：跨服务授权、服务范围拒绝、配额 429、重置
* LLM 双协议转发（OpenAI 入 / 出）、流式、计费
* OCR 失败降级路径与可读错误
* 统一日志与统计（按服务维度）

运行：.venv\\Scripts\\python.exe -m tests.smoke
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "smoke.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB_PATH + suffix)
    except FileNotFoundError:
        pass
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 必须在导入 app 之前设定环境（Settings 在导入时即加载）
os.environ["SG_DATABASE_URL"] = "sqlite:///" + DB_PATH.replace("\\", "/")
os.environ["SG_ADMIN_USERNAME"] = "admin"
os.environ["SG_ADMIN_PASSWORD"] = "Smoke-Admin-2026!"
os.environ["SG_JWT_SECRET"] = "smoke-secret-0123456789-0123456789-0123"
os.environ["SG_FERNET_KEY"] = "8coHn0mS0Xk8mS3m1oQ0K3xH0k1m4r2y6V8b9N1c2Z0="
os.environ["SG_LOG_REQUESTS"] = "true"

sys.path.insert(0, ROOT)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402
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


# ---------------- 上游 mock（OpenAI 协议 + PaddleOCR）----------------
def _mock_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "127.0.0.1":  # 模拟不可达的私有化 OCR
        return httpx.Response(503, json={"error": "down"})
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "mock-a"}, {"id": "mock-b", "owned_by": "x"}]})
    if "paddle" in request.url.path:
        return httpx.Response(200, json={
            "data": [
                {"text": "MOCK OCR 123", "confidence": 0.99,
                 "text_region": [[10, 20], [210, 20], [210, 60], [10, 60]]},
                {"text": "第二行", "confidence": 0.95,
                 "text_region": [[10, 70], [110, 70], [110, 100], [10, 100]]},
            ]
        })

    body = json.loads(request.content or b"{}")
    if body.get("stream"):
        sse = (
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你"}}]}\n\n'
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"好"}}]}\n\n'
            'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


client = TestClient(app)

with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    ocr_base._client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))

    print("\n== 基础 ==")
    r = client.get("/health")
    expect(r.status_code == 200 and set(r.json()["services"]) == {"ocr", "llm"}, "健康检查返回两个服务", r.text)

    r = client.get("/api/admin/config")
    cfg = r.json()
    expect(cfg["auth_enabled"] is True and len(cfg["services"]) == 2, "启动配置含服务元信息")
    expect(any(t["type"] == "baidu" for s in cfg["services"] if s["key"] == "ocr" for t in s["provider_types"]),
           "OCR 厂商表单元信息可下发")

    print("\n== 登录 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "wrong"})
    expect(r.status_code == 401, "错误口令返回 401")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Smoke-Admin-2026!"})
    expect(r.status_code == 200, "正确口令登录成功", r.text)
    token = r.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/admin/keys")
    expect(r.status_code == 401, "未带令牌访问管理接口返回 401")

    print("\n== 厂商配置（统一表）==")
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "baidu", "name": "baidu-main",
        "credentials": {"api_key": "ak-1234", "secret_key": "sk-5678", "app_id": "999"},
        "settings": {"url": "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"},
        "weight": 200,
    })
    expect(r.status_code == 201, "新建 OCR 厂商成功", r.text)
    baidu = r.json()
    expect(baidu["missing_credentials"] == [] and baidu["masked_credentials"]["api_key"] == "****1234",
           "凭证已加密且回显掩码")
    expect(baidu["service_label"] == "OCR 文字识别", "服务标签正确")

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "tencent", "name": "tencent-bad", "credentials": {},
    })
    expect(r.status_code == 400 and "secret_id" in r.text, "缺必填凭证被拦截")

    # 默认 OCR 厂商：私有化 PaddleOCR（mock 地址），用于验证自动路由与成功路径
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "paddle", "name": "mock-paddle",
        "is_default": True, "weight": 300,
        "settings": {"url": "http://mock.local/paddle/ocr"},
    })
    expect(r.status_code == 201, "新建私有化 OCR 厂商成功", r.text)
    paddle_id = r.json()["id"]
    expect(r.json()["has_credentials"] is False and r.json()["missing_credentials"] == [],
           "无凭证类型不误报缺失")

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "mock-openai",
        "base_url": "http://mock.local/v1", "credentials": {"api_key": "sk-upstream"},
        "is_default": True,
    })
    expect(r.status_code == 201, "新建 LLM 供应商成功", r.text)
    llm_pid = r.json()["id"]

    # 凭证合并语义：只改 api_key，不应冲掉其它项
    r = client.put(f"/api/admin/providers/{baidu['id']}", headers=H,
                   json={"credentials": {"secret_key": "sk-NEW1"}})
    expect(r.status_code == 200 and r.json()["masked_credentials"]["api_key"] == "****1234",
           "只改一项凭证不会冲掉其它项")

    # 同名冲突
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "mock-openai", "base_url": "x",
    })
    expect(r.status_code == 409, "同服务同名配置冲突返回 409")

    print("\n== 模型 ==")
    r = client.post("/api/admin/models", headers=H, json={
        "provider_id": llm_pid, "name": "mock-chat", "provider_model": "mock-a",
        "input_price": 0.001, "output_price": 0.002, "currency": "CNY",
    })
    expect(r.status_code == 201, "新增模型成功", r.text)
    model_id = r.json()["id"]

    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "generic", "name": "broken-ocr",
        "settings": {"url": "http://127.0.0.1:1/ocr"},
    })
    broken_ocr = r.json()["id"]

    print("\n== 统一 API Key ==")
    r = client.post("/api/admin/keys", headers=H, json={
        "name": "all-in-one", "allowed_services": ["*"],
        "allowed_providers": {"ocr": ["*"], "llm": ["*"]},
    })
    expect(r.status_code == 201, "新建全服务 Key", r.text)
    key_all = r.json()["key"]
    key_all_id = r.json()["id"]
    expect(key_all.startswith("sg_sk_"), "Key 前缀为 sg_sk_")

    r = client.post("/api/admin/keys", headers=H, json={"name": "ocr-only", "allowed_services": ["ocr"]})
    key_ocr = r.json()["key"]

    r = client.post("/api/admin/keys", headers=H, json={
        "name": "ocr-broken", "allowed_services": ["ocr"], "allowed_providers": {"ocr": ["generic"]},
    })
    key_broken = r.json()["key"]

    r = client.post("/api/admin/keys", headers=H, json={"name": "quota-1", "quota_requests": 1})
    key_quota = r.json()["key"]

    r = client.get("/api/admin/keys", headers=H)
    expect(len(r.json()) == 4 and all("key" not in k or k["key"] is None for k in r.json()),
           "列表不回显明文")

    print("\n== 鉴权与配额 ==")
    r = client.post("/v1/ocr/recognize", json={"image_base64": "aGk="})
    expect(r.status_code == 401, "调用缺 Key 返回 401")
    r = client.post("/v1/ocr/recognize", headers={"Authorization": "Bearer sg_sk_wrong"},
                    json={"image_base64": "aGk="})
    expect(r.status_code == 401, "无效 Key 返回 401")

    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key_ocr}"},
                    json={"model": "mock-chat", "messages": [{"role": "user", "content": "hi"}]})
    expect(r.status_code == 403 and "未授权" in r.text, "服务范围外调用返回 403", r.text)

    print("\n== LLM 转发 ==")
    auth_all = {"Authorization": f"Bearer {key_all}"}
    r = client.post("/v1/chat/completions", headers=auth_all,
                    json={"model": "mock-chat", "messages": [{"role": "user", "content": "你好"}]})
    expect(r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "你好",
           "OpenAI 协议非流式转发成功", r.text)

    r = client.get("/v1/models", headers=auth_all)
    expect(r.status_code == 200 and r.json()["data"][0]["id"] == "mock-chat", "模型列表可用")

    with client.stream("POST", "/v1/chat/completions", headers=auth_all,
                       json={"model": "mock-chat", "stream": True,
                             "messages": [{"role": "user", "content": "你好"}]}) as resp:
        chunks = "".join(resp.iter_text())
    expect(resp.status_code == 200 and "data:" in chunks and "[DONE]" in chunks, "流式转发返回 SSE")

    r = client.post("/v1/chat/completions", headers=auth_all,
                    json={"model": "no-such-model", "messages": []})
    expect(r.status_code == 404, "未知模型返回 404")

    r = client.post("/v1/messages", headers=auth_all,
                    json={"model": "mock-chat", "max_tokens": 64,
                          "messages": [{"role": "user", "content": "你好"}]})
    expect(r.status_code == 200 and r.json()["content"][0]["type"] == "text",
           "Anthropic 入站协议被正确转换", r.text)

    print("\n== OCR 识别 ==")
    r = client.post("/v1/ocr/recognize", headers=auth_all, json={"image_base64": "aGk="})
    expect(r.status_code == 200, "自动路由到默认厂商成功", r.text)
    ocr_out = r.json()
    expect("MOCK OCR 123" in ocr_out["text"] and ocr_out["char_count"] > 0, "识别文本与字符数正确", r.text)
    expect(ocr_out["provider"] == "paddle" and ocr_out["blocks"][0]["bbox"] == [10, 20, 210, 60],
           "结构化区块（bbox）归一化正确", str(ocr_out["blocks"][:1]))

    r = client.post("/v1/ocr/recognize/file", headers=auth_all,
                    files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    expect(r.status_code == 200 and r.json()["char_count"] > 0, "文件上传识别成功", r.text)

    r = client.get("/v1/ocr/types", headers=auth_all)
    expect(r.status_code == 200 and len(r.json()["types"]) == 8, "厂商类型清单返回 8 种")

    r = client.post("/v1/ocr/recognize", headers={"Authorization": f"Bearer {key_broken}"},
                    json={"image_base64": "aGk="})
    expect(r.status_code == 502, "厂商不可用时返回 502 且带可读错误", r.text)

    print("\n== 配额 ==")
    auth_q = {"Authorization": f"Bearer {key_quota}"}
    r1 = client.post("/v1/chat/completions", headers=auth_q,
                     json={"model": "mock-chat", "messages": [{"role": "user", "content": "1"}]})
    r2 = client.post("/v1/chat/completions", headers=auth_q,
                     json={"model": "mock-chat", "messages": [{"role": "user", "content": "2"}]})
    expect(r1.status_code == 200 and r2.status_code == 429, "超出次数配额返回 429", f"{r1.status_code}/{r2.status_code}")

    print("\n== 配置即测 ==")
    r = client.post(f"/api/admin/providers/{llm_pid}/test", headers=H)
    expect(r.status_code == 200 and r.json()["ok"] is True and r.json()["detail"]["models_count"] == 2,
           "LLM 供应商连通性测试通过", r.text)
    r = client.post(f"/api/admin/providers/{paddle_id}/test", headers=H)
    expect(r.status_code == 200 and r.json()["ok"] is True and "MOCK OCR" in r.json()["detail"]["text"],
           "OCR 厂商配置即测通过", r.text)
    r = client.post(f"/api/admin/providers/{broken_ocr}/test", headers=H)
    expect(r.status_code == 200 and r.json()["ok"] is False, "OCR 不可用厂商测试回显失败原因", r.text)

    print("\n== 日志与统计 ==")
    r = client.get("/api/admin/logs", headers=H, params={"service": "llm"})
    body = r.json()
    expect(r.status_code == 200 and body["total"] >= 4, "按服务筛选日志", r.text)
    expect(any(i["tokens_in"] == 10 and i["tokens_out"] == 5 for i in body["items"]), "token 用量已入库")
    expect(any(i["tokens_in"] == 7 for i in body["items"]), "流式 usage 已入库")

    r = client.get("/api/admin/logs", headers=H, params={"service": "ocr"})
    expect(r.json()["total"] >= 1 and any(i["status"] == "error" for i in r.json()["items"]),
           "OCR 失败也记录日志")

    r = client.get("/api/admin/stats/overview", headers=H, params={"days": 7})
    ov = r.json()
    expect(ov["total_requests"] >= 5 and ov["success"] >= 3, "总览统计正确", str(ov))
    expect(len(ov["services"]) == 2 and {s["service"] for s in ov["services"]} == {"ocr", "llm"},
           "统计含服务维度")
    llm_stat = next(s for s in ov["services"] if s["service"] == "llm")
    expect(llm_stat["tokens"] >= 25 and llm_stat["cost"] > 0, "按服务聚合 tokens 与费用", str(llm_stat))

    r = client.get("/api/admin/stats/series", headers=H, params={"days": 7})
    expect(len(r.json()) == 7, "按天趋势返回 7 个点", r.text)

    r = client.get("/api/admin/stats/breakdown", headers=H, params={"days": 7})
    bd = r.json()
    expect(any(x["name"] == "mock-chat" for x in bd["by_model"]), "按模型聚合", r.text)

    print("\n== Key 重置与删除 ==")
    r = client.post(f"/api/admin/keys/{key_all_id}/reset", headers=H)
    new_key = r.json()["key"]
    expect(new_key and new_key != key_all, "重置后返回新明文")
    r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key_all}"},
                    json={"model": "mock-chat", "messages": []})
    expect(r.status_code == 401, "旧 Key 重置后失效")

    r = client.delete(f"/api/admin/models/{model_id}", headers=H)
    expect(r.status_code == 204, "删除模型")
    r = client.delete(f"/api/admin/providers/{llm_pid}", headers=H)
    expect(r.status_code == 204, "删除供应商（级联模型）")

    print("\n== 静态后台 ==")
    r = client.get("/admin/")
    expect(r.status_code == 200 and "Simple Gateway" in r.text, "管理后台页面可访问")
    r = client.get("/")
    expect(r.status_code in (200, 307), "根路径重定向")

print(f"\n全部通过：{len(PASSED)} 项断言")
