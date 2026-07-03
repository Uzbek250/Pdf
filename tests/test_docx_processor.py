"""
Test 2: DOCX run-darajasidagi tarjima taqsimoti — formatlash (bold/italic)
run obyektlarida saqlanib qolishini tekshiradi.
"""
from __future__ import annotations

from docx import Document

from .services.docx_processor import (
    _collect_translatable_paragraphs,
    _distribute_translation_to_runs,
)


def _make_multi_run_paragraph():
    """Bitta paragrafda 2 ta run (bold va oddiy) yaratadi."""
    document = Document()
    paragraph = document.add_paragraph()
    run1 = paragraph.add_run("Hello ")
    run1.bold = True
    run2 = paragraph.add_run("world")
    run2.italic = True
    return document, paragraph, run1, run2


def test_distribute_translation_preserves_run_formatting_flags() -> None:
    """Tarjimadan keyin run.bold / run.italic o'zgarishsiz qolishi kerak."""
    document, paragraph, run1, run2 = _make_multi_run_paragraph()
    assert paragraph.text == "Hello world"

    _distribute_translation_to_runs(paragraph, "Salom dunyo")

    # Format bayroqlari saqlanishi kerak (run obyektlari o'zgarmagan)
    assert paragraph.runs[0].bold is True
    assert paragraph.runs[1].italic is True

    # Matn to'liq va to'g'ri taqsimlangan (ikkala run yig'indisi)
    combined = "".join(run.text for run in paragraph.runs)
    assert combined == "Salom dunyo"


def test_distribute_translation_single_run_replaces_text_directly() -> None:
    """Faqat bitta run bo'lganda, matn to'g'ridan-to'g'ri almashtiriladi."""
    document = Document()
    paragraph = document.add_paragraph()
    run = paragraph.add_run("Original matn")
    run.font.size = None  # placeholder, format tekshirilmaydi bu yerda

    _distribute_translation_to_runs(paragraph, "Tarjima qilingan matn")

    assert len(paragraph.runs) == 1
    assert paragraph.runs[0].text == "Tarjima qilingan matn"


def test_distribute_translation_total_length_matches_translated_text() -> None:
    """Ko'p run holatida ham umumiy uzunlik tarjima matni uzunligiga teng bo'lishi kerak
    (yaxlitlash xatolari to'g'rilanganini tekshiradi)."""
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("A")  # juda qisqa run
    paragraph.add_run("BB")
    paragraph.add_run("CCC")

    translated = "Bu ancha uzunroq tarjima qilingan matn bo'lishi mumkin"
    _distribute_translation_to_runs(paragraph, translated)

    combined = "".join(run.text for run in paragraph.runs)
    assert combined == translated
    assert len(combined) == len(translated)


def test_collect_translatable_paragraphs_skips_empty_paragraphs() -> None:
    """Bo'sh paragraflar tarjima ro'yxatiga kiritilmasligi kerak."""
    document = Document()
    document.add_paragraph("Birinchi paragraf")
    document.add_paragraph("")  # bo'sh
    document.add_paragraph("   ")  # faqat bo'shliq
    document.add_paragraph("Ikkinchi paragraf")

    refs = _collect_translatable_paragraphs(document)
    texts = [ref.original_text for ref in refs]

    assert texts == ["Birinchi paragraf", "Ikkinchi paragraf"]
