"""
Attachment digests: fetch -> sha256 -> junk filter -> read (+ OCR) -> one digest
call, stored once per sha (schema/attachment_digest.v1.schema.json), reused
across windows, casefiles and re-runs (Tier 0 cache, 02 §2.3).

Modes (EXTRACTION_ATTACHMENT_MODE / --attachments):
    off     nothing is fetched; the window prompt gets no attachments
    list    fetch + sha + junk filter; content-bearing files are listed by
            name and kind, text is extracted and cached, no model call
    digest  as list, plus OCR of scanned PDF pages (src/attachments/ocr.py)
            and one LLM digest call per content-bearing file that has text
            (or an image, when EXTRACTION_VISION_ENABLED)

Identity documents are classified and never read (04 §4). The extractor never
sees a URL; it gets shas and digests (07 §3).

The stored digest carries an extra `audit` block (name, bytes, junk reason,
reader version, OCR note) that the attachment_digest.v1 schema does not list;
a Mongo loader strips it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.attachments.fetch import Fetcher
from src.attachments.junk_filter import attachment_name, decide, family_of
from src.attachments.ocr import PdfOcr
from src.attachments.readers import (
    READER_VERSION,
    ReadResult,
    is_identity_document,
    join_pages,
    read_bytes,
)
from src.config import (
    ATTACHMENT_DIGEST_PROMPT_VERSION,
    EXTRACTION_DIGEST_TEXT_MAX_CHARS,
    EXTRACTION_VISION_ENABLED,
    get_prompt,
)
from src.extraction.store import RunStore
from src.llm import LLMClient, LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

# Closed tag list (04 §2.1: free-text tags fragment like symptom wording).
DIGEST_TAGS = [
    "service_report", "sof", "defect_report", "inspection_report", "running_in_procedure",
    "quotation", "invoice", "purchase_order", "delivery_note", "packing_list", "awb",
    "logistics", "customs", "class_document", "flag_document", "drawing", "manual",
    "setting_file", "performance_form", "photo", "logbook", "correspondence", "identity_document",
    "other",
]

KIND_BY_FAMILY_LISTED = {
    "image": "Image (not read)",
    "archive": "Archive (listed, not opened)",
    "telemetry": "Telemetry / setting file",
    "pdf": "PDF (text not extracted)",
    "word": "Word document (not read)",
    "sheet": "Spreadsheet (not read)",
    "eml": "Nested mail (not parsed)",
    "other": "File (not read)",
}

TEXT_METHODS = ("pdf_text", "pdf_ocr", "docx_text", "xlsx_table", "eml_parse")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AttachmentDigester:
    def __init__(
        self,
        store: RunStore,
        fetcher: Fetcher,
        llm: Optional[LLMClient],
        mode: str = "digest",
        concurrency: int = 4,
        vision: bool = EXTRACTION_VISION_ENABLED,
        text_max_chars: int = EXTRACTION_DIGEST_TEXT_MAX_CHARS,
        ocr: Optional[PdfOcr] = None,
    ):
        if mode not in ("off", "list", "digest"):
            raise ValueError(f"attachment mode must be off|list|digest, got {mode!r}")
        self.store = store
        self.fetcher = fetcher
        self.llm = llm
        self.mode = mode
        self.vision = vision
        self.text_max_chars = text_max_chars
        self.ocr = ocr if (ocr is not None and ocr.enabled and mode == "digest") else None
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._locks: dict[str, asyncio.Lock] = {}
        self.prompt = get_prompt("attachment_digest", ATTACHMENT_DIGEST_PROMPT_VERSION) if mode == "digest" else ""
        self.stats = {"links": 0, "fetched": 0, "cache_hits": 0, "digest_calls": 0, "ocr_pages": 0, "fetch_errors": 0}
        # Per-URL outcome of the last process_mail_attachments() calls, for the window audit
        # (docs/14): {status: cache_hit | fetched | fetch_error, sha256, name, digested_now, error}.
        # The runner reads it right after the call; nothing is re-fetched for it.
        self.outcomes: dict[str, dict] = {}

    # ------------------------------------------------------------------------
    async def process_mail_attachments(
        self, urls: list[str], mail_id: str, casefile_id: str, tracker: Optional[TokenTracker]
    ) -> list[dict]:
        """Digest docs for every URL on one mail (order kept; failed fetches dropped with a note)."""
        if self.mode == "off" or not urls:
            return []
        results = await asyncio.gather(
            *(self._one(url, mail_id, casefile_id, tracker) for url in urls), return_exceptions=True
        )
        docs = []
        for url, r in zip(urls, results):
            if isinstance(r, Exception):
                self.stats["fetch_errors"] += 1
                self.outcomes[url] = {"status": "fetch_error", "sha256": None, "name": attachment_name(url), "digested_now": False, "error": f"{type(r).__name__}: {r}"}
                logger.warning("attachment skipped (%s): %s", url, r)
                continue
            if r is not None:
                docs.append(r)
        return docs

    def _needs_rebuild(self, doc: dict) -> bool:
        """A cached digest that a newer reader, OCR or digest mode can improve."""
        if not doc.get("content_bearing"):
            return False  # junk decisions are made once (04 §2.1)
        audit = doc.get("audit") or {}
        method = doc.get("extraction_method")
        if method == "listed_only" and audit.get("reader_version", 1) < READER_VERSION:
            return True
        if method == "listed_only" and audit.get("family") == "pdf" and self.ocr and not audit.get("ocr_backend"):
            return True
        if method == "pdf_text" and audit.get("thin_pages") and self.ocr and not audit.get("ocr_backend"):
            return True
        if self.mode == "digest" and method in TEXT_METHODS and not doc.get("summary") and not audit.get("digest_error"):
            return True  # text cached by a `list` run; digest call still owed
        return False

    async def _one(self, url: str, mail_id: str, casefile_id: str, tracker: Optional[TokenTracker]) -> Optional[dict]:
        self.stats["links"] += 1
        name = attachment_name(url)
        known = self.store.get_url(url)
        sha = known["sha256"] if known else None
        data: Optional[bytes] = None
        content_type = known.get("content_type", "") if known else ""

        if sha is None:
            async with self._sem:
                fetched = await self.fetcher.get(url)
            self.stats["fetched"] += 1
            data = fetched.data
            content_type = fetched.content_type
            sha = hashlib.sha256(data).hexdigest()
            self.store.put_blob(sha, data)
            self.store.put_url(url, {"url": url, "sha256": sha, "content_type": content_type, "fetched_at": _now()})
        else:
            self.stats["cache_hits"] += 1

        lock = self._locks.setdefault(sha, asyncio.Lock())
        digested_now = False
        async with lock:
            doc = self.store.get_digest(sha)
            if doc is None or self._needs_rebuild(doc):
                if data is None:
                    data = self.store.get_blob(sha)
                    if data is None:  # blob evicted: refetch
                        async with self._sem:
                            data = (await self.fetcher.get(url)).data
                doc = await self._build_digest(sha, name, data, content_type, tracker, existing=doc)
                digested_now = True
            self._append_link(doc, mail_id, casefile_id, url, name)
            self.store.put_digest(sha, doc)
        self.outcomes[url] = {
            "status": "cache_hit" if known else "fetched",
            "sha256": sha,
            "name": name,
            "digested_now": digested_now,
            "error": None,
        }
        return doc

    # ------------------------------------------------------------------------
    @staticmethod
    def _append_link(doc: dict, mail_id: str, casefile_id: str, url: str, name: str) -> None:
        for link in doc["links"]:
            if link["mailbodyId"] == mail_id and link["attachment_url"] == url:
                return
        doc["links"].append(
            {"mailbodyId": mail_id, "casefileId": casefile_id, "attachment_url": url, "attachment_name": name}
        )

    @staticmethod
    def _identity(doc: dict) -> dict:
        doc.update(
            kind_of_doc="Identity document",
            summary="Identity document — classified, not extracted",
            tags=["identity_document"],
            maker="NA",
            model="NA",
            equipment_details="NA",
            extracted_text=None,
            extraction_method="skipped",
        )
        doc["audit"]["identity_document"] = True
        return doc

    async def _build_digest(
        self, sha: str, name: str, data: bytes, content_type: str, tracker: Optional[TokenTracker], existing: Optional[dict] = None
    ) -> dict:
        decision = decide(name, data, content_type)
        doc = {
            "sha256": sha,
            "links": list(existing["links"]) if existing else [],
            "content_bearing": decision.content_bearing,
            "kind_of_doc": "",
            "summary": None,
            "tags": [],
            "maker": "NA",
            "model": "NA",
            "equipment_details": "NA",
            "extracted_text": None,
            "text_ref": None,
            "extraction_method": "skipped",
            "audit": {
                "name": name, "bytes": len(data), "family": decision.family, "junk_reason": decision.reason,
                "created_at": (existing or {}).get("audit", {}).get("created_at") or _now(), "updated_at": _now(),
                "reader_version": READER_VERSION,
            },
        }
        if not decision.content_bearing:
            doc["kind_of_doc"] = "Email signature / logo image" if decision.family == "image" else "Non-content file"
            return doc

        # Identity documents by name: classify, never read (04 §4).
        if is_identity_document(name):
            return self._identity(doc)

        read = read_bytes(name, decision.family, data)
        family = read.meta.get("sniffed_family", decision.family)
        doc["audit"]["family"] = family
        doc["audit"]["read_note"] = read.note
        doc["audit"].update({k: v for k, v in read.meta.items() if k in ("page_count", "thin_pages", "members", "sniffed_family")})

        # OCR of scanned pages, then re-join the page texts in order.
        thin = read.meta.get("thin_page_indexes") or []
        if family == "pdf" and thin and self.ocr:
            pages_text = list(read.meta.get("pages_text") or [])
            texts, note = await self.ocr.ocr(data, thin, len(pages_text), tracker, label=name)
            doc["audit"]["ocr_backend"] = self.ocr.backend
            doc["audit"]["ocr_pages"] = len(texts)
            doc["audit"]["ocr_note"] = note
            self.stats["ocr_pages"] += len(texts)
            if texts:
                for i, t in texts.items():
                    if t.strip():
                        pages_text[i] = t.strip()
                joined = join_pages(pages_text)
                read = ReadResult(joined or None, "pdf_ocr" if joined else "listed_only", f"{read.note}; {note}", read.meta)

        doc["extracted_text"] = read.text
        doc["extraction_method"] = read.method
        doc["audit"]["read_note"] = read.note

        # Identity documents by content (a passport scan the name did not reveal).
        if read.text and is_identity_document(name, read.text):
            return self._identity(doc)

        if read.text is None and family == "image" and self.vision and self.mode == "digest" and self.llm:
            await self._digest_call(doc, name, family, None, data, content_type, tracker)
            doc["extraction_method"] = "vision" if doc.get("summary") else "listed_only"
            return doc

        if read.text is None:
            doc["kind_of_doc"] = KIND_BY_FAMILY_LISTED.get(family, "File (not read)")
            doc["summary"] = f"{name}: {read.note}" if read.method == "listed_only" else None
            doc["tags"] = ["photo"] if family == "image" else ["other"]
            return doc

        if self.mode == "digest" and self.llm:
            await self._digest_call(doc, name, family, read.text, None, content_type, tracker)
        else:
            doc["kind_of_doc"] = f"{family} document (text extracted, not digested)"
            doc["summary"] = None
        return doc

    async def _digest_call(
        self, doc: dict, name: str, family: str, text: Optional[str], image: Optional[bytes], content_type: str, tracker: Optional[TokenTracker]
    ) -> None:
        user_parts: list[dict] = []
        header = (
            f"File name: {name}\nFamily: {family}\nAllowed tags: {', '.join(DIGEST_TAGS)}\n"
            "Return JSON with keys kind_of_doc, summary, tags, maker, model, equipment_details."
        )
        if text is not None:
            body = text if len(text) <= self.text_max_chars else text[: self.text_max_chars] + f"\n[... cut at {self.text_max_chars} chars]"
            user_parts.append({"type": "text", "text": header + "\n\n--- DOCUMENT TEXT ---\n" + body})
        else:
            mime = content_type.split(";")[0] if content_type.startswith("image/") else "image/png"
            b64 = base64.b64encode(image or b"").decode("ascii")
            user_parts.append({"type": "text", "text": header + "\n\nThe document is the attached image."})
            user_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        try:
            self.stats["digest_calls"] += 1
            resp = await self.llm.chat_completion(
                messages=[{"role": "user", "content": user_parts}],
                system_message=self.prompt,
                response_format={"type": "json_object"},
                token_tracker=tracker,
            )
            content = resp["content"] or ""
            out = json.loads(content)
        except (LLMError, json.JSONDecodeError) as e:
            logger.warning("digest call failed for %s (%s): %s", name, doc["sha256"][:8], e)
            doc["kind_of_doc"] = doc.get("kind_of_doc") or f"{family} document (digest failed)"
            doc["audit"]["digest_error"] = str(e)
            return
        doc["kind_of_doc"] = str(out.get("kind_of_doc") or "Document")
        doc["summary"] = out.get("summary") if isinstance(out.get("summary"), str) else None
        tags = [t for t in (out.get("tags") or []) if isinstance(t, str) and t in DIGEST_TAGS]
        doc["tags"] = tags or ["other"]
        for k in ("maker", "model", "equipment_details"):
            v = out.get(k)
            doc[k] = v.strip() if isinstance(v, str) and v.strip() else "NA"
        doc["audit"]["digest_prompt_version"] = ATTACHMENT_DIGEST_PROMPT_VERSION
        # The model may recognise an identity document the name and regexes missed.
        if "identity_document" in doc["tags"] or doc["kind_of_doc"].lower().startswith("identity"):
            self._identity(doc)


def digest_for_prompt(doc: dict) -> dict:
    """What the window prompt sees of an attachment: sha + digest, no URL (07 §3)."""
    return {
        "attachment_sha": doc["sha256"],
        "file_name": (doc.get("links") or [{}])[0].get("attachment_name"),
        "kind_of_doc": doc.get("kind_of_doc"),
        "summary": doc.get("summary"),
        "tags": doc.get("tags"),
        "maker": doc.get("maker"),
        "model": doc.get("model"),
        "equipment_details": doc.get("equipment_details"),
        "extraction_method": doc.get("extraction_method"),
        "text_excerpt": (doc.get("extracted_text") or "")[:4000] or None,
    }


def plan_attachments(urls: list[str]) -> dict:
    """Dry-run view: what the junk filter would keep by file name alone (no fetch)."""
    from src.attachments.junk_filter import _JUNK_NAME_PATTERNS, ALWAYS_JUNK_EXT, extension

    by_family: dict[str, int] = {}
    junk_by_name = 0
    for url in urls:
        name = attachment_name(url)
        fam = family_of(name)
        by_family[fam] = by_family.get(fam, 0) + 1
        if extension(name) in ALWAYS_JUNK_EXT or any(rx.search(name) for rx in _JUNK_NAME_PATTERNS):
            junk_by_name += 1
    return {"links": len(urls), "by_family": by_family, "junk_by_name": junk_by_name}
