"""
Test 1: TranslationCache uchun kesh kaliti generatsiyasi va LRU xatti-harakati.
"""
from __future__ import annotations

import hashlib

import pytest

from app.cache.translation_cache import (
    LRUMemoryCache,
    build_cache_key,
    normalize_text,
)


def test_build_cache_key_is_sha256_16_chars_and_deterministic() -> None:
    """Kesh kaliti SHA256(target_lang + normalized_text)[:16] formatida bo'lishi kerak."""
    key1 = build_cache_key("uz", "Salom dunyo")
    key2 = build_cache_key("uz", "Salom dunyo")

    # Deterministik: bir xil kirish -> bir xil kalit
    assert key1 == key2
    # Uzunlik aniq 16 ta hex belgi
    assert len(key1) == 16

    # Qo'lda hisoblab tekshiramiz
    expected_full = hashlib.sha256(b"uz:Salom dunyo").hexdigest()
    assert key1 == expected_full[:16]


def test_build_cache_key_normalizes_whitespace_and_is_case_sensitive_for_lang() -> None:
    """Ortiqcha bo'shliqlar normalizatsiya qilinishi, til kodi lower-case qilinishi kerak."""
    key_extra_spaces = build_cache_key("uz", "Salom   dunyo\n\n")
    key_normal = build_cache_key("uz", "Salom dunyo")
    assert key_extra_spaces == key_normal

    key_upper_lang = build_cache_key("UZ", "Salom dunyo")
    assert key_upper_lang == key_normal

    # Turli tillar uchun turli kalit
    key_ru = build_cache_key("ru", "Salom dunyo")
    assert key_ru != key_normal


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("  Salom   dunyo  ") == "Salom dunyo"
    assert normalize_text("a\n\nb\tc") == "a b c"


def test_lru_memory_cache_evicts_oldest_when_full() -> None:
    """LRU kesh max_items dan oshganda eng eski elementni chiqarib tashlashi kerak."""
    cache = LRUMemoryCache(max_items=2)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.set("c", "3")  # "a" chiqarib tashlanishi kerak

    assert cache.get("a") is None
    assert cache.get("b") == "2"
    assert cache.get("c") == "3"
    assert len(cache) == 2


def test_lru_memory_cache_get_refreshes_recency() -> None:
    """get() chaqirilgan element eng yangi hisoblanishi va chiqarib tashlanmasligi kerak."""
    cache = LRUMemoryCache(max_items=2)
    cache.set("a", "1")
    cache.set("b", "2")
    cache.get("a")  # "a" ni yangilaymiz -> endi "b" eng eski
    cache.set("c", "3")  # "b" chiqarib tashlanishi kerak

    assert cache.get("a") == "1"
    assert cache.get("b") is None
    assert cache.get("c") == "3"
