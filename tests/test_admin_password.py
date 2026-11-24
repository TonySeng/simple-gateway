"""管理员强制改密 + 强密码校验（端到端，独立 DB，避免污染主冒烟库）。

运行：python -m tests.test_admin_password
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "pwtest.db")
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except FileNotFoundError:
        pass
os.makedirs(os.path.dirname(DB), exist_ok=True)

# 必须在导入 app 之前设定环境（Settings 在导入时即加载）
os.environ["SG_DATABASE_URL"] = "sqlite:///" + DB.replace("\\", "/")
os.environ["SG_ADMIN_USERNAME"] = "admin"
# 关键：使用默认弱口令 admin123，触发强制改密
os.environ["SG_ADMIN_PASSWORD"] = "admin123"
os.environ["SG_JWT_SECRET"] = "pwtest-secret-0123456789-0123456789-0123"
os.environ["SG_FERNET_KEY"] = "8coHn0mS0Xk8mS3m1oQ0K3xH0k1m4r2y6V8b9N1c2Z0="

sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASSED: list[str] = []


def ok(label: str) -> None:
    PASSED.append(label)
    print(f"  [PASS] {label}")


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    ok(label)


client = TestClient(app)

with client:
    print("\n== 默认弱口令 -> 强制改密 ==")
    r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    expect(r.status_code == 200 and r.json().get("must_change_password") is True,
           "默认口令登录返回 must_change=true", r.text)
    token = r.json()["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/admin/providers", headers=H)
    expect(r.status_code == 403, "强制改密期间普通管理接口返回 403", r.text)

    # 弱新密码应被拒
    r = client.post("/api/admin/change-password", headers=H,
                    json={"current_password": "admin123", "new_password": "short"})
    expect(r.status_code == 400 and "强度" in r.text, "弱新密码被拒绝(400)", r.text)

    # 当前密码错误
    r = client.post("/api/admin/change-password", headers=H,
                    json={"current_password": "wrong", "new_password": "New-Str0ng-P@ss2026"})
    expect(r.status_code == 401, "当前密码错误返回 401")

    # 新密码与当前相同应被拒
    r = client.post("/api/admin/change-password", headers=H,
                    json={"current_password": "admin123", "new_password": "admin123"})
    expect(r.status_code == 400, "新密码与当前相同被拒绝(400)")

    # 成功改密（强密码）
    NEW = "New-Str0ng-P@ss2026"
    r = client.post("/api/admin/change-password", headers=H,
                    json={"current_password": "admin123", "new_password": NEW})
    expect(r.status_code == 200 and r.json().get("must_change_password") is False,
           "改密成功且 must_change=false", r.text)
    new_token = r.json()["access_token"]
    H2 = {"Authorization": f"Bearer {new_token}"}

    r = client.get("/api/admin/providers", headers=H2)
    expect(r.status_code == 200, "改密后管理接口恢复 200", r.text)

    r = client.get("/api/admin/me", headers=H2)
    expect(r.status_code == 200 and r.json().get("must_change_password") is False,
           "/me 返回 must_change=false")

    # 新密码可正常登录，且不再强制改密
    r = client.post("/api/admin/login", json={"username": "admin", "password": NEW})
    expect(r.status_code == 200 and r.json().get("must_change_password") is False,
           "新密码可登录且无需再次改密")

print(f"\n全部通过：{len(PASSED)} 项断言")
