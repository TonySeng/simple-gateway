"""词库导入导出回归测试。

覆盖：
* JSON 全量 / 单库导出，TXT 单库导出与多库拒绝
* 导入模板（JSON / TXT）内容与可直接导入
* 导入三种冲突策略：merge 去重、overwrite 替换、copy 另存副本
* 非法正则、空内容、非法 mode 被拒绝
* builtin_key 匹配、BOM 清理、导出再导入的词条守恒

运行：.venv\\Scripts\\python.exe -m tests.test_safety_import_export
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "test_safety_import_export.db")
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

from app.core.db import SessionLocal  # noqa: E402
from app.core.models import SafetyLibrary  # noqa: E402
from app.main import app  # noqa: E402

PASSED: list[str] = []


def expect(cond: bool, label: str, detail="") -> None:
    if not cond:
        print(f"  [FAIL] {label} {detail}")
        raise SystemExit(1)
    PASSED.append(label)
    print(f"  [PASS] {label}")


def term_count(terms: str) -> int:
    return sum(1 for x in terms.splitlines() if x.strip() and not x.strip().startswith("#"))


client = TestClient(app)
with client:
    r = client.post("/api/admin/login", json={"username": "admin", "password": "Test-Admin-2026!"})
    expect(r.status_code == 200, "管理员登录", r.text[:200])
    H = {"Authorization": "Bearer " + r.json()["access_token"]}

    print("\n== 导出 ==")
    r = client.get("/api/admin/safety/libraries/export?fmt=json", headers=H)
    expect(r.status_code == 200, "全量导出 JSON 返回 200", r.text[:200])
    expect("application/json" in r.headers.get("content-type", ""), "导出 JSON 的 Content-Type 正确")
    cd = r.headers.get("content-disposition", "")
    expect("attachment" in cd and "filename*=UTF-8''" in cd, "带附件头且文件名做了编码", cd)
    pack = r.json()
    expect(pack.get("format") == "simple-gateway.safety.libraries", "导出文件带格式标识")
    expect(isinstance(pack.get("libraries"), list) and len(pack["libraries"]) >= 17,
           "全量导出包含全部内置词库", f"n={len(pack.get('libraries') or [])}")
    sample = pack["libraries"][0]
    for field in ("name", "terms", "risk_level", "hit_action", "is_enabled"):
        expect(field in sample, f"导出条目包含 {field}")

    # 造一个专用词库，后续用例都用它，避免受内置数据影响
    created = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "导入导出测试库",
        "description": "回归测试专用",
        "terms": "# 注释行\n苹果\n香蕉\nre:坏词\\d+",
        "risk_level": "high",
        "hit_action": "block",
    })
    expect(created.status_code == 201, "准备测试词库", created.text[:200])
    lib_id = created.json()["id"]
    expect(created.json()["term_count"] == 3, "测试词库词条数为 3", str(created.json()["term_count"]))

    r = client.get(f"/api/admin/safety/libraries/export?ids={lib_id}&fmt=json", headers=H)
    one = r.json()
    expect(len(one["libraries"]) == 1, "按 id 导出单个词库")
    expect(one["libraries"][0]["name"] == "导入导出测试库", "单库导出内容正确")

    r = client.get(f"/api/admin/safety/libraries/export?ids={lib_id}&fmt=txt", headers=H)
    expect(r.status_code == 200, "单库导出 TXT 返回 200", r.text[:200])
    body = r.content.decode("utf-8")
    expect("text/plain" in r.headers.get("content-type", ""), "TXT 的 Content-Type 正确")
    expect(body.startswith("\ufeff# 词库：导入导出测试库"), "TXT 带 BOM 与词库名注释头", body[:40])
    expect("苹果" in body and "re:坏词\\d+" in body, "TXT 保留全部词条与正则")

    r = client.get("/api/admin/safety/libraries/export?fmt=txt", headers=H)
    expect(r.status_code == 400, "多库导出 TXT 被拒绝", str(r.status_code))
    r = client.get(f"/api/admin/safety/libraries/export?ids={lib_id}&fmt=csv", headers=H)
    expect(r.status_code == 400, "非法 fmt 被拒绝", str(r.status_code))

    print("\n== 导入模板 ==")
    r = client.get("/api/admin/safety/libraries/template?fmt=json", headers=H)
    expect(r.status_code == 200, "JSON 模板可下载", r.text[:200])
    tpl = r.json()
    expect(tpl.get("format") == "simple-gateway.safety.libraries", "模板带格式标识")
    expect(len(tpl["libraries"]) == 1 and tpl["libraries"][0]["name"], "模板含一个示例词库")
    expect("#" in tpl["libraries"][0]["terms"], "模板词条含注释说明")

    r = client.get("/api/admin/safety/libraries/template?fmt=txt", headers=H)
    expect(r.status_code == 200, "TXT 模板可下载", r.text[:200])
    tpl_txt = r.content.decode("utf-8")
    expect("re:" in tpl_txt and "# 词库名" in tpl_txt, "TXT 模板写明格式与正则示例")
    expect(r.headers.get("content-disposition", "").endswith(".txt"), "TXT 模板文件名为 .txt")

    r = client.get("/api/admin/safety/libraries/template?fmt=doc", headers=H)
    expect(r.status_code == 400, "模板 fmt 非法被拒绝", str(r.status_code))

    print("\n== 导入：新建 / 合并 / 覆盖 / 副本 ==")
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{
            "name": "全新导入库",
            "description": "来自导入",
            "terms": "词一\n词二",
            "risk_level": "critical",
            "hit_action": "review",
        }],
    })
    expect(r.status_code == 200, "导入新建返回 200", r.text[:300])
    res = r.json()
    expect(res["created"] == 1 and res["terms_added"] == 2, "新建计数正确", json.dumps(res, ensure_ascii=False))
    db = SessionLocal()
    new_lib = db.query(SafetyLibrary).filter(SafetyLibrary.name == "全新导入库").first()
    expect(new_lib is not None, "新建的词库已落库")
    expect(new_lib.risk_level == "critical" and new_lib.hit_action == "review", "导入保留风险等级与动作")
    expect(db.query(SafetyLibrary).filter(SafetyLibrary.name == "全新导入库").count() == 1, "没有重复建库")
    db.close()

    # merge：同名追加去重
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{"name": "导入导出测试库", "terms": "苹果\n橙子\n橙子"}],
    })
    merged = r.json()
    expect(merged["merged"] == 1 and merged["items"][0]["added"] == 1,
           "合并只追加新词条并去重", json.dumps(merged, ensure_ascii=False))
    db = SessionLocal()
    lib = db.query(SafetyLibrary).filter(SafetyLibrary.id == lib_id).first()
    expect(lib is not None and lib.term_count == 4, "合并后词条数 +1", str(lib.term_count if lib else None))
    expect(lib.risk_level == "high", "合并不覆盖原有风险等级")
    db.close()

    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{"name": "导入导出测试库", "terms": "苹果\n橙子"}],
    })
    again = r.json()
    expect(again["skipped"] == 1 and again["items"][0]["added"] == 0,
           "重复导入同样内容全部跳过", json.dumps(again, ensure_ascii=False))

    # overwrite：整体替换
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "overwrite",
        "libraries": [{
            "name": "导入导出测试库",
            "description": "被覆盖",
            "terms": "只有一条",
            "risk_level": "low",
            "hit_action": "sanitize",
            "is_enabled": False,
        }],
    })
    over = r.json()
    expect(over["overwritten"] == 1, "覆盖计数正确", json.dumps(over, ensure_ascii=False))
    db = SessionLocal()
    lib = db.query(SafetyLibrary).filter(SafetyLibrary.id == lib_id).first()
    expect(lib.term_count == 1 and lib.terms == "只有一条", "覆盖后词条整体替换", lib.terms)
    expect(lib.risk_level == "low" and lib.hit_action == "sanitize", "覆盖后元数据更新")
    expect(lib.is_enabled is False, "覆盖后启用状态更新")
    expect(lib.description == "被覆盖", "覆盖后说明更新")
    db.close()

    # copy：另存副本，原库不动
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "copy",
        "libraries": [{"name": "导入导出测试库", "terms": "副本词"}],
    })
    cp = r.json()
    expect(cp["created"] == 1, "副本模式新建一条", json.dumps(cp, ensure_ascii=False))
    expect(cp["items"][0]["name"] == "导入导出测试库（导入）", "副本名称带（导入）后缀", cp["items"][0]["name"])
    db = SessionLocal()
    orig = db.query(SafetyLibrary).filter(SafetyLibrary.id == lib_id).first()
    expect(orig.terms == "只有一条", "副本模式不影响原库")
    copy_lib = db.query(SafetyLibrary).filter(SafetyLibrary.name == "导入导出测试库（导入）").first()
    expect(copy_lib is not None and copy_lib.terms == "副本词", "副本内容正确")
    db.close()

    print("\n== 边界与校验 ==")
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "libraries": [{"name": "坏正则库", "terms": "re:([未闭合"}],
    })
    expect(r.status_code == 400, "非法正则被拒绝", r.text[:200])
    db = SessionLocal()
    expect(db.query(SafetyLibrary).filter(SafetyLibrary.name == "坏正则库").first() is None,
           "校验失败时不落库（整批回滚）")
    db.close()

    r = client.post("/api/admin/safety/libraries/import", headers=H, json={"libraries": []})
    expect(r.status_code == 400, "空导入被拒绝", str(r.status_code))
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "append", "libraries": [{"name": "x", "terms": "y"}],
    })
    expect(r.status_code == 400, "非法 mode 被拒绝", str(r.status_code))

    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{"name": "BOM 库", "terms": "\ufeff带BOM的首行\n第二行"}],
    })
    expect(r.status_code == 200, "带 BOM 的内容可导入", r.text[:200])
    db = SessionLocal()
    bom_lib = db.query(SafetyLibrary).filter(SafetyLibrary.name == "BOM 库").first()
    expect(bom_lib.term_count == 2, "BOM 没有被算成词条内容", str(bom_lib.term_count))
    expect(bom_lib.terms.splitlines()[0] == "带BOM的首行", "BOM 已剥离", repr(bom_lib.terms.splitlines()[0]))
    db.close()

    # builtin_key 匹配：名称不同但内置标识相同 → 合并到同一条，不新建
    db = SessionLocal()
    builtin_lib = db.query(SafetyLibrary).filter(SafetyLibrary.builtin_key == "builtin:deep_drug").first()
    builtin_name = builtin_lib.name if builtin_lib else ""
    before = builtin_lib.term_count if builtin_lib else 0
    db.close()
    expect(builtin_lib is not None, "存在内置词库 builtin:deep_drug")
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{
            "name": "故意改个名字",
            "builtin_key": "builtin:deep_drug",
            "terms": "测试用新增词条",
        }],
    })
    bk = r.json()
    expect(r.status_code == 200 and bk.get("created") == 0, "builtin_key 命中时不新建",
           json.dumps(bk, ensure_ascii=False))
    db = SessionLocal()
    expect(db.query(SafetyLibrary).filter(SafetyLibrary.name == "故意改个名字").first() is None,
           "没有新建成改名词库")
    again_lib = db.query(SafetyLibrary).filter(SafetyLibrary.builtin_key == "builtin:deep_drug").first()
    expect(again_lib.name == builtin_name and again_lib.term_count == before + 1,
           "按 builtin_key 合并到原内置词库", f"{again_lib.term_count} vs {before}")
    db.close()

    print("\n== 导出再导入守恒 ==")
    r = client.get(f"/api/admin/safety/libraries/export?ids={lib_id}&fmt=json", headers=H)
    pack = r.json()
    pack["libraries"][0]["name"] = "往返校验库"
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge", "libraries": pack["libraries"],
    })
    expect(r.status_code == 200, "导出的包可原样导回", r.text[:200])
    db = SessionLocal()
    rt = db.query(SafetyLibrary).filter(SafetyLibrary.name == "往返校验库").first()
    expect(rt is not None and rt.term_count == 1, "往返后词条数一致", str(rt.term_count if rt else None))
    expect(rt.risk_level == "low" and rt.hit_action == "sanitize", "往返后元数据一致")
    db.close()

    print("\n== 增量语义与报告准确性（merge 只追加 / overwrite 以文件为准）==")
    base_terms = "语义甲\n语义乙\n语义丙"
    adj_terms = "语义甲\n语义乙改\n语义丁"
    r = client.post("/api/admin/safety/libraries", headers=H, json={
        "name": "语义验证库", "terms": base_terms,
        "risk_level": "medium", "hit_action": "block",
    })
    expect(r.status_code == 201, "建立语义验证原库", r.text[:160])
    sid = r.json()["id"]

    # merge：只做并集追加，不删不改
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{"name": "语义验证库", "terms": adj_terms,
                       "risk_level": "high", "hit_action": "sanitize"}],
    })
    res = r.json()
    expect(res["merged"] == 1 and res["terms_added"] == 2 and res["terms_removed"] == 0,
           "merge 只报新增 2 条、不移除", str({k: res[k] for k in ("merged", "terms_added", "terms_removed")}))
    db = SessionLocal()
    m = db.query(SafetyLibrary).filter(SafetyLibrary.id == sid).first()
    got = [x for x in (m.terms or "").splitlines() if x.strip()]
    expect("语义乙" in got and "语义乙改" in got,
           "merge 下被改写的词以「新旧并存」留下（不替换）", str(got))
    expect("语义丙" in got, "merge 不移除文件中没有的词", str(got))
    expect(m.risk_level == "medium" and m.hit_action == "block",
           "merge 不改动风险等级与命中动作", f"{m.risk_level}/{m.hit_action}")
    db.close()

    # merge 且无新词：skipped，并提示改用覆盖模式
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "merge",
        "libraries": [{"name": "语义验证库", "terms": "语义甲",
                       "risk_level": "critical", "hit_action": "block"}],
    })
    res = r.json()
    expect(res["skipped"] == 1, "无新词时 merge 计入 skipped")
    expect("覆盖模式" in res["items"][0]["reason"],
           "skipped 原因提示改用覆盖模式", res["items"][0]["reason"])
    db = SessionLocal()
    m2 = db.query(SafetyLibrary).filter(SafetyLibrary.id == sid).first()
    expect(m2.risk_level == "medium", "skipped 时元数据未被悄悄改写", m2.risk_level)
    db.close()

    # 重置为原始三词，再验证 overwrite 的真实差异口径
    client.put(f"/api/admin/safety/libraries/{sid}", headers=H, json={
        "terms": base_terms, "risk_level": "medium", "hit_action": "block",
    })
    r = client.post("/api/admin/safety/libraries/import", headers=H, json={
        "mode": "overwrite",
        "libraries": [{"name": "语义验证库", "terms": adj_terms,
                       "risk_level": "high", "hit_action": "sanitize"}],
    })
    res = r.json()
    expect(res["overwritten"] == 1, "overwrite 命中同名库")
    db = SessionLocal()
    o = db.query(SafetyLibrary).filter(SafetyLibrary.id == sid).first()
    got2 = [x for x in (o.terms or "").splitlines() if x.strip()]
    expect(got2 == ["语义甲", "语义乙改", "语义丁"],
           "overwrite 后词条完全等于文件（旧的乙、丙已消失）", str(got2))
    expect(o.risk_level == "high" and o.hit_action == "sanitize",
           "overwrite 更新风险等级与命中动作", f"{o.risk_level}/{o.hit_action}")
    db.close()
    it = res["items"][0]
    # old={甲,乙,丙} new={甲,乙改,丁} → 新增 {乙改,丁}=2、移除 {乙,丙}=2
    expect(it["added"] == 2 and it["removed"] == 2,
           "overwrite 报告真实新增 2 / 移除 2（不把替换后总数当新增）", str(it))
    expect(res["terms_added"] == 2 and res["terms_removed"] == 2,
           "汇总口径与明细一致", str({k: res[k] for k in ("terms_added", "terms_removed")}))
    expect("移除 2 条" in it["reason"], "reason 明写移除条数", it["reason"])

    client.delete(f"/api/admin/safety/libraries/{sid}", headers=H)
    expect(True, "清理语义验证库")

    print(f"\n全部通过：{len(PASSED)} 项断言")
