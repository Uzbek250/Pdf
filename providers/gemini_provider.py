"""
Google Gemini uchun TranslationProvider implementatsiyasi.

MUHIM: bu modul faqat rasmiy `google-genai` Python SDK'sidan foydalanadi
(OpenAI-compatible endpoint EMAS). Agar kelajakda Gemini API o'zgarsa,
faqat shu fayl o'zgaradi — services/ va boshqa qatlamlar tegilmaydi.

Hujjatlar: https://ai.google.dev/gemini-api/docs
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from google import genai
from google.genai import types as genai_types
from google.genai.errors import APIError, ClientError, ServerError

from config.settings import Settings, get_settings
from providers.base import (
    LanguageDetectionResult,
    RateLimitError,
    TranslationProvider,
    TranslationProviderError,
    TranslationResult,
)

logger = logging.getLogger(__name__)


_TRANSLATION_SYSTEM_PROMPT = """\
Siz professional tarjimonsiz. Sizga JSON massiv ko'rinishida matn \
parchalari (paragraflar) beriladi. Har bir parchani ko'rsatilgan maqsad \
tilga tarjima qiling.

QOIDALAR:
1. Faqat JSON massiv qaytaring, boshqa hech qanday matn, izoh yoki \
Markdown belgilari (```json kabi) qo'shmang.
2. Chiqish massivining uzunligi va tartibi kirish massiviga to'liq mos \
kelishi SHART.
3. Raqamlar, sanalar, shaxs ismlari, brend nomlari va URL manzillarni \
o'zgartirmang (agar tarjima tilida qabul qilingan shakli bo'lmasa).
4. Matn formatini (katta-kichik harf, tinish belgilari uslubi) tabiiy \
ravishda maqsad tilga moslashtiring.
5. Bo'sh satr (`""`) kelsa, uni ham bo'sh satr sifatida qaytaring.
6. Agar parcha allaqachon maqsad tilda bo'lsa, uni o'zgarishsiz qaytaring.
"""

_LANGUAGE_DETECTION_PROMPT = """\
Quyidagi matn qaysi tilda yozilganini aniqlang. Faqat ISO 639-1 ikki \
harfli til kodini qaytaring (masalan: "uz", "ru", "en"). Boshqa hech \
qanday matn qo'shmang.
"""

_OCR_PROMPT = """\
Ushbu rasmdagi (skanerlangan hujjat sahifasi) barcha matnni aniq va \
to'liq o'qib chiqing. Faqat matnni qaytaring, hech qanday izoh yoki \
tavsif qo'shmang. Paragraflar orasidagi bo'shliqlarni saqlang.
"""


def _ocr_translate_prompt(target_language_name: str) -> str:
    return (
        f"Ushbu rasmdagi (skanerlangan hujjat sahifasi) barcha matnni "
        f"o'qib chiqing va {target_language_name} tiliga tarjima qiling. "
        f"Faqat tarjima qilingan matnni qaytaring, hech qanday izoh, "
        f"original matn yoki Markdown formatlash qo'shmang. Original "
        f"paragraf tuzilishini (bo'sh qatorlar bilan) saqlang."
    )


class GeminiProvider(TranslationProvider):
    """`gemini-2.5-flash` (yoki sozlangan boshqa model) orqali ishlaydigan provayder."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.GEMINI_API_KEY:
            logger.warning(
                "GEMINI_API_KEY o'rnatilmagan. Gemini so'rovlari xato beradi."
            )
        # Rasmiy google-genai klienti. httpx orqali async ishlaydi.
        self._client = genai.Client(api_key=self._settings.GEMINI_API_KEY)
        self._model = self._settings.GEMINI_MODEL

    # ------------------------------------------------------------------ #
    # Umumiy retry / backoff mexanizmi
    # ------------------------------------------------------------------ #
    async def _call_with_retry(self, coro_factory):
        """`coro_factory()` chaqiruvini exponential backoff bilan qayta uradi.

        Args:
            coro_factory: har chaqirilganda yangi coroutine qaytaruvchi
                argumentsiz callable (chunki bitta coroutine ikki marta
                await qilinmaydi).
        """
        settings = self._settings
        attempt = 0
        backoff = settings.GEMINI_INITIAL_BACKOFF_SECONDS

        while True:
            try:
                return await coro_factory()
            except ClientError as exc:  # 4xx xatoliklar (429 shu yerda ham bo'lishi mumkin)
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                is_rate_limited = status == 429
                if not is_rate_limited or attempt >= settings.GEMINI_MAX_RETRIES:
                    raise (
                        RateLimitError(str(exc))
                        if is_rate_limited
                        else TranslationProviderError(str(exc))
                    ) from exc
            except ServerError as exc:  # 5xx — vaqtinchalik server muammosi
                if attempt >= settings.GEMINI_MAX_RETRIES:
                    raise TranslationProviderError(str(exc)) from exc
            except APIError as exc:  # boshqa umumiy API xatoliklari
                if attempt >= settings.GEMINI_MAX_RETRIES:
                    raise TranslationProviderError(str(exc)) from exc

            # Exponential backoff + jitter
            jitter = random.uniform(0, backoff * 0.25)
            sleep_for = min(backoff + jitter, settings.GEMINI_MAX_BACKOFF_SECONDS)
            logger.info(
                "Gemini so'rovi muvaffaqiyatsiz (urinish %s/%s). %.2f soniyadan "
                "keyin qayta urinilmoqda.",
                attempt + 1,
                settings.GEMINI_MAX_RETRIES,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, settings.GEMINI_MAX_BACKOFF_SECONDS)
            attempt += 1

    # ------------------------------------------------------------------ #
    # Batch tarjima
    # ------------------------------------------------------------------ #
    async def translate_batch(
        self,
        texts: list[str],
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        if not texts:
            return TranslationResult(translations=[], source_language=source_language)

        from config.languages import get_language

        target_lang_name = get_language(target_language).name_en
        source_hint = (
            f" Manba til: {get_language(source_language).name_en}."
            if source_language
            else ""
        )

        user_prompt = (
            f"Maqsad til: {target_lang_name}.{source_hint}\n\n"
            f"Kirish JSON massivi:\n{json.dumps(texts, ensure_ascii=False)}"
        )

        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_TRANSLATION_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                    http_options=genai_types.HttpOptions(
                        timeout=int(self._settings.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000)
                    ),
                ),
            )

        response = await self._call_with_retry(_call)
        translations = self._parse_json_array_response(response.text, expected_len=len(texts))

        return TranslationResult(
            translations=translations,
            source_language=source_language,
            raw_provider_response={"model": self._model},
        )

    @staticmethod
    def _parse_json_array_response(raw_text: str | None, expected_len: int) -> list[str]:
        """Gemini javobini xavfsiz JSON massivga aylantiradi.

        Model ba'zan ```json qatorlari bilan o'rab qaytarishi mumkin —
        shuni tozalab olamiz.
        """
        if raw_text is None:
            raise TranslationProviderError("Gemini bo'sh javob qaytardi.")

        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()

        try:
            parsed: Any = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise TranslationProviderError(
                f"Gemini javobini JSON sifatida o'qib bo'lmadi: {exc}"
            ) from exc

        if not isinstance(parsed, list):
            raise TranslationProviderError(
                f"Gemini javobi JSON massiv emas: {type(parsed)}"
            )

        if len(parsed) != expected_len:
            raise TranslationProviderError(
                f"Gemini {expected_len} ta parcha kutilgan edi, "
                f"{len(parsed)} ta qaytardi."
            )

        return [str(item) for item in parsed]

    # ------------------------------------------------------------------ #
    # Til aniqlash (fallback)
    # ------------------------------------------------------------------ #
    async def detect_language(self, text: str) -> LanguageDetectionResult:
        sample = text.strip()[:500]  # namuna sifatida yetarli

        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._model,
                contents=sample,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_LANGUAGE_DETECTION_PROMPT,
                    temperature=0.0,
                    http_options=genai_types.HttpOptions(
                        timeout=int(self._settings.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000)
                    ),
                ),
            )

        response = await self._call_with_retry(_call)
        code = (response.text or "").strip().lower()[:2]
        if not code.isalpha():
            raise TranslationProviderError(
                f"Gemini noto'g'ri til kodi qaytardi: {response.text!r}"
            )
        return LanguageDetectionResult(
            language_code=code, confidence=1.0, method="gemini_fallback"
        )

    # ------------------------------------------------------------------ #
    # OCR (vision)
    # ------------------------------------------------------------------ #
    async def ocr_image(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._model,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    _OCR_PROMPT,
                ],
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    http_options=genai_types.HttpOptions(
                        timeout=int(self._settings.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000)
                    ),
                ),
            )

        response = await self._call_with_retry(_call)
        return response.text or ""

    async def ocr_translate_image(
        self,
        image_bytes: bytes,
        target_language: str,
        mime_type: str = "image/png",
    ) -> str:
        from config.languages import get_language

        target_lang_name = get_language(target_language).name_en
        prompt = _ocr_translate_prompt(target_lang_name)

        async def _call():
            return await self._client.aio.models.generate_content(
                model=self._model,
                contents=[
                    genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
                config=genai_types.GenerateContentConfig(
                    temperature=0.2,
                    http_options=genai_types.HttpOptions(
                        timeout=int(self._settings.GEMINI_REQUEST_TIMEOUT_SECONDS * 1000)
                    ),
                ),
            )

        response = await self._call_with_retry(_call)
        return response.text or ""
