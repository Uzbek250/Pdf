"""
Fayl konvertatsiya xizmatlari:
    - PDF -> DOCX (pdf2docx orqali, layout imkon qadar saqlanadi)
    - DOCX -> PDF (LibreOffice headless orqali)
    - Skaner (rasm asosidagi) PDF'ni aniqlash — matn qatlami bo'lmasa OCR
      yo'liga yo'naltirish uchun.

Bu modul faqat fayl format transformatsiyalari bilan shug'ullanadi;
tarjima mantiqi bu yerda YO'Q (u services/translator.py va
services/docx_processor.py'da).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import pypdf
from pdf2docx import Converter as Pdf2DocxConverter

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class ConversionError(Exception):
    """Konvertatsiya jarayonida yuz beradigan xatoliklar uchun."""


# ---------------------------------------------------------------------- #
# Skaner PDF aniqlash
# ---------------------------------------------------------------------- #
def is_scanned_pdf(pdf_path: Path, settings: Settings | None = None) -> bool:
    """PDF asosan rasm(lar)dan iboratmi (skaner qilingan) tekshiradi.

    Har bir sahifadan matn qatlamini o'qishga harakat qilamiz. Agar
    o'rtacha sahifada `SCANNED_PDF_MIN_CHARS_PER_PAGE` dan kam belgi
    topilsa, hujjat skaner (faqat rasm) deb hisoblanadi va OCR yo'liga
    yuboriladi.
    """
    settings = settings or get_settings()
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"PDF o'qib bo'lmadi: {exc}") from exc

    if len(reader.pages) == 0:
        return False

    total_chars = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - ba'zi buzuq PDF sahifalari xato berishi mumkin
            text = ""
        total_chars += len(text.strip())

    avg_chars_per_page = total_chars / len(reader.pages)
    logger.debug(
        "PDF %s: sahifa boshiga o'rtacha %.1f belgi (chegara=%s)",
        pdf_path.name,
        avg_chars_per_page,
        settings.SCANNED_PDF_MIN_CHARS_PER_PAGE,
    )
    return avg_chars_per_page < settings.SCANNED_PDF_MIN_CHARS_PER_PAGE


def get_pdf_page_count(pdf_path: Path) -> int:
    """PDF'dagi sahifalar sonini qaytaradi."""
    reader = pypdf.PdfReader(str(pdf_path))
    return len(reader.pages)


# ---------------------------------------------------------------------- #
# PDF -> DOCX
# ---------------------------------------------------------------------- #
def _pdf_to_docx_sync(pdf_path: Path, docx_path: Path) -> None:
    """pdf2docx orqali sinxron konvertatsiya (blocking, thread'da ishga tushiriladi)."""
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    converter = Pdf2DocxConverter(str(pdf_path))
    try:
        converter.convert(str(docx_path), start=0, end=None)
    finally:
        converter.close()


async def pdf_to_docx(pdf_path: Path, docx_path: Path) -> Path:
    """PDF faylni DOCX'ga aylantiradi (layout imkon qadar saqlanadi).

    pdf2docx CPU-bog'liq va blocking kutubxona bo'lgani uchun uni
    thread pool'da ishga tushiramiz, shunda event loop bloklanmaydi.
    """
    logger.info("PDF -> DOCX konvertatsiya boshlandi: %s", pdf_path.name)
    try:
        await asyncio.to_thread(_pdf_to_docx_sync, pdf_path, docx_path)
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"PDF->DOCX konvertatsiya muvaffaqiyatsiz: {exc}") from exc
    logger.info("PDF -> DOCX konvertatsiya tugadi: %s", docx_path.name)
    return docx_path


# ---------------------------------------------------------------------- #
# DOCX -> PDF (LibreOffice)
# ---------------------------------------------------------------------- #
def _run_libreoffice_convert(
    docx_path: Path, output_dir: Path, settings: Settings
) -> Path:
    """LibreOffice'ni subprocess orqali headless rejimda ishga tushiradi."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.LIBREOFFICE_BINARY,
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        str(docx_path),
    ]
    logger.info("LibreOffice buyrug'i ishga tushirilmoqda: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.LIBREOFFICE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(
            f"LibreOffice belgilangan {settings.LIBREOFFICE_TIMEOUT_SECONDS}s "
            f"vaqtida yakunlanmadi."
        ) from exc

    if result.returncode != 0:
        raise ConversionError(
            f"LibreOffice xato bilan yakunlandi (kod={result.returncode}): "
            f"{result.stderr or result.stdout}"
        )

    expected_pdf = output_dir / f"{docx_path.stem}.pdf"
    if not expected_pdf.exists():
        raise ConversionError(
            f"LibreOffice PDF fayl yaratmadi (kutilgan: {expected_pdf})."
        )
    return expected_pdf


async def docx_to_pdf(
    docx_path: Path, output_dir: Path, settings: Settings | None = None
) -> Path:
    """DOCX faylni PDF'ga aylantiradi (LibreOffice headless orqali).

    LibreOffice chiqish fayl nomini avtomatik `{stem}.pdf` deb belgilaydi,
    shu sababli chaqiruvchi kod kerak bo'lsa keyinchalik faylni ko'chirishi
    yoki qayta nomlashi mumkin.
    """
    settings = settings or get_settings()
    logger.info("DOCX -> PDF konvertatsiya boshlandi: %s", docx_path.name)
    try:
        result_path = await asyncio.to_thread(
            _run_libreoffice_convert, docx_path, output_dir, settings
        )
    except ConversionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"DOCX->PDF konvertatsiya muvaffaqiyatsiz: {exc}") from exc
    logger.info("DOCX -> PDF konvertatsiya tugadi: %s", result_path.name)
    return result_path


# ---------------------------------------------------------------------- #
# Skaner PDF sahifalarini rasmga aylantirish (OCR uchun)
# ---------------------------------------------------------------------- #
def _render_pdf_pages_to_png_sync(pdf_path: Path, output_dir: Path, dpi: int = 200) -> list[Path]:
    """pypdfium2 orqali har bir PDF sahifasini PNG rasmga renderlaydi."""
    import pypdfium2 as pdfium

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        scale = dpi / 72.0
        for i, page in enumerate(pdf):
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
            out_path = output_dir / f"page_{i + 1:04d}.png"
            pil_image.save(out_path, format="PNG")
            paths.append(out_path)
    finally:
        pdf.close()
    return paths


async def render_pdf_pages_to_images(
    pdf_path: Path, output_dir: Path, dpi: int = 200
) -> list[Path]:
    """PDF sahifalarini PNG rasmlarga aylantiradi (skaner PDF OCR oqimi uchun)."""
    try:
        return await asyncio.to_thread(
            _render_pdf_pages_to_png_sync, pdf_path, output_dir, dpi
        )
    except Exception as exc:  # noqa: BLE001
        raise ConversionError(f"PDF sahifalarini rasmga aylantirib bo'lmadi: {exc}") from exc


def images_to_pdf(image_paths: list[Path], output_path: Path) -> Path:
    """Rasmlar ro'yxatini bitta PDF faylga birlashtiradi (skaner tarjima natijasi uchun)."""
    import img2pdf

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert([str(p) for p in image_paths]))
    return output_path
