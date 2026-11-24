#!/usr/bin/env python
"""Simple Gateway —— 从 SQLite 迁移数据到 PostgreSQL。

用法（在能同时访问源库与目标库的容器内运行）：

    # 默认：源 /app/data/gateway.db，目标 postgres 服务名
    python tools/migrate_to_pg.py

    # 自定义
    python tools/migrate_to_pg.py \
        --sqlite /path/to/gateway.db \
        --pg "postgresql+psycopg://sg:sgpass@postgres:5432/sgateway"

    # 目标已有数据时清空重导
    python tools/migrate_to_pg.py --drop

设计要点
--------
* 复用应用自身的 ORM metadata（app.core.models）建表，保证 schema 与代码一致。
* 所有主键为 String(32) 的 new_id()，因此直接整行拷贝、保留原 id，无需处理自增序列。
* 按依赖顺序导入：providers -> models(FK->providers) -> api_keys -> request_logs
  -> safety_libraries -> safety_policies -> safety_reviews -> app_settings -> meta。
* 目标非空且未指定 --drop 时直接报错退出，避免半覆盖。
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

# 确保全部表注册到 metadata
from app.core.db import Base  # noqa: F401
from app.core import models  # noqa: F401

TABLE_ORDER = [
    models.Provider,
    models.Model,
    models.ApiKey,
    models.RequestLog,
    models.SafetyLibrary,
    models.SafetyPolicy,
    models.SafetyReview,
    models.AppSetting,
    models.Meta,
]


def _sqlite_url(path: str) -> str:
    # 绝对路径 (/app/data/gateway.db) -> sqlite:////app/data/gateway.db
    if path.startswith("sqlite"):
        return path
    return "sqlite:///" + os.path.abspath(path)


def main() -> None:
    p = argparse.ArgumentParser(description="SQLite -> PostgreSQL 迁移")
    p.add_argument(
        "--sqlite",
        default=os.environ.get("SG_SQLITE_PATH", "/app/data/gateway.db"),
        help="源 SQLite 文件路径（默认 /app/data/gateway.db）",
    )
    p.add_argument(
        "--pg",
        default=os.environ.get(
            "SG_PG_URL", "postgresql+psycopg://sg:sgpass@postgres:5432/sgateway"
        ),
        help="目标 PostgreSQL URL",
    )
    p.add_argument("--drop", action="store_true", help="导入前先清空目标表")
    args = p.parse_args()

    src = create_engine(_sqlite_url(args.sqlite), connect_args={"check_same_thread": False})
    dst = create_engine(args.pg, future=True)

    print(f"[源]   {args.sqlite}")
    print(f"[目标] {args.pg}")

    # 1) 建表（幂等）
    Base.metadata.create_all(bind=dst)
    print("[建表] 目标表已就绪（若不存在则创建）")

    # 2) 预检：目标非空保护
    if not args.drop:
        with dst.connect() as dc:
            for M in TABLE_ORDER:
                t = M.__table__
                n = dc.execute(select(func.count()).select_from(t)).scalar() or 0
                if n > 0:
                    print(
                        f"[错误] 目标表 {t.name} 已有 {n} 行；"
                        f"请先清空或加 --drop 参数，避免半覆盖。"
                    )
                    sys.exit(1)

    # 3) 逐表拷贝（同一套 metadata，schema 一致）
    for M in TABLE_ORDER:
        t = M.__table__
        with src.connect() as sc, Session(dst) as dc:
            rows = list(sc.execute(select(t)).mappings())
            if not rows:
                print(f"  - {t.name}: 0 行（跳过）")
                continue
            if args.drop:
                dc.execute(t.delete())
            dc.execute(t.insert(), [dict(r) for r in rows])
            dc.commit()
            print(f"  + {t.name}: 导入 {len(rows)} 行")

    print("[完成] 数据已迁移至 PostgreSQL。")


if __name__ == "__main__":
    main()
