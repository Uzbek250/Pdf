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
import re
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

_JSON_PARSE_RETRIES = 2
_JSON_PARSE_RETRY_DELAY_SECONDS = 2.5


def _ocr_translate_prompt(target_language_name: str) -> str:
    return (
        f"Ushbu rasmdagi (skanerlangan hujjat sahifasi) barcha matnni "
        f"o'qib chiqing va {target_language_name} tiliga tarjima qiling. "
        f"Faqat tarjima qilingan matnni qaytaring, hech qanday izoh, "
        f"original matn yoki Markdown formatlash qo'shmang. Original "
        f"paragraf tuzilishini (bo'sh qatorlar bilan) saqlang."
    )


class _JsonBatchParseError(TranslationProviderError):
    """Gemini javobining JSON batch sifatida parse qilinishi muvaffaqiyatsiz bo'ldi."""

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


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

        last_parse_error: _JsonBatchParseError | None = None
        for parse_attempt in range(_JSON_PARSE_RETRIES + 1):
            response = await self._call_with_retry(_call)
            raw_text = response.text or ""
            try:
                translations = self._parse_json_array_response(
                    raw_text, expected_len=len(texts)
                )
                break
            except _JsonBatchParseError as exc:
                last_parse_error = exc
                if parse_attempt >= _JSON_PARSE_RETRIES:
                    logger.error(
                        "Gemini batch JSON parse failed after %s retries. "
                        "error_type=%s error=%s raw_response=%r",
                        _JSON_PARSE_RETRIES,
                        type(exc).__name__,
                        exc,
                        exc.raw_text,
                    )
                    raise

                logger.warning(
                    "Gemini batch JSON parse failed (attempt %s/%s, "
                    "error_type=%s error=%s). Retrying in %.1f seconds.",
                    parse_attempt + 1,
                    _JSON_PARSE_RETRIES + 1,
                    type(exc).__name__,
                    exc,
                    _JSON_PARSE_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_JSON_PARSE_RETRY_DELAY_SECONDS)
        else:  # pragma: no cover - defensive guard
            assert last_parse_error is not None
            raise last_parse_error

        return TranslationResult(
            translations=translations,
            source_language=source_language,
            raw_provider_response={"model": self._model},
        )

    @staticmethod
    def _parse_json_array_response(raw_text: str | None, expected_len: int) -> list[str]:
        """Gemini javobini JSON massivga aylantiradi.

        Gemini ba'zan Markdown code fence, tushuntirish matni yoki JSON
        massividan keyingi qo'shimcha matn bilan javob berishi mumkin.
        JSONDecoder.raw_decode() yordamida haqiqiy massivni ajratib olamiz.
        """
        if raw_text is None:
            raise _JsonBatchParseError("Gemini bo'sh javob qaytardi.", "")

        cleaned = raw_text.strip()
        fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        decoder = json.JSONDecoder()
        last_json_error: json.JSONDecodeError | None = None
        parsed: Any = None

        # Proza oldidan kelgan JSON, ```json bloklari va JSON'dan keyingi
        # "Extra data" kabi matnlarni ham qo'llab-quvvatlash uchun massivni
        # topilgan har bir '[' pozitsiyasidan raw_decode qilib ko'ramiz.
        for index, char in enumerate(cleaned):
            if char != "[":
                continue
            try:
                candidate, _end = decoder.raw_decode(cleaned, index)
            except json.JSONDecodeError as exc:
                last_json_error = exc
                continue
            if isinstance(candidate, list):
                parsed = candidate
                break

        if parsed is None:
            if last_json_error is None:
                last_json_error = json.JSONDecodeError(
                    "Gemini javobida JSON massivi topilmadi", cleaned, 0
                )
            raise _JsonBatchParseError(
                f"Gemini javobini JSON sifatida o'qib bo'lmadi: {last_json_error}",
                raw_text,
            ) from last_json_error

        if not isinstance(parsed, list):
            raise _JsonBatchParseError(
                f"Gemini javobi JSON massiv emas: {type(parsed)}", raw_text
            )

        if len(parsed) != expected_len:
            raise _JsonBatchParseError(
                f"Gemini {expected_len} ta parcha kutilgan edi, "
                f"{len(parsed)} ta qaytardi.",
                raw_text,
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
