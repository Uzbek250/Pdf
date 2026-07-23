"""
Ilova sozlamalari. .env faylidan o'qiladi (pydantic-settings orqali).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Butun ilova bo'ylab ishlatiladigan markazlashgan sozlamalar."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Umumiy ----
    APP_NAME: str = "Document Translator API"
    APP_ENV: Literal["development", "production", "testing"] = "development"
    DEBUG: bool = True

    # ---- Gemini provider ----
    GEMINI_API_KEY: str = Field(default="", description="Google AI Studio API kaliti")
    GEMINI_MODEL: str = Field(default="gemini-2.5-flash")
    GEMINI_MAX_RETRIES: int = Field(default=5, ge=0)
    GEMINI_INITIAL_BACKOFF_SECONDS: float = Field(default=1.0, gt=0)
    GEMINI_MAX_BACKOFF_SECONDS: float = Field(default=60.0, gt=0)
    GEMINI_REQUEST_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)

    # ---- Batching ----
    BATCH_PARAGRAPH_SIZE: int = Field(default=20, ge=1, le=100)
    # Token-based dynamic batching: paragraflar fixed-count o'rniga
    # taxminiy OUTPUT token budjeti bo'yicha guruhlanadi. Gemini'ning
    # 64K output limitidan xavfsiz masofada, truncation'ni oldini olish
    # uchun ~42K atrofida saqlanadi.
    BATCH_MAX_OUTPUT_TOKENS: int = Field(default=42000, ge=1000)
    # Bir vaqtda Gemini'ga yuboriladigan parallel batch so'rovlar soni.
    # Bitta API key bilan ishlaganda ToS/rate-limit xavfini kamaytirish
    # uchun past qiymatda saqlanadi.
    BATCH_MAX_CONCURRENCY: int = Field(default=3, ge=1, le=20)

    # ---- Til aniqlash ----
    LANGDETECT_CONFIDENCE_THRESHOLD: float = Field(default=0.95, ge=0.0, le=1.0)
    LANGDETECT_SEED: int = Field(default=0)

    # ---- Cache ----
    CACHE_MEMORY_MAX_ITEMS: int = Field(default=2000, ge=0)
    CACHE_REDIS_URL: str = Field(default="redis://localhost:6379/0")
    CACHE_REDIS_TTL_SECONDS: int = Field(default=60 * 60 * 24 * 30)  # 30 kun
    CACHE_ENABLED: bool = True

    # ---- Fayl / konvertatsiya ----
    WORK_DIR: Path = Field(default=Path("/tmp/doc_translator"))
    MAX_UPLOAD_SIZE_MB: int = Field(default=50, ge=1)
    LIBREOFFICE_BINARY: str = Field(default="libreoffice")
    LIBREOFFICE_TIMEOUT_SECONDS: int = Field(default=180, ge=1)

    # ---- Skaner PDF / OCR aniqlash ----
    SCANNED_PDF_MIN_CHARS_PER_PAGE: int = Field(
        default=20,
        description="Agar sahifada shundan kam belgi topilsa, u skaner deb hisoblanadi.",
    )

    # ---- Celery / Redis broker ----
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6379/2")

    # ---- API ----
    API_PREFIX: str = "/api"
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["*"])

    def ensure_work_dir(self) -> Path:
        """WORK_DIR mavjudligini ta'minlaydi va uni qaytaradi."""
        self.WORK_DIR.mkdir(parents=True, exist_ok=True)
        return self.WORK_DIR


@lru_cache
def get_settings() -> Settings:
    """Settings obyektini keshlab qaytaradi (bitta instance butun jarayon uchun)."""
    return Settings()
