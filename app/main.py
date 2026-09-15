"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import WORKSPACE_DIR, get_settings
from app.db import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.web.routes import router as web_router
from app.web.api import router as api_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    log.info("Opportunity Engine %s | LLM: %s", __version__, settings.claude_model if settings.llm_enabled else "MOCK mode (no ANTHROPIC_API_KEY)")
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="Opportunity Engine", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
app.include_router(api_router)
app.include_router(web_router)
