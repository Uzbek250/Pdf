"""
FastAPI marshrutlari (endpoint'lar).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config.languages import DEFAULT_TARGET_LANGUAGE, is_supported, list_languages
from app.config.settings import get_settings
from app.services.translator import get_translator_service
from app.workers.celery_tasks import read_progress, translate_document_task

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


class LanguageOut(BaseModel):
    code: str
    name_en: str
    name_native: str


class TranslateResponse(BaseModel):
    task_id: str
    message: str = "Fayl qabul qilindi, tarjima navbatga qo'yildi."


class DetectLanguageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)


class DetectLanguageResponse(BaseModel):
    language_code: str


class ProgressResponse(BaseModel):
    status: str
    progress: int
    updated_at: float | None = None
    output_path: str | None = None
    error: str | None = None
    source_language: str | None = None
    is_scanned: bool | None = None


# -------------------------------------------------------------------- #
# POST /api/translate
# -------------------------------------------------------------------- #
@router.post("/translate", response_model=TranslateResponse)
async def translate_endpoint(
    file: UploadFile = File(...),
    target_lang: str = Form(default=DEFAULT_TARGET_LANGUAGE),
) -> TranslateResponse:
    """PDF yoki DOCX faylni qabul qiladi va tarjima vazifasini navbatga qo'yadi."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Fayl nomi topilmadi.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Qo'llab-quvvatlanmaydigan fayl turi: {suffix}. "
            f"Faqat {sorted(ALLOWED_EXTENSIONS)} qabul qilinadi.",
        )

    target_lang = target_lang.lower()
    if not is_supported(target_lang):
        raise HTTPException(
            status_code=400, detail=f"Qo'llab-quvvatlanmaydigan maqsad til: {target_lang}"
        )

    contents = await file.read()
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Fayl hajmi {settings.MAX_UPLOAD_SIZE_MB}MB dan oshmasligi kerak.",
        )

    task_id = str(uuid.uuid4())
    work_dir = settings.ensure_work_dir() / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    input_path = work_dir / f"input{suffix}"
    input_path.write_bytes(contents)

    translate_document_task.delay(
        task_id=task_id,
        input_path_str=str(input_path),
        original_filename=file.filename,
        target_language=target_lang,
    )

    return TranslateResponse(task_id=task_id)


# -------------------------------------------------------------------- #
# GET /api/progress/{task_id}  (Server-Sent Events)
# -------------------------------------------------------------------- #
@router.get("/progress/{task_id}")
async def progress_endpoint(task_id: str) -> StreamingResponse:
    """Tarjima vazifasi progressini SSE orqali oqim (stream) qiladi.

    Har 1 soniyada Redis'dan holatni o'qiydi va o'zgarish bo'lsa yuboradi.
    Vazifa "completed" yoki "failed" holatiga yetganda oqim yopiladi.
    """

    async def event_generator():
        last_payload: str | None = None
        max_iterations = 60 * 30  # ~30 daqiqagacha kutish (1s interval)
        for _ in range(max_iterations):
            data = read_progress(task_id)
            if data is None:
                payload = json.dumps({"status": "pending", "progress": 0})
            else:
                payload = json.dumps(data, ensure_ascii=False)

            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload

            if data is not None and data.get("status") in ("completed", "failed"):
                break

            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# -------------------------------------------------------------------- #
# GET /api/download/{task_id}
# -------------------------------------------------------------------- #
@router.get("/download/{task_id}")
async def download_endpoint(task_id: str) -> FileResponse:
    """Tayyor bo'lgan tarjima faylini yuklab beradi."""
    data = read_progress(task_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Vazifa topilmadi.")

    if data.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Fayl hali tayyor emas. Joriy holat: {data.get('status')}",
        )

    output_path_str = data.get("output_path")
    if not output_path_str:
        raise HTTPException(status_code=500, detail="Chiqish fayli yo'li topilmadi.")

    output_path = Path(output_path_str)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Fayl diskda topilmadi (muddati o'tgan bo'lishi mumkin).")

    return FileResponse(
        path=str(output_path),
        filename=output_path.name,
        media_type="application/octet-stream",
    )


# -------------------------------------------------------------------- #
# GET /api/languages
# -------------------------------------------------------------------- #
@router.get("/languages", response_model=list[LanguageOut])
async def languages_endpoint() -> list[LanguageOut]:
    """Qo'llab-quvvatlanadigan barcha tillar ro'yxatini qaytaradi."""
    return [
        LanguageOut(code=lang.code, name_en=lang.name_en, name_native=lang.name_native)
        for lang in list_languages()
    ]


# -------------------------------------------------------------------- #
# GET /api/detect-lang  (matn namunasi orqali)
# -------------------------------------------------------------------- #
@router.post("/detect-lang", response_model=DetectLanguageResponse)
async def detect_lang_endpoint(payload: DetectLanguageRequest) -> DetectLanguageResponse:
    """Berilgan matn namunasi tilini aniqlaydi."""
    translator = get_translator_service()
    try:
        code = await translator.detect_language(payload.text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Til aniqlashda xato")
        raise HTTPException(status_code=500, detail=f"Til aniqlanmadi: {exc}") from exc
    return DetectLanguageResponse(language_code=code)
