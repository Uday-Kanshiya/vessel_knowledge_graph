"""
OCR for scanned PDF pages (02 §2.3: "if thin, treat as scanned → OCR").

KING ONE showed this is not a corner case: 19 of its 30 PDFs — including the
service report and all three SOFs that carry the repair chain — have no text
layer. Pages are rendered with PyMuPDF and transcribed by one of two backends:

    vision     the configured chat model, one call per page, asked to transcribe
               verbatim (default: works with the LLM key already in .env)
    tesseract  pytesseract + a tesseract binary, when installed

The transcription becomes the document's `extracted_text` and the digest is
taken from it, so evidence quotes from a scanned SOF are checked against the
same text the model read. Transcription is capped at PDF_OCR_MAX_PAGES per
file; the remaining pages are noted, not silently dropped.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Optional, Sequence

from src.config import (
    EXTRACTION_OCR_MODEL,
    OCR_TRANSCRIBE_PROMPT_VERSION,
    PDF_OCR_BACKEND,
    PDF_OCR_DPI,
    PDF_OCR_MAX_PAGES,
    get_prompt,
)
from src.llm import LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

BACKENDS = ("off", "vision", "tesseract")


def render_pdf_pages(data: bytes, page_indexes: Sequence[int], dpi: int = PDF_OCR_DPI) -> dict[int, bytes]:
    """PNG bytes per requested page index. Empty dict when PyMuPDF is missing or the file will not open."""
    try:
        import fitz
    except ImportError:
        logger.warning("OCR needs pymupdf to render pages; not installed")
        return {}
    out: dict[int, bytes] = {}
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            for i in page_indexes:
                if 0 <= i < len(doc):
                    pix = doc[i].get_pixmap(dpi=dpi)
                    out[i] = pix.tobytes("png")
    except Exception as e:
        logger.warning("page render failed: %s", e)
    return out


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.S)
    return m.group(1).strip() if m else text


class PdfOcr:
    def __init__(
        self,
        llm=None,
        backend: str = PDF_OCR_BACKEND,
        max_pages: int = PDF_OCR_MAX_PAGES,
        dpi: int = PDF_OCR_DPI,
        model: Optional[str] = EXTRACTION_OCR_MODEL or None,
    ):
        if backend not in BACKENDS:
            raise ValueError(f"OCR backend must be one of {BACKENDS}, got {backend!r}")
        if backend == "vision" and llm is None:
            raise ValueError("vision OCR needs an LLM client")
        self.llm = llm
        self.backend = backend
        self.max_pages = max(1, max_pages)
        self.dpi = dpi
        self.model = model
        self.prompt = get_prompt("ocr_transcribe", OCR_TRANSCRIBE_PROMPT_VERSION) if backend == "vision" else ""
        self.stats = {"pages": 0, "calls": 0, "failed_pages": 0}

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    async def ocr(self, data: bytes, page_indexes: Sequence[int], total_pages: int, tracker: Optional[TokenTracker], label: str = "") -> tuple[dict[int, str], str]:
        """({page_index: text}, note). Pages beyond max_pages are left out and named in the note."""
        if not self.enabled or not page_indexes:
            return {}, "OCR off"
        wanted = list(page_indexes)[: self.max_pages]
        skipped = list(page_indexes)[self.max_pages:]
        images = render_pdf_pages(data, wanted, self.dpi)
        if not images:
            return {}, "OCR: pages could not be rendered"
        texts: dict[int, str] = {}
        if self.backend == "tesseract":
            texts = self._tesseract(images)
        else:
            for i in wanted:
                if i not in images:
                    continue
                try:
                    texts[i] = await self._vision_page(images[i], i, total_pages, tracker)
                except LLMError as e:
                    self.stats["failed_pages"] += 1
                    logger.warning("OCR page %d of %s failed: %s", i + 1, label, e)
        self.stats["pages"] += len(texts)
        note = f"OCR ({self.backend}) on {len(texts)}/{len(page_indexes)} scanned pages"
        if skipped:
            note += f"; {len(skipped)} pages beyond PDF_OCR_MAX_PAGES={self.max_pages} not transcribed"
        return texts, note

    async def _vision_page(self, png: bytes, index: int, total: int, tracker: Optional[TokenTracker]) -> str:
        b64 = base64.b64encode(png).decode("ascii")
        content = [
            {"type": "text", "text": f"Page {index + 1} of {total}. Transcribe every piece of text on this page."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
        ]
        kwargs = {"model": self.model} if self.model else {}
        self.stats["calls"] += 1
        resp = await self.llm.chat_completion(
            messages=[{"role": "user", "content": content}],
            system_message=self.prompt,
            token_tracker=tracker,
            **kwargs,
        )
        return _strip_fences(resp["content"] or "")

    def _tesseract(self, images: dict[int, bytes]) -> dict[int, str]:
        try:
            import io

            import pytesseract
            from PIL import Image
        except ImportError:
            logger.warning("tesseract backend needs pytesseract and Pillow")
            return {}
        out: dict[int, str] = {}
        for i, png in images.items():
            try:
                with Image.open(io.BytesIO(png)) as im:
                    out[i] = pytesseract.image_to_string(im, lang="eng+chi_sim").strip()
            except Exception as e:
                self.stats["failed_pages"] += 1
                logger.warning("tesseract failed on page %d: %s", i + 1, e)
        return out
