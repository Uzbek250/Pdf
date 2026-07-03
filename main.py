"""
FastAPI ilovasining kirish nuqtasi.

Ishga tushirish:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Celery worker (alohida terminalda):
    celery -A app.workers.celery_tasks.celery_app worker --loglevel=info
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router as api_router
from .config.settings import get_settings

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "PDF va DOCX fayllarni original formatini saqlab tarjima qiluvchi API. "
        "Provider: Google Gemini (gemini-2.5-flash, free tier)."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_PREFIX)


@app.on_event("startup")
async def on_startup() -> None:
    settings.ensure_work_dir()
    logger.info("%s ishga tushdi. WORK_DIR=%s", settings.APP_NAME, settings.WORK_DIR)


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Oddiy health-check endpoint (load balancer / monitoring uchun)."""
    return {"status": "ok"}
