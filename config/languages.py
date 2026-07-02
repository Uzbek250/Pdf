"""
Qo'llab-quvvatlanadigan tillar ro'yxati va yordamchi funksiyalar.

Kodlar ISO 639-1 standartiga mos (tojik va qirg'iz kabi ba'zi tillar uchun
ISO 639-1 mavjud bo'lsa ham, Gemini modeliga so'rov yuborishda til nomini
to'liq yozamiz, chunki bu tarjima sifatini oshiradi).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    """Bitta tilni tavsiflovchi immutable struktura."""

    code: str          # ISO 639-1 kod, masalan "uz"
    name_en: str        # Ingliz tilidagi nomi (model uchun)
    name_native: str    # O'z tilidagi nomi (UI uchun)


SUPPORTED_LANGUAGES: dict[str, Language] = {
    "uz": Language("uz", "Uzbek", "O'zbekcha"),
    "ru": Language("ru", "Russian", "Русский"),
    "en": Language("en", "English", "English"),
    "tr": Language("tr", "Turkish", "Türkçe"),
    "kk": Language("kk", "Kazakh", "Қазақша"),
    "de": Language("de", "German", "Deutsch"),
    "ko": Language("ko", "Korean", "한국어"),
    "ar": Language("ar", "Arabic", "العربية"),
    "fa": Language("fa", "Persian", "فارسی"),
    "tg": Language("tg", "Tajik", "Тоҷикӣ"),
    "ky": Language("ky", "Kyrgyz", "Кыргызча"),
    "fr": Language("fr", "French", "Français"),
    "es": Language("es", "Spanish", "Español"),
    "zh": Language("zh", "Chinese", "中文"),
}

DEFAULT_TARGET_LANGUAGE = "uz"


def is_supported(code: str) -> bool:
    """Berilgan til kodi qo'llab-quvvatlanadimi tekshiradi."""
    return code.lower() in SUPPORTED_LANGUAGES


def get_language(code: str) -> Language:
    """Til kodiga mos Language obyektini qaytaradi.

    Raises:
        KeyError: kod qo'llab-quvvatlanmasa.
    """
    key = code.lower()
    if key not in SUPPORTED_LANGUAGES:
        raise KeyError(f"Qo'llab-quvvatlanmaydigan til kodi: {code!r}")
    return SUPPORTED_LANGUAGES[key]


def list_languages() -> list[Language]:
    """Barcha qo'llab-quvvatlanadigan tillar ro'yxatini qaytaradi."""
    return list(SUPPORTED_LANGUAGES.values())
