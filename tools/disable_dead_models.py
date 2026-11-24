"""停用「上游无渠道」的模型（默认演练，加 --apply 才真正改）。

背景：2026-11-11 实测讯飞CC 分组 IT-CC-L1-K 下，下列 8 个模型全部返回
503 `model_not_found: 无可用渠道（distributor）`，而网关 `models` 表里仍是启用状态。
请求打进来只会得到一个失败，且在明细里堆噪声。

用法（容器内）：
    docker exec simple-gateway python /tmp/disable_dead_models.py            # 演练
    docker exec simple-gateway python /tmp/disable_dead_models.py --apply    # 执行
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SG_INNER_BASE", "http://127.0.0.1:8000")
ADMIN = ("admin", "Iflytek@2026#")
PROVIDER = "讯飞CC"
APPLY = "--apply" in sys.argv

DEAD = [
    "claude-fable-5",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-6-thinking",
    "claude-opus-4-7-thinking",
    "claude-opus-5-thinking",
    "claude-sonnet-4-6-thinking",
    "claude-sonnet-5-thinking",
]

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
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


st, body = call("/api/admin/login", {"username": ADMIN[0], "password": ADMIN[1]})
if st != 200:
    print(f"登录失败 {st}: {body[:200]}")
    raise SystemExit(1)
token = json.loads(body)["access_token"]

st, body = call("/api/admin/providers", token=token)
providers = json.loads(body) if st == 200 else []
target = next((p for p in providers if p["name"] == PROVIDER), None)
if not target:
    print(f"找不到厂商 {PROVIDER}")
    raise SystemExit(1)

st, body = call(f"/api/admin/models?provider_id={target['id']}", token=token)
models = json.loads(body) if st == 200 else []
by_name = {m["name"]: m for m in models}
print(f"{PROVIDER} 下共 {len(models)} 个模型；待停用名单 {len(DEAD)} 个")

changed = skipped = missing = 0
for name in DEAD:
    m = by_name.get(name)
    if not m:
        print(f"  [缺失] {name}（模型表里没有）")
        missing += 1
        continue
    if not m.get("is_active", True):
        print(f"  [已停用] {name}")
        skipped += 1
        continue
    if not APPLY:
        print(f"  [演练] 将停用 {name}（id={m['id']}）")
        changed += 1
        continue
    st, body = call(
        f"/api/admin/models/{m['id']}", {"is_active": False}, token, method="PUT"
    )
    if st == 200:
        print(f"  [已停用] {name}")
        changed += 1
    else:
        print(f"  [失败] {name} → {st} {body[:150]}")

print(f"\n{'执行' if APPLY else '演练'}结果：停用 {changed} · 原本已停用 {skipped} · 缺失 {missing}")
if not APPLY:
    print("加 --apply 才真正修改。")

st, body = call(f"/api/admin/models?provider_id={target['id']}", token=token)
if st == 200:
    active = [m["name"] for m in json.loads(body) if m.get("is_active")]
    print(f"\n当前仍启用 {len(active)} 个：")
    for n in sorted(active):
        print("  ", n)
