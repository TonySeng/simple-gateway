"""OCR 接口 422 的成因与回归测试。

覆盖三种真实踩坑姿势，确保：
* 表单 / multipart 打到「只收 JSON」的端点 → 422（**不是 500**），并给出 hint；
* 上传端点缺 ``file`` 字段或字段名写错 → 422 且 hint 指明「字段名必须是 file」；
* 空 JSON 仍走业务校验 → 400 而非 422。

运行：.venv\\Scripts\\python.exe -m tests.test_ocr_validation
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_ocr_validation.db")
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

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import SessionLocal, ensure_extra_columns, init_db  # noqa: E402
from app.core.models import ApiKey  # noqa: E402
from app.core.security import generate_api_key, hash_api_key  # noqa: E402
from app.main import app  # noqa: E402

PASSED: list[str] = []
BOUNDARY = "----TestBoundary123"
# 故意包含非 UTF-8 字节（0x89 0xff），用来复现 jsonable_encoder 解码崩溃
BINARY = bytes.fromhex("89504e470d0a1a0a") + b"\x00\x01\x02\xff" + b"\x1b\x80"


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


def multipart(field: str, filename: str, content: bytes) -> bytes:
    head = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{BOUNDARY}--\r\n".encode()


RAW_KEY, KEY_HASH = generate_api_key()
init_db()
ensure_extra_columns()
with SessionLocal() as db:
    if not db.query(ApiKey).filter(ApiKey.key_hash == KEY_HASH).first():
        db.add(
            ApiKey(
                name="ocr-validation-test",
                key_hash=KEY_HASH,
                key_prefix=RAW_KEY[:12],
                status="active",
                allowed_services=["ocr"],
                allowed_providers={},
                quota_requests=0,
            )
        )
        db.commit()

AUTH = {"Authorization": f"Bearer {RAW_KEY}"}
MULTI = {**AUTH, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}

print("\n[1] 表单(urlencoded) 打到 JSON 端点")
with TestClient(app) as c:
    r = c.post(
        "/v1/ocr/recognize",
        headers={**AUTH, "Content-Type": "application/x-www-form-urlencoded"},
        content="image_base64=abc",
    )
    expect(r.status_code == 422, "返回 422", f"got {r.status_code} {r.text[:200]}")
    body = r.json()
    expect("hint" in body and "JSON" in body["hint"], "hint 指明要用 JSON", str(body)[:200])

    print("\n[2] multipart 二进制 打到 JSON 端点（原为 500）")
    r = c.post("/v1/ocr/recognize", headers=MULTI, content=multipart("file", "a.png", BINARY))
    expect(r.status_code == 422, "返回 422 而非 500", f"got {r.status_code} {r.text[:300]}")
    expect("hint" in r.json(), "仍带 hint", r.text[:200])
    expect("<binary" in r.text or "\\ufffd" not in r.text, "二进制 input 已安全降级", r.text[:300])

    print("\n[3] 上传端点用 JSON 调用（缺 file）")
    r = c.post("/v1/ocr/recognize/file", headers=AUTH, json={})
    expect(r.status_code == 422, "返回 422", f"got {r.status_code} {r.text[:200]}")
    expect("file" in r.text, "detail 指向 file 字段", r.text[:200])
    expect("multipart" in (r.json().get("hint") or ""), "hint 指明 multipart + 字段名 file", r.text[:200])

    print("\n[4] 上传端点字段名写错（image 而非 file）")
    r = c.post("/v1/ocr/recognize/file", headers=MULTI, content=multipart("image", "a.png", BINARY))
    expect(r.status_code == 422, "返回 422", f"got {r.status_code} {r.text[:200]}")
    expect("multipart" in (r.json().get("hint") or ""), "hint 可定位到字段名问题", r.text[:200])

    print("\n[5] 空 JSON 走业务校验（应 400）")
    r = c.post("/v1/ocr/recognize", headers=AUTH, json={})
    expect(r.status_code == 400, "返回 400 而非 422", f"got {r.status_code} {r.text[:200]}")
    expect("image_base64" in r.text, "提示需提供 image_base64/image_url", r.text[:200])

print(f"\n✅ 全部通过：{len(PASSED)} 项")
