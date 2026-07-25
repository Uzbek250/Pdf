"""
Uch darajali tarjima keshi.

Daraja 1: in-process LRU (OrderedDict) — eng tez, lekin jarayon
    o'chganda yo'qoladi va boshqa workerlar bilan bo'lishilmaydi.
Daraja 2: Redis — worker/process'lar orasida bo'lishiladigan, TTL bilan
    boshqariladigan doimiy kesh.
Daraja 3 (bu klassdan tashqarida): agar ikkalasida ham topilmasa,
    chaqiruvchi kod (services/translator.py) Gemini API'ga murojaat qiladi
    va natijani shu kesh orqali saqlaydi.

Cache kaliti: SHA256(model_version + source_lang + target_lang + normalized_text)[:16]

Model versiyasi kalitga qo'shilgan sababi: agar kelajakda GEMINI_MODEL
yangilansa (masalan sifatliroq modelga o'tilsa) yoki tarjima prompti
o'zgartirilsa, eski kesh yozuvlari yangi so'rovlarga aralashib
ketmasligi kerak — model/prompt o'zgarganda avtomatik ravishda yangi
kesh maydoni boshlanadi, eskisi esa uzoq TTL orqali vaqti bilan o'zi
tozalanadi (qo'lda tozalash shart emas).
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


def build_cache_key(
    target_lang: str, text: str, source_lang: str | None, model_version: str
) -> str:
    """SHA256(model_version + source_lang + target_lang + normalized_text)[:16]
    formatidagi kesh kalitini quradi.

    ``source_lang`` "auto"/None bo'lishi mumkin — bu holatda ham barqaror
    kalit uchun "auto" satri ishlatiladi.
    """
    normalized = normalize_text(text)
    src = (source_lang or "auto").lower()
    payload = f"{model_version}:{src}:{target_lang.lower()}:{normalized}".encode("utf-8")
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

    def _model_version(self) -> str:
        return self._settings.GEMINI_MODEL

    async def get(
        self, target_lang: str, text: str, source_lang: str | None = None
    ) -> Optional[str]:
        """Kesh orqali tarjimani qidiradi. Topilmasa None qaytaradi."""
        if not self._settings.CACHE_ENABLED:
            return None

        key = build_cache_key(target_lang, text, source_lang, self._model_version())

        # 1-daraja: memory
        hit = self._memory.get(key)
        if hit is not None:
            await self._record_metric(hit=True)
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
                await self._record_metric(hit=True)
                return value

        await self._record_metric(hit=False)
        return None

    async def set(
        self,
        target_lang: str,
        text: str,
        translation: str,
        source_lang: str | None = None,
    ) -> None:
        """Tarjima natijasini ikkala darajaga ham yozadi."""
        if not self._settings.CACHE_ENABLED:
            return

        key = build_cache_key(target_lang, text, source_lang, self._model_version())
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
        self, target_lang: str, texts: list[str], source_lang: str | None = None
    ) -> dict[int, str]:
        """Bir nechta matn uchun keshdan topilganlarni index -> tarjima
        ko'rinishida qaytaradi.

        Barcha matnlar uchun kalitlar avval hisoblanadi, so'ng Redis'ga
        BITTA pipeline (MGET) so'rovi bilan murojaat qilinadi — har bir
        matn uchun alohida round-trip qilish o'rniga. Bu ko'p paragrafli
        hujjatlarda (masalan 40-500 paragraf) sezilarli tezlik beradi.
        """
        if not self._settings.CACHE_ENABLED or not texts:
            return {}

        model_version = self._model_version()
        keys = [
            build_cache_key(target_lang, text, source_lang, model_version)
            for text in texts
        ]

        results: dict[int, str] = {}
        redis_miss_indices: list[int] = []
        redis_miss_keys: list[str] = []

        # 1-daraja: memory (har biri arzon, in-process — loop yetarli)
        for idx, key in enumerate(keys):
            hit = self._memory.get(key)
            if hit is not None:
                results[idx] = hit
            else:
                redis_miss_indices.append(idx)
                redis_miss_keys.append(key)

        # 2-daraja: Redis — qolganlarni BITTA MGET so'rovida tekshiramiz
        if redis_miss_keys and self._redis is not None:
            redis_keys = [self._redis_key(k) for k in redis_miss_keys]
            try:
                raw_values = await self._redis.mget(redis_keys)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Redis MGET xatosi: %s", exc)
                raw_values = [None] * len(redis_keys)

            for pos, raw in enumerate(raw_values):
                if raw is None:
                    continue
                idx = redis_miss_indices[pos]
                value = json.loads(raw)["translation"]
                results[idx] = value
                # Redis'dan topilganini memory keshga ham yozib qo'yamiz
                self._memory.set(keys[idx], value)

        hits = len(results)
        misses = len(texts) - hits
        await self._record_metric(hit=True, count=hits)
        await self._record_metric(hit=False, count=misses)

        return results

    async def set_many(
        self,
        target_lang: str,
        items: list[tuple[str, str]],
        source_lang: str | None = None,
    ) -> None:
        """Bir nechta (manba matn, tarjima) juftligini pipeline orqali keshga yozadi."""
        if not self._settings.CACHE_ENABLED or not items:
            return

        model_version = self._model_version()
        for text, translation in items:
            self._memory.set(
                build_cache_key(target_lang, text, source_lang, model_version),
                translation,
            )

        if self._redis is None:
            return

        try:
            pipe = self._redis.pipeline(transaction=False)
            for text, translation in items:
                key = build_cache_key(target_lang, text, source_lang, model_version)
                payload = json.dumps({"translation": translation}, ensure_ascii=False)
                pipe.set(
                    self._redis_key(key),
                    payload,
                    ex=self._settings.CACHE_REDIS_TTL_SECONDS,
                )
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis pipeline SET xatosi: %s", exc)

    async def _record_metric(self, hit: bool, count: int = 1) -> None:
        """Kesh hit/miss sonini Redis counterlarida yuritadi (monitoring uchun).

        Metrika ixtiyoriy — Redis mavjud bo'lmasa yoki xato bo'lsa, jim
        o'tkazib yuboriladi, tarjima oqimiga ta'sir qilmaydi.
        """
        if count <= 0 or self._redis is None:
            return
        metric_key = "translation_cache:hits" if hit else "translation_cache:misses"
        try:
            await self._redis.incrby(metric_key, count)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Kesh metrikasini yozib bo'lmadi: %s", exc)

    async def get_metrics(self) -> dict[str, int]:
        """Joriy hit/miss sonlarini qaytaradi (masalan admin/monitoring endpoint uchun)."""
        if self._redis is None:
            return {"hits": 0, "misses": 0}
        try:
            hits_raw, misses_raw = await self._redis.mget(
                "translation_cache:hits", "translation_cache:misses"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kesh metrikasini o'qib bo'lmadi: %s", exc)
            return {"hits": 0, "misses": 0}
        return {
            "hits": int(hits_raw) if hits_raw else 0,
            "misses": int(misses_raw) if misses_raw else 0,
        }

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
