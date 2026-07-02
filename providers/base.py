"""
Tarjima provayderlari uchun mavhum (abstract) interfeys.

Bu modul tufayli asosiy kod (services/translator.py va boshqalar) qaysi
LLM provayderi ishlatilayotganidan mutlaqo bexabar bo'ladi. Kelajakda
Gemini o'rniga boshqa provayder (masalan OpenAI, Claude, DeepL) qo'shish
uchun faqat shu interfeysni implement qiluvchi yangi klass yozish kifoya —
qolgan kodga tegish shart emas.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class TranslationResult:
    """Bitta tarjima operatsiyasi natijasi."""

    translations: list[str]
    """Kirish paragraflari bilan bir xil tartib va uzunlikdagi tarjimalar."""

    source_language: str | None = None
    """Aniqlangan (yoki berilgan) manba til kodi, agar ma'lum bo'lsa."""

    raw_provider_response: dict | None = field(default=None, repr=False)
    """Diagnostika uchun provayderning xom javobi (ixtiyoriy)."""


@dataclass(slots=True)
class LanguageDetectionResult:
    """Til aniqlash natijasi."""

    language_code: str
    confidence: float
    method: str  # "langdetect" | "gemini_fallback"


class TranslationProviderError(Exception):
    """Provayderga xos xatoliklar uchun asosiy exception klassi."""


class RateLimitError(TranslationProviderError):
    """Provayder rate-limit (429) qaytarganda ko'tariladi."""


class TranslationProvider(ABC):
    """Barcha tarjima provayderlari implement qilishi kerak bo'lgan interfeys."""

    @abstractmethod
    async def translate_batch(
        self,
        texts: list[str],
        target_language: str,
        source_language: str | None = None,
    ) -> TranslationResult:
        """Matnlar ro'yxatini bitta so'rovda (batch) tarjima qiladi.

        Args:
            texts: Tarjima qilinadigan matn parchalari (paragraflar).
            target_language: Maqsad til kodi (masalan "uz").
            source_language: Agar ma'lum bo'lsa, manba til kodi. None bo'lsa,
                provayder o'zi aniqlab tarjima qilishi kerak.

        Returns:
            TranslationResult — texts bilan bir xil tartibdagi tarjimalar.

        Raises:
            TranslationProviderError: provayder xatoligi yuz berganda.
        """
        raise NotImplementedError

    @abstractmethod
    async def detect_language(self, text: str) -> LanguageDetectionResult:
        """Berilgan matn tilini aniqlaydi (LLM fallback sifatida ishlatiladi)."""
        raise NotImplementedError

    @abstractmethod
    async def ocr_image(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        """Rasmdan (skaner sahifa) matnni vision orqali o'qiydi (OCR)."""
        raise NotImplementedError

    @abstractmethod
    async def ocr_translate_image(
        self,
        image_bytes: bytes,
        target_language: str,
        mime_type: str = "image/png",
    ) -> str:
        """Skaner sahifani bevosita o'qib, tarjima qilingan matnni qaytaradi.

        Bu OCR + tarjimani bitta so'rovga birlashtirib, API chaqiruvlarini
        kamaytirish uchun ishlatiladi (skaner PDF sahifalari uchun).
        """
        raise NotImplementedError
