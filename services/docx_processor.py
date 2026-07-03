"""
DOCX hujjatlarini RUN darajasida tarjima qilish.

MUHIM ARXITEKTURAVIY QOIDA:
    python-docx'da `paragraph.text` faqat o'qish uchun qulay, lekin unga
    yozish (`paragraph.text = "..."`) BARCHA run'larni (bold, italic,
    rang, shrift kabi formatlashni) yo'qotib, bitta yagona run bilan
    almashtiradi. Natijada original hujjat formati butunlay buziladi.

    Shu sabab biz doim `run.text` darajasida ishlaymiz: har bir paragraf
    o'z run'lariga bo'linadi, faqat matn qismi tarjima qilinadi, format
    xususiyatlari (run.bold, run.italic, run.font, run.color va h.k.)
    umuman tegilmaydi.

    Muammoli holat: bitta jumla ko'pincha bir necha run'ga bo'linib
    ketadi (masalan, Word avtomatik imlo tekshiruvi tufayli). Agar har
    bir run'ni alohida tarjima qilsak, ma'no yo'qoladi (masalan "Hello"
    va " world" alohida-alohida tarjima qilinsa noto'g'ri natija chiqadi).
    Shu sababli biz avval paragraf run'larini "matn segmentlariga"
    yig'amiz, BUTUN paragraf matnini tarjima qilamiz, so'ngra tarjima
    qilingan matnni run'lar orasiga ULARNING NISBIY UZUNLIGIGA mos ravishda
    qayta taqsimlaymiz — shu orqali formatlash imkon qadar saqlanadi.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from .services.translator import TranslatorService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ParagraphRef:
    """Bitta paragrafga va uning joriy matniga havola (tarjima navbati uchun)."""

    paragraph: Paragraph
    original_text: str
    run_lengths: list[int] = field(default_factory=list)


def _iter_block_paragraphs(document: Document):
    """Hujjatdagi barcha paragraflarni (asosiy tana + jadval kataklari) yig'adi.

    Header/footer alohida qayta ishlanadi (docx_processor.translate_docx ichida).
    """
    yield from document.paragraphs

    for table in document.tables:
        yield from _iter_table_paragraphs(table)


def _iter_table_paragraphs(table: Table):
    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs
            for nested_table in cell.tables:
                yield from _iter_table_paragraphs(nested_table)


def _collect_translatable_paragraphs(document: Document) -> list[ParagraphRef]:
    """Tarjima qilinishi kerak bo'lgan paragraflarni yig'adi (bo'shlarni o'tkazib yuboradi)."""
    refs: list[ParagraphRef] = []
    for paragraph in _iter_block_paragraphs(document):
        text = paragraph.text
        if not text or not text.strip():
            continue
        run_lengths = [len(run.text) for run in paragraph.runs]
        # Agar run'lar umuman bo'lmasa (kamdan-kam, lekin nazariy jihatdan
        # mumkin), paragraph.text'ga tayanamiz va keyin bitta "virtual run"
        # sifatida ko'ramiz.
        if not run_lengths:
            run_lengths = [len(text)]
        refs.append(ParagraphRef(paragraph=paragraph, original_text=text, run_lengths=run_lengths))
    return refs


def _distribute_translation_to_runs(paragraph: Paragraph, translated_text: str) -> None:
    """Tarjima qilingan matnni paragraf run'lariga nisbiy uzunlik bo'yicha taqsimlaydi.

    Format (bold/italic/rang/shrift) run obyektining o'zida saqlanadi —
    biz faqat `run.text` maydonini o'zgartiramiz, run obyektini o'chirmaymiz.
    """
    runs: list[Run] = paragraph.runs
    if not runs:
        return

    if len(runs) == 1:
        runs[0].text = translated_text
        return

    original_lengths = [len(run.text) for run in runs]
    total_original = sum(original_lengths) or 1
    total_translated = len(translated_text)

    # Har bir run'ga tegishli nisbiy uzunlikni hisoblaymiz
    cursor = 0
    allocated_lengths: list[int] = []
    for i, orig_len in enumerate(original_lengths):
        if i == len(original_lengths) - 1:
            # Oxirgi run — qolgan hamma narsani oladi (yaxlitlash xatosini yopish uchun)
            allocated = total_translated - cursor
        else:
            ratio = orig_len / total_original
            allocated = round(total_translated * ratio)
        allocated = max(allocated, 0)
        allocated_lengths.append(allocated)
        cursor += allocated

    # Ehtiyot chorasi: agar yaxlitlash tufayli umumiy uzunlik oshib/kamayib
    # ketsa, oxirgi elementni tuzatamiz.
    diff = total_translated - sum(allocated_lengths)
    if diff != 0 and allocated_lengths:
        allocated_lengths[-1] = max(allocated_lengths[-1] + diff, 0)

    pos = 0
    for run, length in zip(runs, allocated_lengths):
        run.text = translated_text[pos : pos + length]
        pos += length


def _translate_header_footer(
    document: Document, translations_map: dict[str, str]
) -> None:
    """Header/footer paragraflarini translations_map orqali tarjima qiladi.

    (translations_map allaqachon tarjima qilingan {original_text: translated_text}
    lug'ati — bu funksiya faqat qo'llaydi, yangi API so'rovi qilmaydi.)
    """
    for section in document.sections:
        for container in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            if container is None:
                continue
            for paragraph in container.paragraphs:
                text = paragraph.text
                if text and text.strip() and text in translations_map:
                    _distribute_translation_to_runs(paragraph, translations_map[text])


async def translate_docx(
    input_path: Path,
    output_path: Path,
    target_language: str,
    translator: TranslatorService,
    source_language: str | None = None,
) -> Path:
    """DOCX faylini run-darajasida tarjima qilib, yangi faylga saqlaydi.

    Args:
        input_path: manba .docx fayli.
        output_path: tarjima qilingan .docx fayl saqlanadigan joy.
        target_language: maqsad til kodi.
        translator: TranslatorService instansi (provider + cache).
        source_language: agar oldindan aniqlangan bo'lsa, manba til kodi.

    Returns:
        output_path (qulaylik uchun).
    """
    document = Document(str(input_path))

    body_refs = _collect_translatable_paragraphs(document)
    unique_texts = list({ref.original_text for ref in body_refs})

    logger.info(
        "DOCX tarjima boshlandi: %s ta noyob paragraf (%s ta jami)",
        len(unique_texts),
        len(body_refs),
    )

    translated_list = await translator.translate_paragraphs(
        paragraphs=unique_texts,
        target_language=target_language,
        source_language=source_language,
    )
    translations_map: dict[str, str] = dict(zip(unique_texts, translated_list))

    for ref in body_refs:
        translated = translations_map.get(ref.original_text)
        if translated is None:
            continue
        _distribute_translation_to_runs(ref.paragraph, translated)

    # Header/footer'larni ham xuddi shu tarjima lug'atidan foydalanib qayta ishlaymiz.
    # Ularni alohida to'plab, kesh-miss bo'lganlarni qo'shimcha tarjima qilamiz.
    header_footer_texts: set[str] = set()
    for section in document.sections:
        for container in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            if container is None:
                continue
            for paragraph in container.paragraphs:
                if paragraph.text and paragraph.text.strip():
                    header_footer_texts.add(paragraph.text)

    missing_hf_texts = [t for t in header_footer_texts if t not in translations_map]
    if missing_hf_texts:
        hf_translated = await translator.translate_paragraphs(
            paragraphs=missing_hf_texts,
            target_language=target_language,
            source_language=source_language,
        )
        translations_map.update(dict(zip(missing_hf_texts, hf_translated)))

    _translate_header_footer(document, translations_map)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))
    logger.info("DOCX tarjima yakunlandi: %s", output_path)
    return output_path
