"""线上验收：上游 503 是否原样透传给客户端。

在容器内运行（容器能直连 127.0.0.1:8020，且无代理干扰）：
    docker exec simple-gateway python /tmp/verify_status_passthrough.py [模型名]

会临时创建一个只允许 llm 的 Key，用完后删除，不留残留。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# 容器内应用监听 8000，宿主机映射到 8020
BASE = os.environ.get("SG_INNER_BASE", "http://127.0.0.1:8000")
ADMIN = ("admin", "Iflytek@2026#")
model = sys.argv[1] if len(sys.argv) > 1 else "claude-fable-5"

# 容器内若有 http_proxy，会把 127.0.0.1 也代理走，必须显式绕过
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path: str, payload=None, token: str = "", method: str = "") -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with opener.open(req, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


st, body = call("/api/admin/login", {"username": ADMIN[0], "password": ADMIN[1]})
if st != 200:
    print(f"登录失败 {st}: {body[:200]}")
    raise SystemExit(1)
token = json.loads(body)["access_token"]
print(f"管理员登录 {st}")

st, body = call("/api/admin/keys", {"name": "tmp-status-check", "allowed_services": ["llm"]}, token)
if st not in (200, 201):
    print(f"建 Key 失败 {st}: {body[:200]}")
    raise SystemExit(1)
key = json.loads(body)
kid, secret = key["id"], key["key"]
print(f"临时 Key 已建 {kid}")

try:
    st, body = call(
        "/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
        secret,
    )
    print(f"\n[非流式] model={model} -> HTTP {st}")
    print(body[:500])

    st2, body2 = call(
        "/v1/chat/completions",
        {"model": model, "stream": True,
         "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
        secret,
    )
    print(f"\n[流式] model={model} -> HTTP {st2}")
    print(body2[:300])
finally:
    st, body = call(f"/api/admin/keys/{kid}", None, token, method="DELETE")
    print(f"\n清理临时 Key -> {st}")
