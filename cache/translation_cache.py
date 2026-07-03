"""
Uch darajali tarjima keshi.

Daraja 1: in-process LRU (OrderedDict) — eng tez, lekin jarayon
    o'chganda yo'qoladi va boshqa workerlar bilan bo'lishilmaydi.
Daraja 2: Redis — worker/process'lar orasida bo'lishiladigan, TTL bilan
    boshqariladigan doimiy kesh.
Daraja 3 (bu klassdan tashqarida): agar ikkalasida ham topilmasa,
    chaqiruvchi kod (services/translator.py) Gemini API'ga murojaat qiladi
    va natijani shu kesh orqali saqlaydi.

Cache kaliti: SHA256(target_lang + normalized_text)[:16]
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import OrderedDict
from typing import Optional

try:
    import redis.asyncio as redis_asyncio
except ImportError:  # redis kutubxonasi o'rnatilmagan bo'lishi mumkin (testlarda)
    redis_asyncio = None  # type: ignore[assignment]

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Kesh kaliti barqaror bo'lishi uchun matnni normalizatsiya qiladi.

    Ortiqcha bo'shliqlarni yig'ish va kesish orqali "Salom  dunyo" va
    "Salom dunyo" bir xil kalitga tushishini ta'minlaydi.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def build_cache_key(target_lang: str, text: str) -> str:
    """SHA256(target_lang + normalized_text)[:16] formatidagi kesh kalitini quradi."""
    normalized = normalize_text(text)
    payload = f"{target_lang.lower()}:{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


class LRUMemoryCache:
    """Oddiy, thread-unsafe bo'lmagan (asyncio bitta thread) LRU kesh."""

    def __init__(self, max_items: int) -> None:
        self._max_items = max_items
        self._store: "OrderedDict[str, str]" = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        if key not in self._store:
            return None
        # LRU: qayta ishlatilganda oxiriga suramiz
        value = self._store.pop(key)
        self._store[key] = value
        return value

    def set(self, key: str, value: str) -> None:
        if self._max_items <= 0:
            return
        if key in self._store:
            self._store.pop(key)
        elif len(self._store) >= self._max_items:
            self._store.popitem(last=False)  # eng eski elementni chiqarib tashlash
        self._store[key] = value

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


class TranslationCache:
    """LRU memory + Redis'ni birlashtiruvchi yuqori darajali kesh interfeysi."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._memory = LRUMemoryCache(self._settings.CACHE_MEMORY_MAX_ITEMS)
        self._redis: Optional["redis_asyncio.Redis"] = None
        if self._settings.CACHE_ENABLED and redis_asyncio is not None:
            try:
                self._redis = redis_asyncio.from_url(
                    self._settings.CACHE_REDIS_URL,
                    decode_responses=True,
                )
            except Exception as exc:  # noqa: BLE001 - kesh ixtiyoriy, ilovani to'xtatmaydi
                logger.warning("Redis'ga ulanib bo'lmadi, faqat memory kesh ishlatiladi: %s", exc)
                self._redis = None

    async def get(self, target_lang: str, text: str) -> Optional[str]:
        """Kesh orqali tarjimani qidiradi. Topilmasa None qaytaradi."""
        if not self._settings.CACHE_ENABLED:
            return None

        key = build_cache_key(target_lang, text)

        # 1-daraja: memory
        hit = self._memory.get(key)
        if hit is not None:
            return hit

        # 2-daraja: Redis
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._redis_key(key))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis GET xatosi: %s", exc)
                raw = None
            if raw is not None:
                value = json.loads(raw)["translation"]
                # Redis'dan topilgan qiymatni memory keshga ham yozib qo'yamiz
                self._memory.set(key, value)
                return value

        return None

    async def set(self, target_lang: str, text: str, translation: str) -> None:
        """Tarjima natijasini ikkala darajaga ham yozadi."""
        if not self._settings.CACHE_ENABLED:
            return

        key = build_cache_key(target_lang, text)
        self._memory.set(key, translation)

        if self._redis is not None:
            payload = json.dumps({"translation": translation}, ensure_ascii=False)
            try:
                await self._redis.set(
                    self._redis_key(key),
                    payload,
                    ex=self._settings.CACHE_REDIS_TTL_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis SET xatosi: %s", exc)

    async def get_many(
        self, target_lang: str, texts: list[str]
    ) -> dict[int, str]:
        """Bir nechta matn uchun keshdan topilganlarni index -> tarjima ko'rinishida qaytaradi."""
        results: dict[int, str] = {}
        for idx, text in enumerate(texts):
            cached = await self.get(target_lang, text)
            if cached is not None:
                results[idx] = cached
        return results

    @staticmethod
    def _redis_key(key: str) -> str:
        return f"translation_cache:{key}"

    async def close(self) -> None:
        """Redis ulanishini yopadi (ilova to'xtaganda chaqiriladi)."""
        if self._redis is not None:
            await self._redis.close()


_cache_singleton: Optional[TranslationCache] = None


def get_translation_cache() -> TranslationCache:
    """Butun ilova bo'ylab bitta TranslationCache instansini qaytaradi."""
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = TranslationCache()
    return _cache_singleton
