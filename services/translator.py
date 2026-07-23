"""
Tarjima orkestratori: til aniqlash, keshni tekshirish, cache-miss bo'lgan
parchalarni batch qilib provayderga yuborish va natijalarni qayta keshlash.
"""
from __future__ import annotations

import asyncio
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

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Matnning taxminiy token sonini baholaydi.

        Aniq tokenizatsiya qilmaydi (bu qo'shimcha bog'liqlik va sekinlik
        keltiradi); o'rniga ~4 belgi = 1 token taxminidan foydalanadi, bu
        Gemini kabi modellar uchun amalda yetarlicha yaqin natija beradi.
        Tarjima natijasi manba matndan uzunroq bo'lishi mumkinligini
        hisobga olib (masalan o'zbekcha matn inglizchadan uzunroq),
        xavfsizlik uchun 1.3x zaxira ko'paytmasi qo'llaniladi.
        """
        if not text:
            return 0
        return int((len(text) / 4) * 1.3)

    def _build_token_batches(
        self, indices: list[int], paragraphs: list[str]
    ) -> list[list[int]]:
        """Kesh-miss bo'lgan paragraf indekslarini taxminiy OUTPUT token
        budjeti bo'yicha guruhlarga bo'ladi (fixed paragraph count o'rniga).

        Har bir guruh ``BATCH_MAX_OUTPUT_TOKENS`` chegarasidan oshmaslikka
        harakat qiladi. Yakka o'zi shu chegaradan katta bo'lgan juda uzun
        bitta paragraf ham hech qachon bo'linmaydi — o'z-o'zidan alohida
        guruh bo'lib qoladi (provayder xatosi bermasin uchun).
        """
        max_tokens = self._settings.BATCH_MAX_OUTPUT_TOKENS
        batches: list[list[int]] = []
        current_batch: list[int] = []
        current_tokens = 0

        for idx in indices:
            para_tokens = self._estimate_tokens(paragraphs[idx])

            if current_batch and current_tokens + para_tokens > max_tokens:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(idx)
            current_tokens += para_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    async def _translate_one_batch(
        self,
        batch_indices: list[int],
        paragraphs: list[str],
        target_language: str,
        source_language: str | None,
        semaphore: asyncio.Semaphore,
    ) -> list[tuple[int, str]]:
        """Bitta batch'ni tarjima qiladi va (index, tarjima) juftliklarini qaytaradi.

        Semaphore orqali bir vaqtdagi so'rovlar soni ``BATCH_MAX_CONCURRENCY``
        bilan cheklanadi — provayderning mavjud retry/backoff mexanizmi
        (429 va boshqa xatolar uchun) ishlatiladi va bu yerda qayta
        amalga oshirilmaydi.
        """
        batch_texts = [paragraphs[i] for i in batch_indices]
        async with semaphore:
            result = await self._provider.translate_batch(
                texts=batch_texts,
                target_language=target_language,
                source_language=source_language,
            )

        pairs: list[tuple[int, str]] = []
        for idx, translation in zip(batch_indices, result.translations):
            pairs.append((idx, translation))
        return pairs

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
        3. Kesh-miss bo'lgan parchalar taxminiy OUTPUT token budjeti
           (`BATCH_MAX_OUTPUT_TOKENS`) bo'yicha dinamik guruhlarga bo'linadi
           — fixed paragraph-count o'rniga.
        4. Guruhlar bir vaqtning o'zida, `BATCH_MAX_CONCURRENCY` bilan
           cheklangan parallel so'rovlar sifatida provayderga yuboriladi.
        5. Yangi tarjimalar keshga yoziladi va yakuniy natija tartib
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

        # Kesh-miss bo'lgan indekslarni token-budjeti bo'yicha dinamik
        # guruhlarga bo'lamiz (fixed BATCH_PARAGRAPH_SIZE o'rniga)
        token_batches = self._build_token_batches(indices_to_translate, paragraphs)

        if token_batches:
            semaphore = asyncio.Semaphore(self._settings.BATCH_MAX_CONCURRENCY)
            tasks = [
                self._translate_one_batch(
                    batch_indices=batch_indices,
                    paragraphs=paragraphs,
                    target_language=target_language,
                    source_language=source_language,
                    semaphore=semaphore,
                )
                for batch_indices in token_batches
            ]

            # Guruhlar parallel yuboriladi; tugash tartibi farqli bo'lishi
            # mumkin, lekin har bir natija o'z asl indeksi bilan qaytadi,
            # shuning uchun yakuniy tartib buzilmaydi.
            batch_results = await asyncio.gather(*tasks)

            for pairs in batch_results:
                for idx, translation in pairs:
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
