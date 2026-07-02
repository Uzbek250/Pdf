# Document Translator API

PDF va DOCX fayllarni **original formatini saqlab** tarjima qiluvchi FastAPI backend.
Provider: **Google Gemini** (`gemini-2.5-flash`, free tier, rasmiy `google-genai` SDK).

## Arxitektura

```
PDF  → pdf2docx → DOCX → run-level tarjima → LibreOffice → PDF
DOCX →                 → run-level tarjima →             → DOCX
Skaner PDF → sahifalarni rasmga aylantirish → Gemini vision (OCR+tarjima)
```

- **Provider-agnostic**: `providers/base.py`dagi `TranslationProvider` ABC orqali.
  Gemini API o'zgarsa yoki boshqa provayderga o'tish kerak bo'lsa, faqat
  `providers/gemini_provider.py` o'zgaradi.
- **3 darajali kesh**: LRU memory → Redis → Gemini API (`cache/translation_cache.py`).
- **Run-level DOCX tarjima**: `python-docx`da `paragraph.text` emas, har bir
  `run.text` bilan ishlaydi — bold/italic/rang/shrift saqlanib qoladi
  (`services/docx_processor.py`).
- **Avtomatik til aniqlash**: `langdetect` (tez, bepul) + ishonch past bo'lsa
  Gemini fallback.

## O'rnatish

```bash
cd app
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env faylida GEMINI_API_KEY ni to'ldiring (https://aistudio.google.com/apikey)
```

Tizimda **LibreOffice** va **Redis** o'rnatilgan bo'lishi kerak:

```bash
sudo apt-get install libreoffice redis-server
```

## Ishga tushirish

```bash
# Terminal 1: Redis (agar servis sifatida ishlamasa)
redis-server

# Terminal 2: Celery worker
celery -A app.workers.celery_tasks.celery_app worker --loglevel=info

# Terminal 3: FastAPI server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API hujjatlari: http://localhost:8000/docs

## Endpointlar

| Method | Path                       | Tavsif                              |
|--------|-----------------------------|--------------------------------------|
| POST   | `/api/translate`            | Fayl yuklash + tarjima navbatga qo'yish |
| GET    | `/api/progress/{task_id}`   | SSE orqali progress kuzatish         |
| GET    | `/api/download/{task_id}`   | Tayyor faylni yuklab olish            |
| GET    | `/api/languages`             | Qo'llab-quvvatlanadigan tillar       |
| POST   | `/api/detect-lang`           | Matn namunasi tilini aniqlash        |

## Testlash

```bash
cd app
python3 -m pytest tests/ -v
```

14 ta test: kesh (kalit generatsiyasi, LRU eviction), DOCX run-level
formatlashni saqlash, TranslatorService orkestratsiyasi (batching,
kesh-hit/miss, auto-detect fallback) — barchasi mock provider bilan,
haqiqiy API chaqiruvisiz.

## Muhim texnik qarorlar

1. **`run.text` darajasida ishlash**: `paragraph.text = "..."` yozish
   barcha run'larni yo'q qilib, formatni buzadi. Shu sabab paragraf
   run'lariga bo'linadi, butun paragraf matni tarjima qilinadi, so'ng
   natija run'lar orasiga nisbiy uzunlik bo'yicha qayta taqsimlanadi.

2. **Cache kaliti**: `SHA256(target_lang + normalized_text)[:16]` —
   bo'shliqlar normalizatsiya qilinadi, shunda "Salom  dunyo" va
   "Salom dunyo" bir xil kalitga tushadi.

3. **Auto-detect optimallashtirish**: `langdetect.prob > 0.95` bo'lsa,
   Gemini API chaqirilmaydi — bu bepul tier limitini tejaydi.

4. **Batch tarjima**: 20 paragraf bitta so'rovda JSON massiv sifatida
   yuboriladi/qaytariladi — API chaqiruvlar sonini keskin kamaytiradi.

5. **Rate limit handling**: Gemini 429 qaytarsa, exponential backoff +
   jitter bilan qayta uriniladi (`GEMINI_MAX_RETRIES` marta).
