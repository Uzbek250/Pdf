"""
FastAPI ilovasining kirish nuqtasi.

Ishga tushirish:
    uvicorn main:app --host 0.0.0.0 --port $PORT

MUHIM (Railway single-service deploy uchun):
    Ayrim hosting muhitlarida (masalan Railway'ning bepul/trial tarifida)
    bir nechta service orasida umumiy fayl tizimini (Volume) bo'lishish
    imkoni cheklangan bo'lishi mumkin. Bu holatda FastAPI va Celery worker
    turli konteynerlarda ishласа, ular bir-birining vaqtinchalik fayllarini
    (masalan yuklangan PDF/DOCX) ko'ra olmaydi, chunki `/tmp` papkalari
    alohida.

    Shu muammoning oldini olish uchun bu fayl FastAPI ishga tushganda
    Celery worker jarayonini ХУДДИ ШУ KONTEYNER ichida, subprocess
    sifatida orqa fonda ishga tushiradi. Natijada FastAPI va Celery bir xil
    fayl tizimini ko'radi, va alohida Celery-only service kerak bo'lmaydi.

    Agar kelajakda alohida Celery worker service ishlatmoqchi bo'lsangiz
    (masalan Volume to'liq ishlaydigan Pro tarifga o'tsangiz), quyidagi
    ENABLE_EMBEDDED_CELERY_WORKER=false qilib environment variable orqali
    o'chirib qo'yishingiz mumkin, va alohida
    `celery -A workers.celery_tasks.celery_app worker` service'ni davom
    ettirasiz.
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router as api_router
from config.settings import get_settings

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# Bitta konteyner ichida FastAPI bilan bir qatorda ishlaydigan Celery worker
# subprocess'ining ma'lumotnomasi (referensi). Modul darajasida saqlanadi,
# shunda shutdown paytida uni to'xtatish mumkin.
_celery_worker_process: subprocess.Popen | None = None


def _should_run_embedded_celery_worker() -> bool:
    """Ushbu jarayon ichida Celery worker ishga tushirilishi kerakmi tekshiradi."""
    return os.environ.get("ENABLE_EMBEDDED_CELERY_WORKER", "true").lower() in (
        "1",
        "true",
        "yes",
    )


def _start_embedded_celery_worker() -> None:
    """Celery worker'ni joriy Python muhitida subprocess sifatida ishga tushiradi.

    `--concurrency=2` ataylab kichik tutilgan: umumiy konteyner resurslari
    (CPU/RAM) FastAPI bilan baham ko'rilgani uchun juda ko'p parallel
    worker jarayoni xotira yetishmovchiligiga (OOM) olib kelishi mumkin.
    """
    global _celery_worker_process

    cmd = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "workers.celery_tasks.celery_app",
        "worker",
        "--loglevel=info",
        "--concurrency=2",
    ]
    logger.info("Ichki Celery worker ishga tushirilmoqda: %s", " ".join(cmd))
    _celery_worker_process = subprocess.Popen(
        cmd,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def _stop_embedded_celery_worker() -> None:
    """Ilova to'xtaganda Celery worker subprocess'ini ham to'xtatadi."""
    global _celery_worker_process
    if _celery_worker_process is not None and _celery_worker_process.poll() is None:
        logger.info("Ichki Celery worker to'xtatilmoqda...")
        _celery_worker_process.terminate()
        try:
            _celery_worker_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _celery_worker_process.kill()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown logikasi (eskirgan @app.on_event o'rniga)."""
    # --- Startup ---
    settings.ensure_work_dir()
    logger.info("%s ishga tushdi. WORK_DIR=%s", settings.APP_NAME, settings.WORK_DIR)

    if _should_run_embedded_celery_worker():
        _start_embedded_celery_worker()
        atexit.register(_stop_embedded_celery_worker)
    else:
        logger.info(
            "ENABLE_EMBEDDED_CELERY_WORKER=false — ichki Celery worker "
            "ishga tushirilmadi. Alohida worker service ishlatilayotgan "
            "deb hisoblanadi."
        )

    yield

    # --- Shutdown ---
    _stop_embedded_celery_worker()


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "PDF va DOCX fayllarni original formatini saqlab tarjima qiluvchi API. "
        "Provider: Google Gemini (gemini-2.5-flash, free tier)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_PREFIX)

app.mount("/app", StaticFiles(directory="static", html=True), name="static")


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Oddiy health-check endpoint (load balancer / monitoring uchun)."""
    return {"status": "ok"}
