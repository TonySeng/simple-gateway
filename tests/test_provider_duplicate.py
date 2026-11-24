"""厂商配置「复制」接口 + 附加字段数据契约回归测试。

背景：生产上 OCR「移动云152OCR」的 settings.extra_fields 是 JSON 对象，
旧版后台表单用 String(obj) 渲染成 [object Object]，保存时还会把这个坏值写回库
（生产「移动云中转UOCR」已中招）。本测试锁定两件事：

1. 服务端存/读的附加字段始终是对象，且注册表声明了 json 字段标记；
2. duplicate 接口完整复制配置（含加密凭证）、副本停用不抢默认、重名自动编号。

运行：.venv\\Scripts\\python.exe -m tests.test_provider_duplicate
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_provider_duplicate.db")
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

from app.core.admin_store import ensure_admin_seeded  # noqa: E402
from app.core.db import SessionLocal, ensure_extra_columns, init_db  # noqa: E402
from app.core.models import Provider  # noqa: E402
from app.core.providers import get_credentials  # noqa: E402
from app.core.registry import get_service, service_meta  # noqa: E402
from app.main import app  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


init_db()
ensure_extra_columns()
# 管理员口令是在 app lifespan 里播种的；TestClient 不进 with 时不会触发，这里显式补上
with SessionLocal() as _db:
    ensure_admin_seeded(_db)
client = TestClient(app)
r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
expect(r.status_code == 200 and bool(r.json().get("access_token")), "管理员登录成功", str(r.text))
H = {"Authorization": "Bearer " + r.json()["access_token"]}

print("\n[1] 附加字段的数据契约")
spec = get_service("ocr")
expect(spec is not None, "OCR 服务已注册")
extra_field = next(
    (f for f in spec.provider_type("generic").setting_fields if f["name"] == "extra_fields"),
    None,
)
expect(extra_field is not None, "注册表含 extra_fields 字段")
expect(extra_field.get("json") is True, "extra_fields 声明 json=True（前端改用 textarea）")
meta = next(s for s in service_meta() if s["key"] == "ocr")
generic_meta = next(t for t in meta["provider_types"] if t["type"] == "generic")
expect(
    any(f.get("json") for f in generic_meta["setting_fields"] if f["name"] == "extra_fields"),
    "service_meta 把 json 标记透传给前端",
)

print("\n[2] 建一条与生产同构的 OCR 配置（附加字段为对象）")
SRC = {
    "service": "ocr",
    "type": "generic",
    "name": "移动云152OCR",
    "base_url": "",
    "is_enabled": True,
    "is_default": True,
    "weight": 88,
    "priority": 7,
    "timeout_s": 45,
    "credentials": {"api_key": "secret-abc-1234"},
    "settings": {
        "url": "http://223.111.149.152:8090/tuling/uocr/v2/recognize",
        "image_mode": "multipart",
        "extra_fields": {"category": "", "trackId": "gateway"},
    },
    "comment": "生产配置",
}
r = client.post("/api/admin/providers", json=SRC, headers=H)
expect(r.status_code == 201, "源配置创建成功", str(r.text))
src = r.json()
expect(src["settings"]["extra_fields"] == {"category": "", "trackId": "gateway"},
       "附加字段以 JSON 对象回读，不是 [object Object]", str(src["settings"]))
expect(src["has_credentials"] is True and src["masked_credentials"]["api_key"].endswith("1234"),
       "凭证加密存储且只回显掩码", str(src["masked_credentials"]))

print("\n[3] 复制配置")
r = client.post(f"/api/admin/providers/{src['id']}/duplicate", headers=H)
expect(r.status_code == 201, "duplicate 返回 201", str(r.text))
dup = r.json()
expect(dup["name"] == "移动云152OCR（副本）", "名称追加（副本）", dup["name"])
expect(dup["id"] != src["id"], "副本是独立记录")
for key in ("service", "type", "base_url", "weight", "priority", "timeout_s", "comment", "settings"):
    expect(dup[key] == src[key], f"字段 {key} 已复制", f"{dup[key]!r} vs {src[key]!r}")
expect(dup["is_enabled"] is False, "副本默认停用，不参与生产路由")
expect(dup["is_default"] is False, "副本不抢占默认位")
expect(dup["has_credentials"] is True, "副本带上了凭证")

with SessionLocal() as db:
    src_row = db.query(Provider).filter(Provider.id == src["id"]).first()
    dup_row = db.query(Provider).filter(Provider.id == dup["id"]).first()
    expect(get_credentials(dup_row) == get_credentials(src_row), "凭证明文完整复制（同密钥可解密）")

r = client.get("/api/admin/providers?service=ocr", headers=H)
expect(any(p["id"] == dup["id"] for p in r.json()), "副本出现在配置列表")
src_now = next(p for p in r.json() if p["id"] == src["id"])
expect(src_now["is_enabled"] is True and src_now["is_default"] is True, "原配置状态未被改动")

print("\n[4] 重名编号与回退")
r = client.post(f"/api/admin/providers/{src['id']}/duplicate", headers=H)
expect(r.status_code == 201 and r.json()["name"] == "移动云152OCR（副本2）",
       "第二次复制自动编号为（副本2）", r.text)
dup2_id = r.json()["id"]
r = client.post(f"/api/admin/providers/{dup['id']}/duplicate", headers=H)
expect(r.status_code == 201 and r.json()["name"] == "移动云152OCR（副本）（副本）",
       "从副本再复制，基于副本自身名称追加", r.text)
expect(client.delete(f"/api/admin/providers/{dup2_id}", headers=H).status_code == 204, "删除副本2")
r = client.post(f"/api/admin/providers/{src['id']}/duplicate", headers=H)
expect(r.json()["name"] == "移动云152OCR（副本2）", "空出的编号被复用", r.text)

print("\n[5] settings 深拷贝：改副本不串改源")
patched = {**dup["settings"], "extra_fields": {"category": "changed", "trackId": "gw2"}}
r = client.put(f"/api/admin/providers/{dup['id']}", json={
    "name": "移动云152OCR-副本改名", "settings": patched, "is_enabled": True,
}, headers=H)
expect(r.status_code == 200, "副本可正常编辑保存", r.text)
expect(r.json()["name"] == "移动云152OCR-副本改名", "副本可改名")
expect(r.json()["is_enabled"] is True, "测试通过后副本可启用")
r = client.get(f"/api/admin/providers/{src['id']}", headers=H)
expect(r.json()["settings"]["extra_fields"] == {"category": "", "trackId": "gateway"},
       "源配置的附加字段未被副本修改污染", str(r.json()["settings"]))

print("\n[6] 边界与鉴权")
r = client.post("/api/admin/providers/no-such-id/duplicate", headers=H)
expect(r.status_code == 404, "不存在的配置返回 404", str(r.status_code))
r = client.post(f"/api/admin/providers/{src['id']}/duplicate")
expect(r.status_code in (401, 403), "未登录复制被拒绝", str(r.status_code))

long_name = "长" * 130
r = client.post("/api/admin/providers", json={
    "service": "ocr", "type": "generic", "name": long_name,
    "settings": {}, "credentials": {},
}, headers=H)
expect(r.status_code == 201, "超长名称源配置创建成功", r.text)
long_id = r.json()["id"]
r = client.post(f"/api/admin/providers/{long_id}/duplicate", headers=H)
expect(r.status_code == 201 and len(r.json()["name"]) <= 128,
       "超长名称复制后仍不超过 name 字段上限 128", str(len(r.json().get("name", ""))))
expect(r.json()["name"].endswith("（副本）"), "超长名称仍带（副本）后缀")

print(f"\n全部通过：{len(PASSED)} 项断言")
