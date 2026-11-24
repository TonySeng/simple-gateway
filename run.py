"""本地启动入口：python run.py"""
from app.core.config import settings

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info",
    )
