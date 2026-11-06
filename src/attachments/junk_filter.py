"""
Tier 1 junk filter (02 §2.3, 04 §2.1) — deterministic, no model call.

Decides `content_bearing` for a fetched file from its name, size, bytes and
(for images) pixel dimensions. On KING ONE the filename rule alone agreed with
content judgement on 162/166 links; the size/dimension rules catch most of the
rest. The decision is stored once on the sha-keyed digest and never re-made.
"""

from __future__ import annotations

import io
import posixpath
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

from src.config import (
    ATTACHMENT_INLINE_NAME_MAX_BYTES,
    ATTACHMENT_JUNK_MAX_BYTES,
    ATTACHMENT_JUNK_MAX_PIXELS,
)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
PDF_EXT = {".pdf"}
WORD_EXT = {".docx", ".doc", ".rtf", ".odt"}
SHEET_EXT = {".xlsx", ".xlsm", ".xls", ".csv", ".ods"}
EML_EXT = {".eml", ".msg"}
ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
TELEMETRY_EXT = {".addr", ".manbw-spaf", ".dat", ".bin", ".log"}
ALWAYS_JUNK_EXT = {".ics", ".vcf", ".p7s", ".asc"}

# Inline signature / logo image names as mail clients generate them.
_JUNK_NAME_PATTERNS = [
    re.compile(r"^image\d{3}\.(png|jpe?g|gif)$", re.I),  # image001.png
    re.compile(r"^image\.(png|jpe?g|gif)$", re.I),
    re.compile(r"^cid[_:-]", re.I),
    re.compile(r"^outlook-", re.I),
    re.compile(r"^winmail\.dat$", re.I),
    re.compile(r"^(logo|signature|banner|footer|header)[\w-]*\.(png|jpe?g|gif)$", re.I),
    re.compile(r"^~wrd\d+\.jpg$", re.I),  # Word inline render
]


def attachment_name(url: str) -> str:
    """Last path segment of the URL, percent-decoded."""
    path = urlparse(url).path
    return unquote(posixpath.basename(path)) or "unnamed"


def extension(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".tar.gz"):
        return ".tar.gz"
    m = re.search(r"(\.[a-z0-9-]+)$", lower)
    return m.group(1) if m else ""


def image_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) via Pillow when available; None when it cannot be read."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


@dataclass
class JunkDecision:
    content_bearing: bool
    reason: str  # why, for the digest's audit and for the test that pins the rule
    family: str  # image | pdf | word | sheet | eml | archive | telemetry | other


def family_of(name: str, content_type: str = "") -> str:
    ext = extension(name)
    if ext in IMAGE_EXT or content_type.startswith("image/"):
        return "image"
    if ext in PDF_EXT or "pdf" in content_type:
        return "pdf"
    if ext in WORD_EXT:
        return "word"
    if ext in SHEET_EXT:
        return "sheet"
    if ext in EML_EXT or content_type == "message/rfc822":
        return "eml"
    if ext in ARCHIVE_EXT or ext == ".tar.gz":
        return "archive"
    if ext in TELEMETRY_EXT:
        return "telemetry"
    return "other"


def decide(name: str, data: bytes, content_type: str = "") -> JunkDecision:
    """
    The rule, in order: always-junk types; junk file names (unless the image is
    large enough to be a pasted photo); tiny files; tiny
    images; everything else is content-bearing (archives and telemetry are
    content-bearing as *listed* facts — "a dump was taken on this date").
    """
    ext = extension(name)
    fam = family_of(name, content_type)

    if ext in ALWAYS_JUNK_EXT:
        return JunkDecision(False, f"type {ext} carries no case content", fam)
    for rx in _JUNK_NAME_PATTERNS:
        if rx.search(name):
            if fam == "image" and len(data) >= ATTACHMENT_INLINE_NAME_MAX_BYTES:
                return JunkDecision(True, f"inline-style name but {len(data)} bytes: pasted photo, kept", fam)
            return JunkDecision(False, f"name matches signature/inline pattern {rx.pattern!r}", fam)
    if fam == "image":
        if len(data) < ATTACHMENT_JUNK_MAX_BYTES:
            return JunkDecision(False, f"image under {ATTACHMENT_JUNK_MAX_BYTES} bytes", fam)
        dims = image_dimensions(data)
        if dims and (dims[0] < ATTACHMENT_JUNK_MAX_PIXELS or dims[1] < ATTACHMENT_JUNK_MAX_PIXELS):
            return JunkDecision(False, f"image {dims[0]}x{dims[1]} under {ATTACHMENT_JUNK_MAX_PIXELS}px", fam)
        return JunkDecision(True, "image above size and dimension thresholds", fam)
    if not ext and len(data) < ATTACHMENT_JUNK_MAX_BYTES:
        return JunkDecision(False, "no extension and tiny", fam)
    if len(data) == 0:
        return JunkDecision(False, "empty file", fam)
    return JunkDecision(True, "document type", fam)
