"""OCR 侧按 Key 并发限制测试。

通过 monkeypatch ``_run_inner`` 为「可控延迟 + 直接返回」来隔离验证 ``_run`` 的
acquire/release 逻辑，不依赖真实 OCR 厂商配置。覆盖：
* 达到上限后返回 429（OCR 现有的 ``{"detail": ...}`` 格式），未进入上游；
* 释放后新请求可进入；
* 不同 Key 互不影响；
* 失败（_run_inner 抛异常）后槽位释放。

运行：.venv\\Scripts\\python.exe -m tests.test_ocr_concurrency
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_ocr_concurrency.db")
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
os.environ["SG_API_KEY_MAX_CONCURRENCY"] = "1"

sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.concurrency import key_concurrency  # noqa: E402
from app.core.db import SessionLocal, ensure_extra_columns, init_db  # noqa: E402
from app.core.models import ApiKey  # noqa: E402
from app.core.security import generate_api_key, hash_api_key  # noqa: E402
from app.main import app  # noqa: E402
from app.services.ocr import router as ocr_router  # noqa: E402
from app.services.ocr.schemas import OCRResult  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


STATE = {"delay": 0.5, "raise": False}


async def fake_run_inner(db, key, image, mime, provider, engine, options, with_raw,
                         endpoint, ip, t0, req_desc, rid):
    await asyncio.sleep(STATE["delay"])
    if STATE["raise"]:
        raise HTTPException(500, detail="mock upstream fail")
    return OCRResult(request_id=rid, provider="mock", provider_name="mock", text="ok",
                     blocks=[], pages=1, char_count=2, cost_ms=int(STATE["delay"] * 1000))


ocr_router._run_inner = fake_run_inner  # type: ignore[assignment]

init_db()
ensure_extra_columns()

# 建两个各限 1 并发的 Key
KEYS: dict[str, tuple[str, str]] = {}
for name in ("ocrA", "ocrB"):
    raw, h = generate_api_key()
    with SessionLocal() as db:
        db.add(ApiKey(name=name, key_hash=h, key_prefix=raw[:12], status="active",
                      allowed_services=["ocr"], allowed_providers={}))
        db.commit()
    KEYS[name] = (raw, h)
    with SessionLocal() as db:
        KEYS[name] = (raw, db.query(ApiKey).filter(ApiKey.key_hash == h).first().id)

AUTH_A = {"Authorization": f"Bearer {KEYS['ocrA'][0]}"}
AUTH_B = {"Authorization": f"Bearer {KEYS['ocrB'][0]}"}
AID, BID = KEYS["ocrA"][1], KEYS["ocrB"][1]
# 有效 PNG 头（_detect_mime 识别为 png，无需真实图像内容即可进入 _run）
IMG = {"image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/wFfAAAAAElFTkSuQmCC"}


def recognize(headers, out, idx):
    try:
        out[idx] = _client.post("/v1/ocr/recognize", headers=headers, json=IMG)
    except Exception as e:  # noqa: BLE001
        out[idx] = e


with TestClient(app) as _client:
    # ---- 1. 并发上限 ----
    STATE["delay"] = 0.5
    STATE["raise"] = False
    out: dict[int, object] = {}
    ts = [threading.Thread(target=recognize, args=(AUTH_A, out, i)) for i in range(2)]
    for t in ts:
        t.start()
        time.sleep(0.08)
    for t in ts:
        t.join()
    codes = sorted(r.status_code if hasattr(r, "status_code") else -1 for r in out.values())
    expect(codes == [200, 429], "OCR 上限 1：1 成功 + 1 限流(429)", str(codes))
    limited = [r for r in out.values() if hasattr(r, "status_code") and r.status_code == 429]
    expect("并发请求已达上限" in limited[0].json().get("detail", ""),
           "OCR 限流 detail 友好提示", limited[0].text[:200])
    expect(key_concurrency.in_use(AID) == 0, "OCR 占用已释放")

    # ---- 2. Key 隔离 ----
    STATE["delay"] = 0.4
    out2: dict[int, object] = {}
    ta = threading.Thread(target=recognize, args=(AUTH_A, out2, 0))
    ta.start()
    time.sleep(0.1)
    rb = _client.post("/v1/ocr/recognize", headers=AUTH_B, json=IMG)
    expect(rb.status_code == 200, "ocrB 不受 ocrA 占满影响", rb.text[:200])
    ta.join()
    expect(key_concurrency.in_use(AID) == 0 and key_concurrency.in_use(BID) == 0,
           "两个 Key 占用均释放")

    # ---- 3. 失败后释放 ----
    STATE["delay"] = 0.05
    STATE["raise"] = True
    rf = _client.post("/v1/ocr/recognize", headers=AUTH_A, json=IMG)
    expect(rf.status_code == 500, "失败透传", rf.text[:200])
    expect(key_concurrency.in_use(AID) == 0, "OCR 失败后占用释放")

print(f"\n全部通过：{len(PASSED)} 项")
