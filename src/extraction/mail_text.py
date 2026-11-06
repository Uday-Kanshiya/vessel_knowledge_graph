"""
Rendered mail HTML -> plain text the model reads and the validator checks.

The pages at pages[].link[0] are simple: header paragraphs (From / Subject /
Date / To / CC), an <hr>, then the body as <p> and tables. stdlib html.parser
is enough; no bs4 dependency for this.

Quote stripping is conservative on purpose: only explicit reply/forward
separators cut the text. Inside a casefile the quoted mail is almost always
another page, so the loss is small; being aggressive would drop forwarded
vendor content that exists nowhere else. Whatever is cut here is also what the
validator matches evidence quotes against, so model and checker see one text.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "ul", "ol", "blockquote", "pre"}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._cell = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """HTML -> text with block structure kept as newlines, whitespace collapsed per line."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


# Reply/forward separators. Each is anchored to a line start.
_QUOTE_MARKERS = [
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.I | re.M),
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}", re.I | re.M),
    re.compile(r"^_{5,}\s*\n\s*From:", re.I | re.M),  # Outlook separator + header
    re.compile(r"^On .{5,120}? wrote:\s*$", re.I | re.M),
    re.compile(r"^-{3,}\s*原始邮件\s*-{3,}", re.M),  # Chinese "original mail"
    re.compile(r"^发件人[:：]", re.M),  # Chinese "From:" header of a quoted mail
]


def strip_quoted_replies(text: str) -> str:
    """
    Cut the body at the first reply/forward separator that appears AFTER the
    page's own header block. The page header (From:/Subject:/Date:/To:/CC:) is
    never cut, so the mail's own metadata always survives.
    """
    # The page's own header ends at the first blank line after "Date:"/"Subject:".
    header_end = 0
    m = re.search(r"^(?:CC|To|Date|Subject):.*$", text, re.M)
    while m:
        header_end = m.end()
        m = re.compile(r"^(?:CC|To|Date|Subject):.*$", re.M).search(text, header_end)
        if m and m.start() - header_end > 400:  # a header line far below is body content
            break
    cut = len(text)
    for rx in _QUOTE_MARKERS:
        hit = rx.search(text, header_end + 1)
        if hit and hit.start() < cut:
            cut = hit.start()
    if cut < len(text):
        return text[:cut].rstrip() + "\n[quoted earlier mail removed]"
    return text


def truncate(text: str, max_chars: int) -> str:
    if max_chars and len(text) > max_chars:
        return text[:max_chars].rstrip() + f"\n[... cut at {max_chars} chars]"
    return text


def mail_body_text(html: str, strip_quotes: bool = True, max_chars: int = 0) -> str:
    text = html_to_text(html)
    if strip_quotes:
        text = strip_quoted_replies(text)
    return truncate(text, max_chars)


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}


def normalise_for_match(text: str) -> str:
    """
    The quote-substring normalisation (02 §2.4): NFKC, unify dashes and quotes,
    lowercase, collapse whitespace. Applied to both the quote and the source.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DASHES).translate(_QUOTES)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def quote_found(quote: str, source: str) -> bool:
    """Is `quote` a verbatim (normalised) substring of `source`?"""
    if not quote or not source:
        return False
    return normalise_for_match(quote) in normalise_for_match(source)
