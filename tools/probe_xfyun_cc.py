"""排查讯飞CC（One API 风格网关）在某分组下的可用模型与渠道状态。

在容器内运行：
    docker exec simple-gateway python /tmp/probe_xfyun_cc.py [模型名]

只读取 provider 的凭证用于发请求，不打印凭证本身。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

from app.core.db import SessionLocal
from app.core.models import Provider
from app.core.providers import get_credentials, provider_settings

NAME = "讯飞CC"
probe_models = [m for m in sys.argv[1:] if m]


def call(url: str, key: str, payload: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


db = SessionLocal()
p = db.query(Provider).filter(Provider.name == NAME).one()
key = str(get_credentials(p).get("api_key") or "")
cfg = provider_settings(p)
base = (p.base_url or "").rstrip("/")
print(f"provider={p.name} type={p.type} base={base}")
print(f"settings={ {k: v for k, v in cfg.items() if k != 'api_key'} }")
print(f"api_key 前缀={key[:6]}… 长度={len(key)}")

status, body = call(f"{base}/models", key)
print(f"\n[GET /models] -> {status}")
if status == 200:
    try:
        rows = json.loads(body).get("data") or []
        names = sorted({r.get("id", "") for r in rows})
        print(f"共 {len(names)} 个模型：")
        for n in names:
            print("  ", n)
    except Exception:  # noqa: BLE001
        print(body[:800])
else:
    print(body[:800])

for probe_model in probe_models:
    print(f"\n[POST chat/completions] model={probe_model}")
    st, bd = call(
        f"{base}/chat/completions",
        key,
        {
            "model": probe_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 64,
        },
    )
    brief = bd[:300].replace("\n", " ")
    print(f"-> {st} {brief}")

db.close()
