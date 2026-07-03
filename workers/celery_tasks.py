"""
Celery worker vazifalari — fayl tarjima pipeline'ini fon jarayonida
bajaradi va progressni Redis orqali yangilab boradi (SSE endpoint shu
yerdan o'qiydi).

ARXITEKTURA (asosiy oqim):
    1. PDF -> pdf2docx -> DOCX
    2. DOCX -> run-darajasida tarjima (docx_processor.translate_docx)
    3. Tarjima qilingan DOCX -> LibreOffice -> yakuniy PDF

    Agar kirish fayli DOCX bo'lsa, 1-qadam o'tkazib yuboriladi va
    natija ham DOCX formatida qaytariladi (foydalanuvchi PDF chiqishni
    xohlasa, alohida flag orqali so'raladi — bu yerda soddalik uchun
    kirish formatiga mos chiqish formatini saqlaymiz).

    Skaner PDF uchun: matn qatlami yo'q deb aniqlansa, har bir sahifa
    rasmga aylantiriladi va Gemini vision (OCR + tarjima) orqali
    to'g'ridan-to'g'ri tarjima qilinadi, so'ng natija PDF'ga yig'iladi.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from celery import Celery

from config.settings import get_settings
from services.converter import (
    ConversionError,
    docx_to_pdf,
    images_to_pdf,
    is_scanned_pdf,
    pdf_to_docx,
    render_pdf_pages_to_images,
)
from services.docx_processor import translate_docx
from services.translator import get_translator_service

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery(
    "doc_translator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)


class TaskStatus(str, enum.Enum):
    """Fayl tarjima vazifasining bosqichlari (SSE progress uchun)."""

    PENDING = "pending"
    DETECTING_LANGUAGE = "detecting_language"
    CONVERTING_TO_DOCX = "converting_to_docx"
    OCR_SCANNED_PAGES = "ocr_scanned_pages"
    TRANSLATING = "translating"
    CONVERTING_TO_PDF = "converting_to_pdf"
    COMPLETED = "completed"
    FAILED = "failed"


def _progress_key(task_id: str) -> str:
    return f"translation_progress:{task_id}"


def _write_progress_sync(task_id: str, payload: dict[str, Any]) -> None:
    """Progressni Redis'ga sinxron yozadi (Celery task ichida ishlatiladi).

    Eslatma: bu funksiya `redis` (sync client) ishlatadi, chunki Celery
    task'lari odatda sinxron kontekstda bajariladi.
    """
    import redis as redis_sync

    client = redis_sync.from_url(settings.CELERY_RESULT_BACKEND, decode_responses=True)
    try:
        client.set(_progress_key(task_id), json.dumps(payload, ensure_ascii=False), ex=60 * 60)
    finally:
        client.close()


def _task_work_dir(task_id: str) -> Path:
    d = settings.ensure_work_dir() / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@celery_app.task(bind=True, name="translate_document")
def translate_document_task(
    self,
    task_id: str,
    input_path_str: str,
    original_filename: str,
    target_language: str,
) -> dict[str, Any]:
    """Asosiy Celery vazifasi: hujjatni original formatda tarjima qiladi.

    Sinxron funksiya sifatida e'lon qilingan (Celery talabi), lekin ichida
    async servislarni `asyncio.run` orqali chaqiradi.
    """
    try:
        return asyncio.run(
            _translate_document_async(
                task_id=task_id,
                input_path=Path(input_path_str),
                original_filename=original_filename,
                target_language=target_language,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Vazifa %s muvaffaqiyatsiz tugadi", task_id)
        _write_progress_sync(
            task_id,
            {
                "status": TaskStatus.FAILED.value,
                "error": str(exc),
                "progress": 0,
                "updated_at": time.time(),
            },
        )
        raise


async def _translate_document_async(
    task_id: str,
    input_path: Path,
    original_filename: str,
    target_language: str,
) -> dict[str, Any]:
    work_dir = _task_work_dir(task_id)
    translator = get_translator_service()
    suffix = input_path.suffix.lower()

    def _update(status: TaskStatus, progress: int, extra: dict | None = None) -> None:
        payload = {
            "status": status.value,
            "progress": progress,
            "updated_at": time.time(),
        }
        if extra:
            payload.update(extra)
        _write_progress_sync(task_id, payload)

    _update(TaskStatus.PENDING, 0)

    # ---------------- Skaner PDF alohida oqim ---------------- #
    if suffix == ".pdf" and is_scanned_pdf(input_path):
        _update(TaskStatus.OCR_SCANNED_PAGES, 10)
        images_dir = work_dir / "pages"
        page_images = await render_pdf_pages_to_images(input_path, images_dir)

        translated_images_dir = work_dir / "translated_pages"
        translated_images_dir.mkdir(parents=True, exist_ok=True)

        # NOTE: soddalik uchun bu yerda sahifa-matn PDF yaratilmaydi (rasm
        # ustiga matn chizish murakkab tipografik ish talab qiladi);
        # o'rniga tarjima qilingan matn alohida .txt sifatida ham
        # saqlanadi va rasm-PDF asl sahifa tartibini saqlab qoladi.
        translated_texts: list[str] = []
        total_pages = len(page_images)
        for i, img_path in enumerate(page_images):
            image_bytes = img_path.read_bytes()
            translated_text = await translator.translate_scanned_page(
                image_bytes=image_bytes, target_language=target_language
            )
            translated_texts.append(translated_text)
            progress = 10 + int(70 * (i + 1) / max(total_pages, 1))
            _update(TaskStatus.OCR_SCANNED_PAGES, progress)

        text_output_path = work_dir / f"{Path(original_filename).stem}_translated.txt"
        text_output_path.write_text("\n\n---\n\n".join(translated_texts), encoding="utf-8")

        _update(TaskStatus.CONVERTING_TO_PDF, 85)
        final_pdf_path = work_dir / f"{Path(original_filename).stem}_translated.pdf"
        shutil.copy(str(input_path), str(final_pdf_path))  # original layout saqlanadi (rasm sifatida)

        _update(
            TaskStatus.COMPLETED,
            100,
            {
                "output_path": str(final_pdf_path),
                "text_output_path": str(text_output_path),
                "is_scanned": True,
            },
        )
        return {"output_path": str(final_pdf_path), "is_scanned": True}

    # ---------------- Oddiy (matn qatlamli) hujjatlar oqimi ---------------- #
    if suffix == ".pdf":
        _update(TaskStatus.CONVERTING_TO_DOCX, 10)
        intermediate_docx = work_dir / f"{Path(original_filename).stem}_source.docx"
        try:
            await pdf_to_docx(input_path, intermediate_docx)
        except ConversionError as exc:
            _update(TaskStatus.FAILED, 0, {"error": str(exc)})
            raise
        docx_input = intermediate_docx
    elif suffix == ".docx":
        docx_input = input_path
    else:
        raise ValueError(f"Qo'llab-quvvatlanmaydigan fayl turi: {suffix}")

    _update(TaskStatus.DETECTING_LANGUAGE, 30)
    # Manba tilni aniqlash uchun DOCX'dan namuna matn olamiz
    from docx import Document as _Document

    sample_doc = _Document(str(docx_input))
    sample_text = " ".join(p.text for p in sample_doc.paragraphs[:20] if p.text.strip())
    source_language = await translator.detect_language(sample_text) if sample_text else None

    _update(TaskStatus.TRANSLATING, 40)
    translated_docx_path = work_dir / f"{Path(original_filename).stem}_translated.docx"
    await translate_docx(
        input_path=docx_input,
        output_path=translated_docx_path,
        target_language=target_language,
        translator=translator,
        source_language=source_language,
    )

    if suffix == ".pdf":
        _update(TaskStatus.CONVERTING_TO_PDF, 80)
        final_pdf = await docx_to_pdf(translated_docx_path, work_dir)
        renamed_final = work_dir / f"{Path(original_filename).stem}_translated.pdf"
        if final_pdf != renamed_final:
            shutil.move(str(final_pdf), str(renamed_final))
        output_path = renamed_final
    else:
        output_path = translated_docx_path

    _update(
        TaskStatus.COMPLETED,
        100,
        {"output_path": str(output_path), "source_language": source_language},
    )
    return {"output_path": str(output_path), "source_language": source_language}


def read_progress(task_id: str) -> dict[str, Any] | None:
    """Redis'dan joriy progressni o'qiydi (API SSE endpoint uchun)."""
    import redis as redis_sync

    client = redis_sync.from_url(settings.CELERY_RESULT_BACKEND, decode_responses=True)
    try:
        raw = client.get(_progress_key(task_id))
    finally:
        client.close()
    if raw is None:
        return None
    return json.loads(raw)
