"""
Test 3: TranslatorService orkestratsiyasi — kesh-hit/miss, batching va
langdetect->Gemini fallback zanjiri, hammasi mock provider bilan (haqiqiy
API chaqiruvisiz).
"""
from __future__ import annotations

import pytest

from .cache.translation_cache import TranslationCache
from .providers.base import (
    LanguageDetectionResult,
    TranslationProvider,
    TranslationResult,
)
from .services.translator import TranslatorService


class FakeProvider(TranslationProvider):
    """Test uchun soxta provayder — haqiqiy tarmoq so'rovi yubormaydi."""

    def __init__(self) -> None:
        self.translate_batch_calls: list[list[str]] = []
        self.detect_language_calls: list[str] = []

    async def translate_batch(
        self, texts: list[str], target_language: str, source_language: str | None = None
    ) -> TranslationResult:
        self.translate_batch_calls.append(list(texts))
        # Oddiy "tarjima": har bir matnga prefiks qo'shamiz
        translated = [f"[{target_language}] {t}" for t in texts]
        return TranslationResult(translations=translated, source_language=source_language)

    async def detect_language(self, text: str) -> LanguageDetectionResult:
        self.detect_language_calls.append(text)
        return LanguageDetectionResult(language_code="en", confidence=1.0, method="gemini_fallback")

    async def ocr_image(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        return "OCR natija"

    async def ocr_translate_image(
        self, image_bytes: bytes, target_language: str, mime_type: str = "image/png"
    ) -> str:
        return f"[{target_language}] OCR tarjima"


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def disabled_cache(test_settings) -> TranslationCache:
    """CACHE_ENABLED=False bo'lgan sozlamalar bilan kesh (haqiqiy Redis kerak emas)."""
    return TranslationCache(settings=test_settings)


@pytest.mark.asyncio
async def test_translate_paragraphs_preserves_order_and_skips_empty(
    fake_provider: FakeProvider, disabled_cache: TranslationCache, test_settings
) -> None:
    """Tarjima natijalari kirish tartibiga mos bo'lishi va bo'sh qatorlar o'zgarishsiz qolishi kerak."""
    service = TranslatorService(provider=fake_provider, cache=disabled_cache, settings=test_settings)

    paragraphs = ["Salom", "", "Dunyo", "   "]
    result = await service.translate_paragraphs(paragraphs, target_language="en")

    assert result[0] == "[en] Salom"
    assert result[1] == ""  # bo'sh qator o'zgarishsiz
    assert result[2] == "[en] Dunyo"
    assert result[3] == "   "  # faqat bo'shliq ham o'zgarishsiz


@pytest.mark.asyncio
async def test_translate_paragraphs_respects_batch_size(
    fake_provider: FakeProvider, disabled_cache: TranslationCache, test_settings
) -> None:
    """BATCH_PARAGRAPH_SIZE=2 bo'lganda, 5 ta parcha 3 ta so'rovga bo'linishi kerak (2+2+1)."""
    service = TranslatorService(provider=fake_provider, cache=disabled_cache, settings=test_settings)

    paragraphs = [f"Matn {i}" for i in range(5)]
    await service.translate_paragraphs(paragraphs, target_language="ru")

    assert len(fake_provider.translate_batch_calls) == 3
    assert [len(batch) for batch in fake_provider.translate_batch_calls] == [2, 2, 1]


@pytest.mark.asyncio
async def test_translate_paragraphs_invalid_target_language_raises(
    fake_provider: FakeProvider, disabled_cache: TranslationCache, test_settings
) -> None:
    """Qo'llab-quvvatlanmaydigan til kodi berilsa ValueError ko'tarilishi kerak."""
    service = TranslatorService(provider=fake_provider, cache=disabled_cache, settings=test_settings)

    with pytest.raises(ValueError):
        await service.translate_paragraphs(["matn"], target_language="xx")


@pytest.mark.asyncio
async def test_detect_language_uses_langdetect_for_confident_text(
    fake_provider: FakeProvider, disabled_cache: TranslationCache, test_settings
) -> None:
    """Aniq ingliz tilidagi uzun matn uchun langdetect ishonchli natija berishi
    va Gemini fallback chaqirilmasligi kerak."""
    service = TranslatorService(provider=fake_provider, cache=disabled_cache, settings=test_settings)

    # Uzun va aniq ingliz matni — langdetect yuqori ishonch bilan aniqlashi kutiladi
    confident_english_text = (
        "The quick brown fox jumps over the lazy dog. This is a long and "
        "clear English sentence written specifically to ensure that the "
        "language detection library can identify it with high confidence."
    )
    detected = await service.detect_language(confident_english_text)

    assert detected == "en"
    # Gemini fallback chaqirilmagan bo'lishi kerak (langdetect yetarli ishonch bergan)
    assert len(fake_provider.detect_language_calls) == 0


@pytest.mark.asyncio
async def test_translate_paragraphs_uses_cache_and_avoids_duplicate_provider_calls(
    fake_provider: FakeProvider, test_settings
) -> None:
    """Bir xil matn ikkinchi marta so'ralganda provayder qayta chaqirilmasligi kerak (kesh orqali)."""
    # Bu test uchun keshni yoqamiz
    cache_enabled_settings = test_settings.model_copy(update={"CACHE_ENABLED": True})
    cache = TranslationCache(settings=cache_enabled_settings)
    service = TranslatorService(provider=fake_provider, cache=cache, settings=cache_enabled_settings)

    # Birinchi chaqiruv — provayderga borishi kerak
    await service.translate_paragraphs(["Salom dunyo"], target_language="en")
    assert len(fake_provider.translate_batch_calls) == 1

    # Ikkinchi chaqiruv — xuddi shu matn, kesh orqali qaytarilishi kerak
    result2 = await service.translate_paragraphs(["Salom dunyo"], target_language="en")
    assert result2 == ["[en] Salom dunyo"]
    assert len(fake_provider.translate_batch_calls) == 1  # o'zgarmagan — provayder chaqirilmadi
