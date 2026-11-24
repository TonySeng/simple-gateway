"""Simple Gateway —— 统一 AI 能力中转网关。

一个进程、一套后台、一套 API Key，同时对外提供：

* OCR 识别      ``/v1/ocr/*``
* 大模型对话    ``/v1/chat/completions``（OpenAI 兼容）与 ``/v1/messages``（Anthropic 兼容）

新增中转服务只需在 ``app/services/`` 下加一个包并注册 **ServiceSpec**（见
``app/core/registry.py``），路由、后台表单、鉴权配额、日志统计会自动适配。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core import registry
from app.core.admin_store import ensure_admin_seeded
from app.core.config import ensure_data_dir, resolve_secrets, settings
from app.core.db import SessionLocal, backfill_channels, ensure_extra_columns, init_db
from app.core.safety_bootstrap import bootstrap_safety
from app.core.errors import validation_exception_handler
from app.core.logretention import cleanup_loop

# 先注册所有服务，再装配路由
registry.load_services()

from app.admin import auth as admin_auth  # noqa: E402
from app.admin import keys as admin_keys  # noqa: E402
from app.admin import live as admin_live  # noqa: E402
from app.admin import logs as admin_logs  # noqa: E402
from app.admin import providers as admin_providers  # noqa: E402
from app.admin import safety as admin_safety  # noqa: E402
from app.admin import services as admin_services  # noqa: E402
from app.admin import stats as admin_stats  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("gateway")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    resolve_secrets()
    ensure_data_dir()
    init_db()
    ensure_extra_columns()
    backfill_channels()
    with SessionLocal() as _db:
        ensure_admin_seeded(_db)
        # 内置词库/规则：只在版本变化或显式调用时补齐，绝不覆盖用户修改
        bootstrap_safety(_db)
    # 日志详情定期清理：启动即清一次，之后每小时一次（只清空详情，不删统计行）
    stop_cleanup = asyncio.Event()
    cleanup_task = asyncio.create_task(cleanup_loop(stop_cleanup))
    logger.info(
        "%s %s 已启动 | 服务：%s | 数据库：%s",
        settings.app_name,
        settings.version,
        ", ".join(s.key for s in registry.all_services()) or "-",
        settings.database_url,
    )
    yield
    stop_cleanup.set()
    try:
        await asyncio.wait_for(cleanup_task, timeout=5)
    except asyncio.TimeoutError:
        cleanup_task.cancel()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("日志清理任务退出异常（已忽略）")
    for spec in registry.all_services():
        if spec.on_shutdown:
            try:
                await spec.on_shutdown()
            except Exception:
                logger.exception("服务 %s 清理失败", spec.key)


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="统一 AI 能力中转网关：OCR + LLM，可插拔扩展更多服务。",
    lifespan=lifespan,
)

# 统一参数校验错误：即便 body 是二进制（如 multipart 打到 JSON 端点）也返回 422
# 而不是 500，并附加「该怎么改」的 hint。
app.add_exception_handler(RequestValidationError, validation_exception_handler)

_origins = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def collapse_duplicate_v1(request: Request, call_next):
    """容忍 ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` 已经带 ``/v1`` 的情况。

    官方 Anthropic SDK 会在 base_url 后自行拼 ``/v1/messages``，若用户按 OpenAI
    习惯把 base_url 写成 ``http://host:8020/v1``，实际请求就变成
    ``/v1/v1/messages`` 并 404 —— 而 Claude Code 会把这类 404 显示成
    「模型可能不存在」，极具误导性。这里直接把重复的 ``/v1`` 折叠掉。
    """
    path = request.scope.get("path", "")
    while path.startswith("/v1/v1/"):
        path = "/v1/" + path[len("/v1/v1/"):]
    if path != request.scope.get("path"):
        request.scope["path"] = path
        request.scope["raw_path"] = path.encode("utf-8")
    return await call_next(request)


# ---------- 对外服务路由 ----------
for _spec in registry.all_services():
    if _spec.router is not None:
        app.include_router(_spec.router)

# ---------- 管理接口 ----------
app.include_router(admin_auth.router)
app.include_router(admin_services.router)
app.include_router(admin_providers.router)
app.include_router(admin_keys.router)
app.include_router(admin_logs.router)
app.include_router(admin_live.router)
app.include_router(admin_stats.router)
app.include_router(admin_safety.router)

# ---------- 服务专属管理接口（如 LLM 的模型管理）----------
for _spec in registry.all_services():
    for _router in _spec.admin_routers:
        app.include_router(_router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "services": [s.key for s in registry.all_services()],
    }


@app.get("/api/health", tags=["meta"], include_in_schema=False)
def health_alias():
    return health()


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin/")


# ---------- 管理后台（原生 JS SPA，可离线运行）----------
if os.path.isdir(STATIC_DIR):
    app.mount("/admin", StaticFiles(directory=STATIC_DIR, html=True), name="admin")

    @app.get("/admin", include_in_schema=False)
    def admin_no_slash():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
