"""线上验收：流式请求遇到上游非 2xx 后，实时监控条目必须立即摘除。

复现原始故障：让讯飞CC 返回 503（该分组无 claude-fable-5 渠道），用 stream=true 打过去。
修复前该条目会一直挂在 /api/admin/live 直到 MAX_AGE_S(1800s)；
修复后请求一结束就应归零。

脚本会临时启用 claude-fable-5、建临时 Key，跑完自动停用并删除，不留残留。

用法（容器内）：
    docker exec simple-gateway python /tmp/verify_zombie_fix.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SG_INNER_BASE", "http://127.0.0.1:8000")
ADMIN = ("admin", "Iflytek@2026#")
PROVIDER = "讯飞CC"
MODEL = "claude-fable-5"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path: str, payload=None, token: str = "", method: str = "") -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data else "GET")
    )
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with opener.open(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


st, body = call("/api/admin/login", {"username": ADMIN[0], "password": ADMIN[1]})
if st != 200:
    print(f"登录失败 {st}: {body[:200]}")
    raise SystemExit(1)
token = json.loads(body)["access_token"]

st, body = call("/api/admin/providers", token=token)
prov = next((p for p in json.loads(body) if p["name"] == PROVIDER), None)
st, body = call(f"/api/admin/models?provider_id={prov['id']}", token=token)
model = next((m for m in json.loads(body) if m["name"] == MODEL), None)
print(f"目标模型 {MODEL} id={model['id']} 当前 is_active={model['is_active']}")

st, body = call("/api/admin/live", token=token)
print(f"部署后实时条目：{json.loads(body)['total']}（应为 0，容器重启已清空旧僵尸）")

st, body = call(f"/api/admin/models/{model['id']}", {"is_active": True}, token, method="PUT")
print(f"临时启用 {MODEL} -> {st}")
st, body = call("/api/admin/keys", {"name": "tmp-zombie-check", "allowed_services": ["llm"]}, token)
kid, secret = json.loads(body)["id"], json.loads(body)["key"]

try:
    t0 = time.time()
    st, body = call(
        "/v1/chat/completions",
        {"model": MODEL, "stream": True, "max_tokens": 8,
         "messages": [{"role": "user", "content": "ping"}]},
        secret,
    )
    cost = time.time() - t0
    print(f"\n[流式请求] -> HTTP {st}，耗时 {cost:.2f}s")
    print(body[:220])

    for i in range(3):
        st, body = call("/api/admin/live", token=token)
        d = json.loads(body)
        print(f"  请求结束后第 {i + 1}s：实时条目 {d['total']}")
        if d["total"]:
            print("  残留：", json.dumps(d["items"], ensure_ascii=False)[:300])
        time.sleep(1)
finally:
    st, _ = call(f"/api/admin/models/{model['id']}", {"is_active": False}, token, method="PUT")
    print(f"\n恢复停用 {MODEL} -> {st}")
    st, _ = call(f"/api/admin/keys/{kid}", None, token, method="DELETE")
    print(f"清理临时 Key -> {st}")
