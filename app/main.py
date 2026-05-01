from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

# Windows：默认 ProactorEventLoop 在客户端断开连接时，可能在 _call_connection_lost
# 里对 socket 执行 shutdown 并抛出 ConnectionResetError（WinError 10054），污染日志。
# 在创建事件循环之前改用 Selector 策略，与 uvicorn 在 Windows 上的常见建议一致。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, SessionLocal, engine
from app.routes.web import router as web_router
from app.services.user_service import ensure_admin_user


@asynccontextmanager
async def _lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_admin_user(db)
    finally:
        db.close()
    yield


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_app() -> FastAPI:
    app = FastAPI(title="PaperFigure Admin", version="1.0.0", lifespan=_lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("PB_SESSION_SECRET", "paperfigure-dev-secret"),
        session_cookie=os.getenv("PB_SESSION_COOKIE_NAME", "paperbanana_session"),
        max_age=60 * 60 * 24 * 7,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.mount("/assets", StaticFiles(directory="assets"), name="assets")
    app.include_router(web_router)

    return app


app = create_app()


def run() -> None:
    """
    Unified launch entry for cloud deployment.
    Defaults to external access (0.0.0.0:8000).
    """
    import uvicorn

    host = os.getenv("PB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.getenv("PB_PORT", "80"))
    except Exception:
        port = 80
    reload_enabled = _env_bool("PB_RELOAD", False)
    # 0.0.0.0 仅表示监听所有网卡，浏览器请用 127.0.0.1 或 localhost
    print(
        f"\n  本机访问: http://127.0.0.1:{port}/login\n"
        f"  不要用浏览器打开 http://0.0.0.0/（会报错 ERR_ADDRESS_INVALID）\n",
        flush=True,
    )
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled)


if __name__ == "__main__":
    run()
