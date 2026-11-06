"""
Tier 2 typed readers (02 §2.3, 04 §4): bytes -> text, by file family.

Every reader returns a ReadResult with the text it got and the
`extraction_method` the digest records (schema/attachment_digest.v1). Readers
degrade, never fail the window: a missing library or an unreadable file gives
`listed_only` with the reason, and the digest says so.

Routing is by BYTES first (sniff_family), then by name: the server labels
forwarded `.eml` files as PDF (04 §1), and KING ONE had three of them.

    pdf     PyMuPDF text layer (pypdf fallback). Pages with fewer than
            PDF_OCR_PAGE_THRESHOLD chars are reported in meta so digest.py can
            OCR them (src/attachments/ocr.py); a fully scanned PDF comes back
            listed_only here and is upgraded to pdf_ocr there.
    word    python-docx (.docx). .doc needs LibreOffice: listed_only.
    sheet   openpyxl (.xlsx/.xlsm) -> markdown-ish rows, visible sheets, row cap.
    eml     stdlib email: headers + text body + attachment names (not recursed).
    archive zipfile/tarfile member listing only — never opened (02 §2.3).
    image   vision is a model call, done in digest.py; here it is listed_only.

Identity documents (passports, seaman's books, visas, certificates) are
classified and never read — the rule is applied on the name before reading
and on the text after OCR (is_identity_document).

READER_VERSION is stamped on every digest's audit; a cached listed_only
digest from an older reader is re-read on the next run.
"""

from __future__ import annotations

import email
import email.policy
import io
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from src.config import PDF_OCR_PAGE_THRESHOLD

READER_VERSION = 2
SHEET_ROW_CAP = 200
SHEET_COL_CAP = 30

_EMAIL_HEADER_RX = re.compile(
    rb"^(?:Received|Return-Path|Delivered-To|From|To|Subject|Date|Message-ID|MIME-Version|Content-Type|X-[A-Za-z-]+):",
    re.I | re.M,
)
_IDENTITY_NAME_RX = re.compile(
    r"passport|seaman|seafarer|(?<![a-z])visa(?![a-z])|(?<![a-z])cdc(?![a-z])|id[ _-]?card|identity|(?<![a-z])coc(?![a-z])|certificate of competen|medical cert|vaccinat|"
    r"yellow fever|\bpp\b|\bdob\b",
    re.I,
)
_IDENTITY_TEXT_RX = re.compile(
    r"\bPASSPORT\b|P<[A-Z]{3}[A-Z<]{5,}|\bSEAMAN'?S? BOOK\b|\bSEAFARER'?S? IDENTITY\b|\bDate of birth\b.{0,40}\bNationality\b",
    re.I | re.S,
)


@dataclass
class ReadResult:
    text: Optional[str]
    method: str  # pdf_text | pdf_ocr | docx_text | xlsx_table | eml_parse | listed_only | vision | skipped
    note: str = ""  # why listed_only, page counts, member counts, ...
    meta: dict = field(default_factory=dict)


def sniff_family(name: str, declared: str, data: bytes) -> str:
    """The family the BYTES say the file is; falls back to the declared (name-based) family."""
    head = data[:2048]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"{\\rtf"):
        return "word"
    if head.startswith(b"PK\x03\x04"):
        # docx/xlsx are zips too; trust the name for those, else it is an archive.
        return declared if declared in ("word", "sheet") else "archive"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2: .doc/.xls/.msg
        return declared if declared in ("word", "sheet", "eml") else "word"
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"RIFF")):
        return "image"
    if len(_EMAIL_HEADER_RX.findall(head)) >= 2:
        return "eml"
    if declared == "pdf":
        # Declared PDF that is none of the above: a mislabelled text/eml file.
        return "eml" if _EMAIL_HEADER_RX.search(head) else "other"
    return declared


def is_identity_document(name: str, text: Optional[str] = None) -> bool:
    if _IDENTITY_NAME_RX.search(name or ""):
        return True
    if text and _IDENTITY_TEXT_RX.search(text[:4000]):
        return True
    return False


def read_pdf(data: bytes) -> ReadResult:
    pages_text: list[str] = []
    try:
        import fitz  # PyMuPDF

        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                pages_text.append(page.get_text("text"))
    except ImportError:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            for page in reader.pages:
                pages_text.append(page.extract_text() or "")
        except ImportError:
            return ReadResult(None, "listed_only", "no PDF library installed (pymupdf or pypdf)")
        except Exception as e:  # encrypted, malformed
            return ReadResult(None, "listed_only", f"pdf unreadable: {e}")
    except Exception as e:
        return ReadResult(None, "listed_only", f"pdf unreadable: {e}")

    n = len(pages_text)
    thin = [i for i, t in enumerate(pages_text) if len(t.strip()) < PDF_OCR_PAGE_THRESHOLD]
    meta = {"page_count": n, "thin_pages": len(thin), "thin_page_indexes": thin, "pages_text": [t.strip() for t in pages_text]}
    text = join_pages(meta["pages_text"])
    if not text.strip():
        return ReadResult(None, "listed_only", f"scanned PDF, {n} pages, no text layer", meta)
    note = f"{n} pages" + (f", {len(thin)} without a text layer" if thin else "")
    return ReadResult(text, "pdf_text", note, meta)


def join_pages(pages_text: list[str]) -> str:
    return "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pages_text) if t and t.strip())


def read_docx(data: bytes) -> ReadResult:
    try:
        import docx  # python-docx
    except ImportError:
        return ReadResult(None, "listed_only", "python-docx not installed")
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as e:
        return ReadResult(None, "listed_only", f"docx unreadable: {e}")
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    if not text.strip():
        return ReadResult(None, "listed_only", "docx has no text (images only?)")
    return ReadResult(text, "docx_text", f"{len(document.paragraphs)} paragraphs, {len(document.tables)} tables")


def read_xlsx(data: bytes) -> ReadResult:
    try:
        import openpyxl
    except ImportError:
        return ReadResult(None, "listed_only", "openpyxl not installed")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        return ReadResult(None, "listed_only", f"xlsx unreadable: {e}")
    out: list[str] = []
    sheets = 0
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        sheets += 1
        out.append(f"[sheet {ws.title}]")
        rows = 0
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in row[:SHEET_COL_CAP]]
            if not any(cells):
                continue
            while cells and cells[-1] == "":
                cells.pop()
            out.append(" | ".join(cells))
            rows += 1
            if rows >= SHEET_ROW_CAP:
                out.append(f"[... sheet cut at {SHEET_ROW_CAP} rows]")
                break
    text = "\n".join(out)
    if sheets == 0 or not any(ln and not ln.startswith("[") for ln in out):
        return ReadResult(None, "listed_only", "workbook has no visible non-empty cells")
    return ReadResult(text, "xlsx_table", f"{sheets} visible sheets")


def read_eml(data: bytes) -> ReadResult:
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception as e:
        return ReadResult(None, "listed_only", f"eml unreadable: {e}")
    headers = [f"{h}: {msg.get(h)}" for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h)]
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            content = part.get_content()
            if part.get_content_type() == "text/html":
                from src.extraction.mail_text import html_to_text

                content = html_to_text(content)
            body = content
    except Exception as e:
        body = f"[body unreadable: {e}]"
    names = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    text = "\n".join(headers) + "\n\n" + (body or "").strip()
    if names:
        text += "\n\n[nested attachments, not opened: " + ", ".join(names) + "]"
    if not headers and not body.strip():
        return ReadResult(None, "listed_only", "eml has no headers or body")
    return ReadResult(text.strip(), "eml_parse", f"nested mail, {len(names)} attachments listed")


def list_archive(data: bytes, name: str) -> ReadResult:
    members: list[str] = []
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = [i.filename for i in z.infolist() if not i.is_dir()]
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                members = [m.name for m in t.getmembers() if m.isfile()]
    except Exception as e:
        return ReadResult(None, "listed_only", f"archive not listable: {e}")
    shown = members[:100]
    text = f"[archive {name}, {len(members)} files — listed, not opened]\n" + "\n".join(shown)
    if len(members) > 100:
        text += f"\n[... {len(members) - 100} more]"
    return ReadResult(text, "listed_only", f"archive with {len(members)} members", {"members": len(members)})


def _read_text_like(data: bytes) -> Optional[ReadResult]:
    head = data[:4096]
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head[:512]):
        try:
            return ReadResult(data.decode("utf-8", errors="replace")[:50000], "pdf_text", "plain text file")
        except Exception:
            return None
    return None


def read_bytes(name: str, family: str, data: bytes) -> ReadResult:
    """Dispatch on the sniffed family (bytes first, name second)."""
    sniffed = sniff_family(name, family, data)
    result = _read_by_family(name, sniffed, data)
    if sniffed != family:
        result.meta["sniffed_family"] = sniffed
        result.note = f"declared {family}, bytes say {sniffed}; " + result.note
    return result


def _read_by_family(name: str, family: str, data: bytes) -> ReadResult:
    low = name.lower()
    if family == "pdf":
        return read_pdf(data)
    if family == "word":
        if low.endswith(".docx") or data[:4] == b"PK\x03\x04":
            return read_docx(data)
        return ReadResult(None, "listed_only", "legacy Word format needs LibreOffice conversion")
    if family == "sheet":
        if low.endswith((".xlsx", ".xlsm")) or data[:4] == b"PK\x03\x04":
            return read_xlsx(data)
        if low.endswith(".csv"):
            try:
                text = data.decode("utf-8", errors="replace")
                lines = text.splitlines()
                cut = "\n".join(lines[:SHEET_ROW_CAP])
                if len(lines) > SHEET_ROW_CAP:
                    cut += f"\n[... cut at {SHEET_ROW_CAP} rows]"
                return ReadResult(cut, "xlsx_table", f"csv, {len(lines)} rows")
            except Exception as e:
                return ReadResult(None, "listed_only", f"csv unreadable: {e}")
        return ReadResult(None, "listed_only", "legacy spreadsheet format (.xls/.ods) not read")
    if family == "eml":
        if low.endswith(".msg") or data[:4] == b"\xd0\xcf\x11\xe0":
            return ReadResult(None, "listed_only", "Outlook .msg not parsed")
        return read_eml(data)
    if family == "archive":
        return list_archive(data, name)
    if family == "telemetry":
        return ReadResult(None, "listed_only", "telemetry / setting file — binary, recorded not parsed")
    if family == "image":
        return ReadResult(None, "listed_only", "image — vision disabled or not applicable")
    return _read_text_like(data) or ReadResult(None, "listed_only", "unknown binary type")
