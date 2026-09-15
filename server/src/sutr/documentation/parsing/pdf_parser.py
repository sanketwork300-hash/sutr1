"""PDF, with OCR as the fallback for pages that hold no text.

Two strategies, tried in order, because neither is available everywhere:

1. **pypdf**, if installed (`uv sync --extra docs`). A library is preferable —
   no subprocess, no shell, per-page control.
2. **pdftotext** from poppler-utils, if on PATH. Widely present, and the
   `-layout` mode preserves the column structure that limits tables depend on.

If neither exists the parser says which to install rather than blaming the
file. A PDF is not broken because the host is missing a package.

**OCR.** A page that produces no text is usually a scan. LLD §3.5 fixes the
behaviour: *"OCR failure ⇒ flag pages, continue"* — the page is recorded as a
degradation and the rest of the document is processed. Tesseract is used when
present; when it is not, the pages are still flagged, so a document that is
half-scanned reports what was lost instead of silently losing it.
"""

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from sutr.documentation.parsing.base import (
    Element,
    ParseError,
    ParseResult,
    ParserUnavailableError,
)
from sutr.documentation.parsing.text import parse_text

PDFTOTEXT = "pdftotext"
TESSERACT = "tesseract"
PDFTOPPM = "pdftoppm"

# Poppler emits a form feed between pages.
_PAGE_BREAK = "\f"
_EXTRACT_TIMEOUT = 120
_OCR_TIMEOUT = 120
# OCR is slow and a large scanned document could occupy a worker for minutes.
MAX_OCR_PAGES = 20

INSTALL_HINT = (
    "Install one of: the pypdf library (uv sync --extra docs), or poppler-utils "
    "for the pdftotext command."
)


def available() -> tuple[bool, str | None]:
    """(usable, reason-if-not) — reported by /v1/platform/capabilities."""
    if _has_pypdf() or shutil.which(PDFTOTEXT):
        return True, None
    return False, f"No PDF text extractor is available. {INSTALL_HINT}"


def ocr_available() -> tuple[bool, str | None]:
    if not shutil.which(TESSERACT):
        return False, (
            "Tesseract is not installed, so pages that contain no text layer cannot be read. "
            "They are reported as unreadable rather than silently dropped."
        )
    if not shutil.which(PDFTOPPM):
        return False, "pdftoppm (poppler-utils) is needed to rasterise pages for OCR."
    return True, None


def _has_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def parse_pdf(data: bytes, *, allow_ocr: bool = True) -> ParseResult:
    """Extract a PDF's text, page by page."""
    if _has_pypdf():
        pages, parser = _pages_with_pypdf(data), "pypdf"
    elif shutil.which(PDFTOTEXT):
        pages, parser = _pages_with_pdftotext(data), "pdftotext"
    else:
        raise ParserUnavailableError("This deployment cannot read PDFs.", install_hint=INSTALL_HINT)

    result = ParseResult(parser=parser, page_count=len(pages))
    empty_pages = [index for index, text in enumerate(pages, start=1) if not text.strip()]

    if empty_pages and allow_ocr:
        recovered, ocr_degradations = _ocr_pages(data, empty_pages)
        for page_number, text in recovered.items():
            pages[page_number - 1] = text
        result.degradations += ocr_degradations
        empty_pages = [index for index, text in enumerate(pages, start=1) if not text.strip()]

    if empty_pages:
        result.degraded(
            "unreadable_pages",
            f"{len(empty_pages)} page(s) produced no text and could not be read. "
            "The rest of the document was processed.",
            pages=empty_pages,
        )

    elements: list[Element] = []
    text_parts: list[str] = []
    for page_number, page_text in enumerate(pages, start=1):
        if not page_text.strip():
            continue
        offset = sum(len(part) for part in text_parts)
        # A PDF page is just text; the Markdown parser's heuristics find its
        # headings and lists, which is exactly the structure the chunker needs.
        page_result = parse_text(page_text, markdown=False)
        for element in page_result.elements:
            element.page = page_number
            element.start_offset += offset
            element.end_offset += offset
            elements.append(element)
        text_parts.append(page_text + "\n\n")

    result.elements = elements
    result.text = "".join(text_parts)
    if result.is_empty and not result.degradations:
        result.degraded("no_text", "The PDF produced no readable text.")
    return result


def _pages_with_pypdf(data: bytes) -> list[str]:
    import io

    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ParseError("pdf_parse_error", f"The PDF could not be opened: {exc}")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            # One unreadable page must not lose the document.
            pages.append("")
    return pages


def _pages_with_pdftotext(data: bytes) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="sutr-pdf-") as directory:
        path = Path(directory) / "document.pdf"
        path.write_bytes(data)
        try:
            completed = subprocess.run(
                [PDFTOTEXT, "-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True,
                timeout=_EXTRACT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ParseError("pdf_timeout", "Reading the PDF timed out.")
        except OSError as exc:
            raise ParseError("pdf_parse_error", f"pdftotext could not be run: {exc}")
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
            raise ParseError("pdf_parse_error", f"pdftotext failed: {detail}")
    text = completed.stdout.decode("utf-8", errors="replace")
    return text.split(_PAGE_BREAK)


def _ocr_pages(data: bytes, pages: list[int]) -> tuple[dict[int, str], list[dict]]:
    """Rasterise and OCR the pages that produced no text.

    Returns what was recovered plus a degradation for anything that was not, so
    the caller can report the loss rather than discovering an empty document.
    """
    usable, reason = ocr_available()
    if not usable:
        return {}, [
            {
                "code": "ocr_unavailable",
                "message": reason,
                "pages": pages[:MAX_OCR_PAGES],
            }
        ]

    degradations: list[dict] = []
    attempted = pages[:MAX_OCR_PAGES]
    if len(pages) > MAX_OCR_PAGES:
        degradations.append(
            {
                "code": "ocr_page_limit",
                "message": (
                    f"{len(pages)} pages needed OCR; only the first {MAX_OCR_PAGES} were "
                    "attempted. The rest are reported as unreadable."
                ),
                "pages": pages[MAX_OCR_PAGES:],
            }
        )

    recovered: dict[int, str] = {}
    failed: list[int] = []
    with tempfile.TemporaryDirectory(prefix="sutr-ocr-") as directory:
        source = Path(directory) / "document.pdf"
        source.write_bytes(data)
        for page_number in attempted:
            try:
                image_prefix = Path(directory) / f"page-{page_number}"
                subprocess.run(
                    [
                        PDFTOPPM,
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        "-r",
                        "200",
                        "-png",
                        str(source),
                        str(image_prefix),
                    ],
                    capture_output=True,
                    timeout=_OCR_TIMEOUT,
                    check=True,
                )
                images = sorted(Path(directory).glob(f"page-{page_number}*.png"))
                if not images:
                    failed.append(page_number)
                    continue
                completed = subprocess.run(
                    [TESSERACT, str(images[0]), "stdout"],
                    capture_output=True,
                    timeout=_OCR_TIMEOUT,
                    check=True,
                )
                text = completed.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    recovered[page_number] = text
                else:
                    failed.append(page_number)
            except (subprocess.SubprocessError, OSError):
                # LLD §3.5: an OCR failure flags the page and continues.
                failed.append(page_number)

    if recovered:
        degradations.append(
            {
                "code": "ocr_recovered",
                "message": f"{len(recovered)} page(s) had no text layer and were read by OCR.",
                "pages": sorted(recovered),
            }
        )
    if failed:
        degradations.append(
            {
                "code": "ocr_failed",
                "message": f"OCR could not read {len(failed)} page(s).",
                "pages": failed,
            }
        )
    return recovered, degradations


async def parse_pdf_async(data: bytes, *, allow_ocr: bool = True) -> ParseResult:
    """Run the parse off the event loop.

    Both strategies are blocking, and OCR can take a minute; doing that on the
    event loop would stall every other request in the process.
    """
    return await asyncio.to_thread(parse_pdf, data, allow_ocr=allow_ocr)
