"""把旧的 OCR Relay / LLM Gateway 数据迁移进统一网关。

旧项目各用一套 Fernet 密钥加密厂商凭证，因此迁移时需要提供旧密钥：
* OCR：``CRYPTO_KEY``（旧 .env）
* LLM：``GW_FERNET_KEY``（旧容器 ``/app/data/.secrets.json`` 里的 ``fernet_key``，
  未显式设置时可用 ``--llm-secrets`` 指向该文件）

用法示例::

    # 从本地 PostgreSQL（旧的 ocr-relay）与旧 LLM SQLite 迁移
    python -m tools.migrate_legacy \
        --ocr-url "postgresql+psycopg://ocr:ocr123@127.0.0.1:5432/ocr_relay" \
        --ocr-key "$OLD_CRYPTO_KEY" \
        --llm-sqlite "../LLM Tools/llm-gateway/data/gateway.db" \
        --llm-secrets "../LLM Tools/llm-gateway/data/.secrets.json"

默认追加：已存在的同名厂商、同 hash 的 Key 会自动跳过，可安全重复执行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402
from sqlalchemy import create_engine, insert, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core import crypto  # noqa: E402
from app.core.config import ensure_data_dir, resolve_secrets  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.core.models import ApiKey, Model, Provider, RequestLog  # noqa: E402
from app.core.security import new_id  # noqa: E402


# ---------------- 工具 ----------------
def _old_fernet(key: str | None) -> Fernet | None:
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        print(f"  ! 旧密钥格式非法，跳过凭证迁移：{key[:6]}…")
        return None


def _decrypt_with(f: Fernet | None, token: str | None) -> str:
    if not token:
        return ""
    if f is None:
        return ""
    try:
        return f.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def _json(text, default):
    if text is None:
        return default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return default


def _naive(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _mirror_engine(url: str):
    """打开旧库（只读用途）。"""
    return create_engine(url, pool_pre_ping=True, future=True)


# ---------------- OCR ----------------
def migrate_ocr(src_url: str, old_key: str | None, with_logs: bool) -> dict:
    stats = {"providers": 0, "keys": 0, "logs": 0, "skipped": 0}
    if not src_url:
        return stats
    f = _old_fernet(old_key)
    engine = _mirror_engine(src_url)
    db: Session = SessionLocal()
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(
                _table(conn, "provider_configs")
            )).mappings().all()

            mapping: dict[str, str] = {}
            for r in rows:
                name = r["name"]
                exist = db.query(Provider).filter(
                    Provider.service == "ocr", Provider.name == name
                ).first()
                if exist:
                    mapping[r["id"]] = exist.id
                    stats["skipped"] += 1
                    continue
                creds_text = _decrypt_with(f, r.get("credentials_enc"))
                creds = _json(creds_text, {}) if creds_text else {}
                p = Provider(
                    service="ocr",
                    type=r["type"],
                    name=name,
                    is_enabled=bool(r.get("is_enabled", True)),
                    is_default=bool(r.get("is_default", False)),
                    weight=int(r.get("weight") or 100),
                    priority=int(r.get("priority") or 100),
                    settings=_json(r.get("settings"), {}),
                    comment=r.get("comment"),
                )
                if creds:
                    p.credentials_enc = crypto.encrypt_json(creds)
                db.add(p)
                db.flush()
                mapping[r["id"]] = p.id
                stats["providers"] += 1

            for r in conn.execute(select(_table(conn, "api_keys"))).mappings().all():
                if db.query(ApiKey).filter(ApiKey.key_hash == r["key_hash"]).first():
                    stats["skipped"] += 1
                    continue
                allowed = _json(r.get("allowed_providers"), ["*"])
                db.add(ApiKey(
                    name=r.get("name") or "ocr-key",
                    key_hash=r["key_hash"],
                    key_prefix="",
                    status=r.get("status") or "active",
                    allowed_services=["ocr"],
                    allowed_providers={"ocr": allowed},
                    quota_requests=int(r.get("quota") or 0),
                    used_requests=int(r.get("used") or 0),
                    rate_limit=int(r.get("rate_limit") or 0),
                    expires_at=_naive(r.get("expires_at")),
                    comment=r.get("comment"),
                    last_used_at=_naive(r.get("last_used_at")),
                    created_at=_naive(r.get("created_at")) or datetime.utcnow(),
                ))
                stats["keys"] += 1

            if with_logs and "api_requests" in _tables(conn):
                for r in conn.execute(select(_table(conn, "api_requests"))).mappings().all():
                    db.add(RequestLog(
                        service="ocr",
                        endpoint=r.get("endpoint") or "recognize",
                        provider_id=mapping.get(r.get("provider_config_id")),
                        provider_type=r.get("provider") or "",
                        provider_name="",
                        model="",
                        status=r.get("status") or "success",
                        http_status=r.get("http_status"),
                        latency_ms=int(r.get("latency_ms") or 0),
                        char_count=int(r.get("char_count") or 0),
                        error_message=r.get("error") or "",
                        created_at=_naive(r.get("created_at")) or datetime.utcnow(),
                    ))
                    stats["logs"] += 1
        db.commit()
    finally:
        db.close()
        engine.dispose()
    return stats


# ---------------- LLM ----------------
def migrate_llm(db_path: str, secrets_path: str | None, old_key: str | None, with_logs: bool) -> dict:
    stats = {"providers": 0, "models": 0, "keys": 0, "logs": 0, "skipped": 0}
    if not db_path:
        return stats

    if not old_key and secrets_path and os.path.exists(secrets_path):
        try:
            old_key = json.load(open(secrets_path, encoding="utf-8")).get("fernet_key")
            print(f"  从 {secrets_path} 读取旧 Fernet 密钥")
        except Exception as e:
            print(f"  ! 读取旧密钥文件失败：{e}")

    f = _old_fernet(old_key)
    url = db_path if "://" in db_path else "sqlite:///" + db_path.replace("\\", "/")
    engine = _mirror_engine(url)
    db: Session = SessionLocal()
    try:
        with engine.connect() as conn:
            tables = _tables(conn)

            pmap: dict[int, str] = {}
            for r in conn.execute(select(_table(conn, "providers"))).mappings().all():
                exist = db.query(Provider).filter(
                    Provider.service == "llm", Provider.name == r["name"]
                ).first()
                if exist:
                    pmap[r["id"]] = exist.id
                    stats["skipped"] += 1
                    continue
                api_key = _decrypt_with(f, r.get("api_key"))
                p = Provider(
                    service="llm",
                    type=r.get("type") or "openai",
                    name=r["name"],
                    base_url=r.get("base_url") or "",
                    is_enabled=bool(r.get("is_active", True)),
                    is_default=False,
                    weight=100,
                    priority=int(r.get("priority") or 100),
                    settings=_json(r.get("extra_config"), {}),
                )
                if api_key:
                    p.credentials_enc = crypto.encrypt_json({"api_key": api_key})
                db.add(p)
                db.flush()
                pmap[r["id"]] = p.id
                stats["providers"] += 1

            if "models" in tables:
                for r in conn.execute(select(_table(conn, "models"))).mappings().all():
                    pid = pmap.get(r.get("provider_id"))
                    if not pid:
                        continue
                    if db.query(Model).filter(Model.provider_id == pid, Model.name == r["name"]).first():
                        stats["skipped"] += 1
                        continue
                    db.add(Model(
                        provider_id=pid,
                        name=r["name"],
                        provider_model=r.get("provider_model") or r["name"],
                        input_price=float(r.get("input_price") or 0),
                        output_price=float(r.get("output_price") or 0),
                        currency=r.get("currency") or "CNY",
                        context_window=int(r.get("context_window") or 0),
                        is_active=bool(r.get("is_active", True)),
                    ))
                    stats["models"] += 1

            for r in conn.execute(select(_table(conn, "api_keys"))).mappings().all():
                if db.query(ApiKey).filter(ApiKey.key_hash == r["key_hash"]).first():
                    stats["skipped"] += 1
                    continue
                qtype = r.get("quota_type") or "unlimited"
                limit = float(r.get("quota_limit") or 0)
                db.add(ApiKey(
                    name=r.get("name") or "llm-key",
                    key_hash=r["key_hash"],
                    key_prefix="",
                    status=r.get("status") or "active",
                    allowed_services=["llm"],
                    allowed_providers={},
                    quota_tokens=int(limit) if qtype == "tokens" else 0,
                    quota_cost=limit if qtype == "price" else 0.0,
                    used_requests=int(r.get("request_count") or 0),
                    used_tokens=int(r.get("used_tokens") or 0),
                    used_cost=float(r.get("used_price") or 0),
                    expires_at=_naive(r.get("expires_at")),
                    last_used_at=_naive(r.get("last_used_at")),
                    created_at=_naive(r.get("created_at")) or datetime.utcnow(),
                ))
                stats["keys"] += 1

            if with_logs and "request_logs" in tables:
                for r in conn.execute(select(_table(conn, "request_logs"))).mappings().all():
                    db.add(RequestLog(
                        service="llm",
                        endpoint="/v1/messages" if (r.get("mode") or "") == "anthropic" else "/v1/chat/completions",
                        provider_id=pmap.get(r.get("provider_id")),
                        provider_name=r.get("provider_name") or "",
                        provider_type="",
                        model=r.get("model") or "",
                        mode=r.get("mode") or "chat",
                        status=r.get("status") or "success",
                        latency_ms=int(r.get("latency_ms") or 0),
                        tokens_in=int(r.get("tokens_in") or 0),
                        tokens_out=int(r.get("tokens_out") or 0),
                        cost=float(r.get("price") or 0),
                        currency=r.get("currency") or "CNY",
                        error_message=r.get("error_message") or "",
                        ip=r.get("ip") or "",
                        created_at=_naive(r.get("created_at")) or datetime.utcnow(),
                    ))
                    stats["logs"] += 1
        db.commit()
    finally:
        db.close()
        engine.dispose()
    return stats


# ---------------- 表名/反射辅助 ----------------
def _tables(conn) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(conn).get_table_names())


def _table(conn, name: str):
    from sqlalchemy import MetaData, Table

    md = MetaData()
    return Table(name, md, autoload_with=conn)


def main() -> int:
    ap = argparse.ArgumentParser(description="旧 OCR Relay / LLM Gateway 数据迁移")
    ap.add_argument("--ocr-url", help="旧 OCR 库连接串（postgresql+psycopg://... 或 sqlite:///...）")
    ap.add_argument("--ocr-key", help="旧 OCR 的 CRYPTO_KEY（Fernet）")
    ap.add_argument("--llm-sqlite", help="旧 LLM 的 gateway.db 路径或连接串")
    ap.add_argument("--llm-key", help="旧 LLM 的 GW_FERNET_KEY")
    ap.add_argument("--llm-secrets", help="旧 LLM 的 .secrets.json 路径（自动读取 fernet_key）")
    ap.add_argument("--with-logs", action="store_true", help="同时迁移历史调用日志")
    args = ap.parse_args()

    if not (args.ocr_url or args.llm_sqlite):
        ap.error("至少提供 --ocr-url 或 --llm-sqlite 之一")

    resolve_secrets()
    ensure_data_dir()
    init_db()

    print("== 迁移 OCR 数据 ==")
    print("  ", migrate_ocr(args.ocr_url, args.ocr_key, args.with_logs))

    print("== 迁移 LLM 数据 ==")
    print("  ", migrate_llm(args.llm_sqlite, args.llm_secrets, args.llm_key, args.with_logs))

    db = SessionLocal()
    try:
        print("== 迁移后统计 ==")
        for svc in ("ocr", "llm"):
            print(f"  {svc}: 厂商={db.query(Provider).filter(Provider.service == svc).count()}")
        print(f"  合计：厂商={db.query(Provider).count()} 模型={db.query(Model).count()} Key={db.query(ApiKey).count()}")
    finally:
        db.close()
    print("\n旧 Key 的 hash 原样保留，迁移后无需更换客户端密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
