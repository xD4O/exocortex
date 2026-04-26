"""FastAPI app factory for the operator web UI.

Mounts the REST router + the static-file directory, spins up the audit-log
tailer during `lifespan`, and tears it down on shutdown.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from exocortex.config import Settings
from exocortex.memory.durable import DurableMemoryStore
from exocortex.observability.audit import AuditLog
from exocortex.operator.web.events import EventBroadcaster, run_tailer_task, stop_tailer
from exocortex.operator.web.routes import build_router

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    # Touch the audit log so the tailer has a path to poll even if nothing has
    # written one yet.
    if not settings.audit_log_path.exists():
        settings.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        settings.audit_log_path.touch()

    audit = AuditLog(settings.audit_log_path)
    store = DurableMemoryStore(settings.memory_db_path)
    broadcaster = EventBroadcaster()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = await run_tailer_task(settings.audit_log_path, broadcaster)
        try:
            yield
        finally:
            await stop_tailer(task)
            await store.close()

    app = FastAPI(
        title="Exocortex Operator UI",
        description="Read-only lens over the audit log + memory store.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(
        build_router(
            audit=audit, store=store, broadcaster=broadcaster, settings=settings
        )
    )

    # Static files (dashboard, constellation, shared assets)
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/memory", include_in_schema=False)
    async def memory_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "memory.html"))

    @app.get("/agents", include_in_schema=False)
    async def agents_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "agents.html"))

    @app.get("/chat", include_in_schema=False)
    async def chat_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "chat.html"))

    @app.get("/profile", include_in_schema=False)
    async def profile_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "profile.html"))

    @app.get("/debug", include_in_schema=False)
    async def debug_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "debug.html"))

    @app.get("/conversations", include_in_schema=False)
    async def conversations_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "conversations.html"))

    @app.get("/tasks", include_in_schema=False)
    async def tasks_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "tasks.html"))

    return app
