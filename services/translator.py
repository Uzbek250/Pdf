"""
Tarjima orkestratori: til aniqlash, keshni tekshirish, cache-miss bo'lgan
parchalarni batch qilib provayderga yuborish va natijalarni qayta keshlash.
"""
from __future__ import annotations

import logging

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from cache.translation_cache import TranslationCache, get_translation_cache
from config.languages import DEFAULT_TARGET_LANGUAGE, is_supported
from config.settings import Settings, get_settings
from providers.base import TranslationProvider
from providers.gemini_provider import GeminiProvider

logger = logging.getLogger(__name__)

# langdetect natijalari deterministik bo'lishi uchun seed o'rnatiladi
DetectorFactory.seed = 0


class LanguageDetectionFailed(Exception):
    """Til hech qanday usul bilan aniqlanmasa ko'tariladi."""


class TranslatorService:
    """Yuqori darajadagi tarjima xizmati (provider-agnostic)."""

    def __init__(
        self,
        provider: TranslationProvider | None = None,
        cache: TranslationCache | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._provider = provider or GeminiProvider(self._settings)
        self._cache = cache or get_translation_cache()

    # ------------------------------------------------------------------ #
    # Til aniqlash
    # ------------------------------------------------------------------ #
    async def detect_language(self, text: str) -> str:
        """Matn tilini aniqlaydi.

        Avval tez va bepul `langdetect` kutubxonasi ishlatiladi. Agar uning
        ishonchlilik darajasi (probability) sozlangan chegaradan (odatda
        0.95) past bo'lsa, Gemini fallback sifatida chaqiriladi — bu API
        chaqiruvlarini tejaydi, chunki aksariyat holatlarda langdetect
        yetarlicha ishonchli natija beradi.
        """
        stripped = text.strip()
        if not stripped:
            return DEFAULT_TARGET_LANGUAGE

        try:
            candidates = detect_langs(stripped)
        except LangDetectException:
            candidates = []

        if candidates:
            best = candidates[0]
            if best.prob > self._settings.LANGDETECT_CONFIDENCE_THRESHOLD:
                logger.debug(
                    "Til langdetect orqali aniqlandi: %s (%.3f)", best.lang, best.prob
                )
                return best.lang

        # Fallback: Gemini'ga so'rov yuboramiz
        logger.debug("langdetect ishonchsiz, Gemini fallback ishlatilmoqda.")
        result = await self._provider.detect_language(stripped)
        return result.language_code

    # ------------------------------------------------------------------ #
    # Batch tarjima (kesh bilan)
    # ------------------------------------------------------------------ #
    async def translate_paragraphs(
        self,
        paragraphs: list[str],
        target_language: str,
        source_language: str | None = None,
    ) -> list[str]:
        """Paragraflar ro'yxatini tarjima qiladi, kesh va batching orqali.

        Oqim:
        1. Bo'sh/faqat bo'shliqdan iborat parchalar tarjimasiz o'tkaziladi.
        2. Har bir nobo'sh parcha uchun kesh tekshiriladi.
        3. Kesh-miss bo'lgan parchalar `BATCH_PARAGRAPH_SIZE` o'lchamdagi
           guruhlarga bo'linib, provayderga yuboriladi.
        4. Yangi tarjimalar keshga yoziladi va yakuniy natija tartib
           bo'yicha qayta yig'iladi.
        """
        if not paragraphs:
            return []

        if not is_supported(target_language):
            raise ValueError(f"Qo'llab-quvvatlanmaydigan maqsad til: {target_language}")

        final_results: list[str | None] = [None] * len(paragraphs)
        indices_to_translate: list[int] = []

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                final_results[idx] = para  # bo'sh joy — o'zgarishsiz
                continue

            cached = await self._cache.get(target_language, para)
            if cached is not None:
                final_results[idx] = cached
            else:
                indices_to_translate.append(idx)

        # Kesh-miss bo'lgan indexlarni batch'larga bo'lamiz
        batch_size = self._settings.BATCH_PARAGRAPH_SIZE
        for start in range(0, len(indices_to_translate), batch_size):
            batch_indices = indices_to_translate[start : start + batch_size]
            batch_texts = [paragraphs[i] for i in batch_indices]

            result = await self._provider.translate_batch(
                texts=batch_texts,
                target_language=target_language,
                source_language=source_language,
            )

            for idx, translation in zip(batch_indices, result.translations):
                final_results[idx] = translation
                await self._cache.set(target_language, paragraphs[idx], translation)

        # Barcha elementlar to'ldirilgan bo'lishi kerak
        return [r if r is not None else "" for r in final_results]

    # ------------------------------------------------------------------ #
    # OCR + tarjima (skaner sahifalar uchun)
    # ------------------------------------------------------------------ #
    async def translate_scanned_page(
        self, image_bytes: bytes, target_language: str, mime_type: str = "image/png"
    ) -> str:
        """Skaner sahifa rasmini bevosita o'qib-tarjima qiladi (vision orqali)."""
        if not is_supported(target_language):
            raise ValueError(f"Qo'llab-quvvatlanmaydigan maqsad til: {target_language}")
        return await self._provider.ocr_translate_image(
            image_bytes=image_bytes,
            target_language=target_language,
            mime_type=mime_type,
        )

    async def ocr_page(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        """Skaner sahifadan faqat matnni o'qiydi (tarjimasiz, masalan til aniqlash uchun)."""
        return await self._provider.ocr_image(image_bytes=image_bytes, mime_type=mime_type)


_translator_singleton: TranslatorService | None = None


def get_translator_service() -> TranslatorService:
    """Butun ilova bo'ylab bitta TranslatorService instansini qaytaradi."""
    global _translator_singleton
    if _translator_singleton is None:
        _translator_singleton = TranslatorService()
    return _translator_singleton
