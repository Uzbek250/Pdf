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
    qilingan matnni run'lar orasiga so'z chegaralarini hurmat qilgan
    holda qayta taqsimlaymiz — shu orqali formatlash imkon qadar
    saqlanadi va so'zlar o'rtasidagi bo'shliqlar yo'qolmaydi.

MUHIM: RASMLI RUN'LAR
    Bitta `Run` obyekti nafaqat oddiy matn, balki inline rasm
    (`<w:drawing>` yoki eski uslub `<w:pict>` XML elementi) ham bo'lishi
    mumkin. Agar bunday run'ning `.text` xususiyatiga yozsak,
    python-docx uning butun XML mazmunini (jumladan rasm elementini ham)
    o'chirib, faqat oddiy matn bilan almashtiradi — bu rasmni butunlay
    yo'q qilib yuboradi. Shu sabab biz har bir run'ni tarjima matni bilan
    to'ldirishdan oldin, u rasm saqlovchi run emasligini tekshiramiz va
    rasmli run'larni umuman tegmasdan qoldiramiz.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from services.translator import TranslatorService

logger = logging.getLogger(__name__)

# Rasm yoki boshqa grafik obyektlarni ifodalovchi XML teglar (namespace
# prefiksidan qat'iy nazar, shuning uchun `endswith` orqali tekshiramiz).
_DRAWING_TAG_SUFFIXES = ("}drawing", "}pict", "}object")


@dataclass(slots=True)
class ParagraphRef:
    """Bitta paragrafga va uning joriy matniga havola (tarjima navbati uchun)."""

    paragraph: Paragraph
    original_text: str
    run_lengths: list[int] = field(default_factory=list)


def _run_contains_drawing(run: Run) -> bool:
    """Berilgan run ichida rasm/chizma (drawing) elementi bormi tekshiradi.

    Bunday run'ning `.text` xususiyatiga yozish uning ichidagi rasmni
    yo'q qilib yuboradi, shuning uchun bunday run'larni tarjima matnini
    taqsimlashda umuman chetlab o'tamiz.
    """
    for child in run._element.iter():
        if any(child.tag.endswith(suffix) for suffix in _DRAWING_TAG_SUFFIXES):
            return True
    return False


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


def _split_translated_text_by_word_boundaries(
    translated_text: str, num_targets: int
) -> list[str]:
    """Tarjima matnini so'z chegaralarini hurmat qilgan holda `num_targets`
    ta segmentga bo'ladi.

    Oddiy nisbiy-uzunlik bo'yicha kesish so'z o'rtasidan kesib, bo'shliqni
    yo'qotib yuborishi mumkin (masalan "Matnning yangi" -> "Matnningyangi").
    Bu funksiya avval matnni so'zlarga (bo'shliqni saqlagan holda) ajratadi,
    so'ng har bir segmentga nisbatan teng miqdorda so'z guruhini beradi —
    shunda bo'shliqlar hech qachon segment ichida yo'qolmaydi.
    """
    if num_targets <= 1:
        return [translated_text]

    # Bo'shliqlarni ham alohida "token" sifatida saqlab, matnni bo'lakларга
    # ajratamiz — shunda qayta birlashtirilganda hech narsa yo'qolmaydi.
    tokens = re.findall(r"\S+|\s+", translated_text)
    if not tokens:
        return [""] * num_targets

    total_len = len(translated_text)
    target_len_per_segment = total_len / num_targets

    segments: list[str] = []
    current_segment: list[str] = []
    current_len = 0
    remaining_targets = num_targets

    for idx, token in enumerate(tokens):
        current_segment.append(token)
        current_len += len(token)

        remaining_tokens = tokens[idx + 1 :]
        is_last_target = remaining_targets == 1

        if is_last_target:
            # Oxirgi segment — qolgan barcha tokenlarni oladi.
            continue

        if current_len >= target_len_per_segment and not token.isspace():
            # So'z chegarasida (bo'shliq emas) to'xtaymiz, shunda so'z
            # o'rtasidan bo'linmaydi.
            segments.append("".join(current_segment))
            current_segment = []
            current_len = 0
            remaining_targets -= 1
            if not remaining_tokens:
                break

    # Qolgan tokenlarni oxirgi segmentga qo'shamiz.
    if current_segment:
        segments.append("".join(current_segment))

    # Yetarli segment hosil bo'lmagan bo'lsa (juda qisqa matn holatida),
    # bo'sh segmentlar bilan to'ldiramiz.
    while len(segments) < num_targets:
        segments.append("")

    # Agar biror sababdan ortiqcha segment hosil bo'lsa (kamdan-kam holat),
    # ortiqchalarini oxirgisiga birlashtiramiz.
    if len(segments) > num_targets:
        extra = "".join(segments[num_targets - 1 :])
        segments = segments[: num_targets - 1] + [extra]

    return segments


def _distribute_translation_to_runs(paragraph: Paragraph, translated_text: str) -> None:
    """Tarjima qilingan matnni paragraf run'lariga so'z chegaralarini hurmat
    qilgan holda taqsimlaydi.

    Format (bold/italic/rang/shrift) run obyektining o'zida saqlanadi —
    biz faqat `run.text` maydonini o'zgartiramiz, run obyektini o'chirmaymiz.

    RASMLI RUN'LAR TEGILMAYDI: agar biror run ichida rasm (drawing/pict
    elementi) bo'lsa, uning matni umuman o'zgartirilmaydi — aks holda
    python-docx run matnini yozayotganda run ichidagi rasm elementini ham
    o'chirib yuboradi.
    """
    all_runs: list[Run] = paragraph.runs
    if not all_runs:
        return

    # Rasm saqlovchi run'larni ajratib olamiz — ularga umuman tegilmaydi.
    text_runs = [run for run in all_runs if not _run_contains_drawing(run)]

    if not text_runs:
        # Paragrafda faqat rasm(lar) bor, matn run'i yo'q — tegilmaydi.
        return

    if len(text_runs) == 1:
        text_runs[0].text = translated_text
        return

    segments = _split_translated_text_by_word_boundaries(translated_text, len(text_runs))
    for run, segment in zip(text_runs, segments):
        run.text = segment


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
