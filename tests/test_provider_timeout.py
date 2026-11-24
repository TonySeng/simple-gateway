"""按厂商配置上游超时（ReadTimeout 治理）。

覆盖的关键点：
1. resolve_timeout：未配置 / 0 / 负数 回落全局默认；正数覆盖
2. httpx_timeout：读超时用厂商值，连接超时单独封顶
3. 管理接口：创建 / 更新 / 读取 timeout_s（0 = 显式恢复默认，null = 不改）
4. **不同厂商各自的超时真的传到了 HTTP 层**（Assert request.extensions["timeout"]）
5. 流式请求同样生效
6. 超时错误信息里带上实际阈值，便于判断是「配得太短」还是上游真挂了
7. OCR 厂商共用同一套配置
8. 旧库自动补列（ensure_extra_columns）

运行：.venv\\Scripts\\python.exe -m tests.test_provider_timeout
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_provider_timeout.db")
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

from app.core.config import settings  # noqa: E402
from app.core.providers import CONNECT_TIMEOUT_CAP, httpx_timeout, resolve_timeout  # noqa: E402
from app.main import app  # noqa: E402
from app.services.llm import proxy as llm_proxy  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


class _Fake:
    def __init__(self, timeout_s):
        self.timeout_s = timeout_s


# ---------------- 单元：解析 ----------------
print("\n== 超时解析 ==")
GLOBAL = float(settings.proxy_timeout)
expect(resolve_timeout(_Fake(None)) == GLOBAL, f"未配置回落全局 {GLOBAL}s", resolve_timeout(_Fake(None)))
expect(resolve_timeout(_Fake(0)) == GLOBAL, "0 回落全局默认", resolve_timeout(_Fake(0)))
expect(resolve_timeout(_Fake(None) if False else _Fake(-1)) == GLOBAL, "负数回落全局默认")
expect(resolve_timeout(_Fake(600)) == 600.0, "600s 生效", resolve_timeout(_Fake(600)))
expect(httpx_timeout(_Fake(600)).read == 600.0, "httpx read=600")
expect(httpx_timeout(_Fake(600)).connect == CONNECT_TIMEOUT_CAP,
       f"连接超时封顶 {CONNECT_TIMEOUT_CAP}s（不跟着读超时一起放大）",
       httpx_timeout(_Fake(600)).connect)
expect(httpx_timeout(_Fake(8)).connect == 8.0, "小于封顶值时连接超时跟随厂商值")
expect(httpx_timeout(_Fake(8)).read == 8.0, "小于封顶值时读超时跟随厂商值")

# ---------------- 转发层探针 ----------------
STATE = {"mode": "ok"}
SEEN: list[dict] = []

OK_JSON = {
    "id": "chatcmpl-mock", "object": "chat.completion", "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _handler(request: httpx.Request) -> httpx.Response:
    # 关键探针：httpx 会把 per-request timeout 写进 extensions
    SEEN.append(dict(request.extensions.get("timeout") or {}))
    mode = STATE["mode"]
    if mode == "timeout":
        raise httpx.ReadTimeout("read timed out", request=request)
    if mode == "stream_ok":
        chunk = {"id": "c1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": "hi"}}]}
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
        )
    if mode == "not_found":
        raise httpx.ConnectError("no such host", request=request)
    return httpx.Response(200, json=OK_JSON)


client = TestClient(app)
with client:
    llm_proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    print("\n== 准备数据 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录成功", r.text)
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # 厂商 A：600s
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "slow-a",
        "base_url": "http://slow-a.local/v1", "timeout_s": 600,
        "credentials": {"api_key": "sk-a"},
    })
    expect(r.status_code == 201, "创建厂商 A(timeout_s=600)", r.text)
    pa = r.json()
    expect(pa["timeout_s"] == 600, "返回 timeout_s=600", str(pa.get("timeout_s")))
    expect(pa["effective_timeout_s"] == 600, "effective_timeout_s=600", str(pa.get("effective_timeout_s")))

    # 厂商 B：45s
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "fast-b",
        "base_url": "http://fast-b.local/v1", "timeout_s": 45,
        "credentials": {"api_key": "sk-b"},
    })
    expect(r.status_code == 201, "创建厂商 B(timeout_s=45)", r.text)
    pb = r.json()

    # 厂商 C：不配超时
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "llm", "type": "openai", "name": "default-c",
        "base_url": "http://default-c.local/v1",
        "credentials": {"api_key": "sk-c"},
    })
    expect(r.status_code == 201, "创建厂商 C(未配置超时)", r.text)
    pc = r.json()
    expect(pc["timeout_s"] is None, "未配置时 timeout_s 为 null", str(pc.get("timeout_s")))
    expect(pc["effective_timeout_s"] == GLOBAL, f"未配置时实际生效 {GLOBAL}s", str(pc.get("effective_timeout_s")))

    # 模型
    for pid, mname in ((pa["id"], "ma"), (pb["id"], "mb"), (pc["id"], "mc")):
        r = client.post("/api/admin/models", headers=H, json={
            "provider_id": pid, "name": mname, "provider_model": mname,
        })
        expect(r.status_code == 201, f"新增模型 {mname}", r.text)

    r = client.post("/api/admin/keys", headers=H, json={"name": "k1", "allowed_services": ["llm"]})
    expect(r.status_code == 201, "创建 Key", r.text)
    KEY = {"Authorization": f"Bearer {r.json()['key']}"}

    def chat(model, stream=False):
        body = {"model": model, "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]}
        if stream:
            body["stream"] = True
        SEEN.clear()
        return client.post("/v1/chat/completions", headers=KEY, json=body)

    def last_log():
        rr = client.get("/api/admin/logs?page_size=1", headers=H)
        items = rr.json().get("items") or []
        expect(bool(items), "存在日志", rr.text)
        return items[0]

    # ---------- 1. 每家厂商的超时各自生效 ----------
    print("\n== 按厂商超时真正下发到 HTTP 层 ==")
    r = chat("ma")
    expect(r.status_code == 200, "厂商 A 请求成功", r.text[:200])
    expect(SEEN and SEEN[-1].get("read") == 600.0, "A 的 read timeout = 600", str(SEEN))
    expect(SEEN[-1].get("connect") == CONNECT_TIMEOUT_CAP, "A 的 connect timeout 封顶",
           str(SEEN[-1]))

    r = chat("mb")
    expect(r.status_code == 200, "厂商 B 请求成功", r.text[:200])
    expect(SEEN and SEEN[-1].get("read") == 45.0, "B 的 read timeout = 45", str(SEEN))

    r = chat("mc")
    expect(r.status_code == 200, "厂商 C 请求成功", r.text[:200])
    expect(SEEN and SEEN[-1].get("read") == GLOBAL, f"C 用全局默认 {GLOBAL}", str(SEEN))

    # 交叉验证：再打一次 A，确认没被 B 覆盖
    r = chat("ma")
    expect(SEEN and SEEN[-1].get("read") == 600.0, "回到 A 仍是 600（互不污染）", str(SEEN))

    # ---------- 2. 流式同样生效 ----------
    print("\n== 流式按厂商超时 ==")
    STATE["mode"] = "stream_ok"
    r = chat("mb", stream=True)
    expect(r.status_code == 200 and "data:" in r.text, "流式请求成功", r.text[:120])
    expect(SEEN and SEEN[-1].get("read") == 45.0, "流式 read timeout = 45", str(SEEN))
    STATE["mode"] = "ok"

    # ---------- 3. 超时错误信息带阈值 ----------
    print("\n== 超时错误信息带实际阈值 ==")
    STATE["mode"] = "timeout"
    r = chat("mb")
    expect(r.status_code == 502, "超时返回 502", r.status_code)
    log = last_log()
    expect(log["error_type"] == "upstream_timeout", "error_type=upstream_timeout", log["error_type"])
    expect("45s" in log["error_message"], "错误信息含实际阈值 45s，便于判断是否配得太短",
           log["error_message"])
    expect("读取超时" in log["error_message"], "区分读超时与连接超时", log["error_message"])

    r = chat("ma")
    log = last_log()
    expect("600s" in log["error_message"], "同一错误在不同厂商显示各自阈值 600s", log["error_message"])
    STATE["mode"] = "ok"

    # ---------- 4. 更新语义 ----------
    print("\n== 更新语义：0=恢复默认，null=不改 ==")
    r = client.put(f"/api/admin/providers/{pb['id']}", headers=H, json={"comment": "只改备注"})
    expect(r.status_code == 200, "更新备注成功", r.text)
    expect(r.json()["timeout_s"] == 45, "未传 timeout_s 时保持 45 不变", str(r.json().get("timeout_s")))

    r = client.put(f"/api/admin/providers/{pb['id']}", headers=H, json={"timeout_s": 0})
    expect(r.status_code == 200, "timeout_s=0 更新成功", r.text)
    expect(r.json()["timeout_s"] is None, "0 表示恢复默认（落库为 null）", str(r.json().get("timeout_s")))
    expect(r.json()["effective_timeout_s"] == GLOBAL, "恢复默认后生效值为全局默认",
           str(r.json().get("effective_timeout_s")))

    r = chat("mb")
    expect(SEEN and SEEN[-1].get("read") == GLOBAL, "恢复默认后请求使用全局超时", str(SEEN))

    r = client.put(f"/api/admin/providers/{pb['id']}", headers=H, json={"timeout_s": 90})
    expect(r.json()["timeout_s"] == 90, "再设回 90s", str(r.json().get("timeout_s")))

    # ---------- 5. 非法值 ----------
    print("\n== 参数校验 ==")
    for bad in (-1, 4000):
        rr = client.post("/api/admin/providers", headers=H, json={
            "service": "llm", "type": "openai", "name": f"bad{bad}",
            "timeout_s": bad, "credentials": {"api_key": "sk-x"},
        })
        expect(rr.status_code == 422, f"timeout_s={bad} 被拒绝", f"{rr.status_code} {rr.text[:120]}")

    # ---------- 6. OCR 共用同一配置 ----------
    print("\n== OCR 厂商同样支持 ==")
    r = client.post("/api/admin/providers", headers=H, json={
        "service": "ocr", "type": "generic", "name": "ocr-slow",
        "base_url": "http://ocr.local/rec", "timeout_s": 30,
        "settings": {"url": "http://ocr.local/rec"},
    })
    expect(r.status_code == 201, "创建 OCR 厂商(timeout_s=30)", r.text)
    expect(r.json()["timeout_s"] == 30, "OCR 厂商 timeout_s=30", str(r.json().get("timeout_s")))
    expect(r.json()["effective_timeout_s"] == 30, "OCR 厂商生效值 30s",
           str(r.json().get("effective_timeout_s")))

    # ---------- 7. 列表接口带字段 ----------
    print("\n== 列表接口 ==")
    rr = client.get("/api/admin/providers?service=llm", headers=H)
    rows = rr.json() if isinstance(rr.json(), list) else rr.json().get("items") or []
    byname = {x["name"]: x for x in rows}
    expect(byname["slow-a"]["timeout_s"] == 600, "列表中 A 显示 600")
    expect(byname["default-c"]["effective_timeout_s"] == GLOBAL, "列表中 C 显示生效默认值")

print("\n== 旧库自动补列 ==")
# 模拟线上旧库：建一个没有 timeout_s 的 providers 表，跑 ensure_extra_columns
import sqlite3  # noqa: E402

from app.core import db as core_db  # noqa: E402

LEGACY = os.path.join(ROOT, "data", "test_legacy_providers.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(LEGACY + suffix)
    except FileNotFoundError:
        pass
conn = sqlite3.connect(LEGACY)
conn.execute(
    "CREATE TABLE providers (id VARCHAR(32) PRIMARY KEY, service VARCHAR(32), "
    "type VARCHAR(32), name VARCHAR(128), base_url VARCHAR(512), "
    "credentials_enc TEXT, settings JSON, is_enabled BOOLEAN, is_default BOOLEAN, "
    "weight INTEGER, priority INTEGER, comment TEXT, created_at DATETIME, updated_at DATETIME)"
)
conn.commit()
conn.close()

from sqlalchemy import create_engine, inspect, text  # noqa: E402

legacy_engine = create_engine("sqlite:///" + LEGACY.replace("\\", "/"))
orig_engine = core_db.engine
core_db.engine = legacy_engine
try:
    core_db.ensure_extra_columns()
    core_db.ensure_extra_columns()  # 幂等
finally:
    core_db.engine = orig_engine

cols = {c["name"] for c in inspect(legacy_engine).get_columns("providers")}
expect("timeout_s" in cols, "旧 providers 表已补齐 timeout_s 列")
with legacy_engine.begin() as c:
    c.execute(text("INSERT INTO providers (id, service, name, timeout_s) VALUES ('x','llm','n',120)"))
with legacy_engine.connect() as c:
    v = c.execute(text("SELECT timeout_s FROM providers WHERE id='x'")).scalar()
expect(float(v) == 120.0, "补列后可正常读写", str(v))
legacy_engine.dispose()

print(f"\n全部通过：{len(PASSED)} 项")
