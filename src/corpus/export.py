"""
Read the casefile export JSON and hand the pipeline plain casefiles.

The export is a mongoexport-style dump: `_id` is {"$oid": ...}, dates are
{"$date": ...}. Everything downstream wants plain strings, so the wrappers are
unwrapped once, here. The export's LLM-written fields (summary, pages[].summary,
currentStatus, importanceReason, ...) are deliberately NOT exposed on the
Casefile: the record is built from fetched mail bodies and attachments only
(schema/README §5), and the easiest way to keep a prompt honest is to never
hand it the shortcut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


def unwrap(value: Any) -> Any:
    """Turn {"$oid": x} / {"$date": x} into x, recursively."""
    if isinstance(value, dict):
        if len(value) == 1:
            (k, v), = value.items()
            if k in ("$oid", "$date"):
                return unwrap(v)
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


@dataclass
class Page:
    """One mail of a casefile, as the extractor needs it. Nothing LLM-written."""

    mail_id: str
    pagenum: int
    date_time: Optional[str]
    sender_name: Optional[str]
    sender_email: Optional[str]
    to: Optional[str]
    subject: Optional[str]
    link: Optional[str]  # pages[].link[0] — the rendered HTML mail body
    attachments: list[str] = field(default_factory=list)  # plain URLs
    tags: list[str] = field(default_factory=list)  # audit only, never a filter


@dataclass
class Casefile:
    casefile_id: str
    imo: Optional[int]
    vessel_name: Optional[str]
    title: Optional[str]  # export.casefile — becomes problem_statement verbatim (08 §1)
    category: Optional[str]
    ui_category: list[str]
    tags: list[str]
    status: Optional[str]
    casefile_link: Optional[str]
    updated_at: Optional[str]
    pages: list[Page]

    @property
    def mail_count(self) -> int:
        return len(self.pages)

    @property
    def attachment_link_count(self) -> int:
        return sum(len(p.attachments) for p in self.pages)

    def export_meta(self) -> dict:
        """The verbatim audit copy the record carries (schema: export_meta)."""
        return {
            "category": self.category,
            "UICategory": list(self.ui_category),
            "tags": list(self.tags),
            "status": self.status,
            "casefile_link": self.casefile_link,
        }


def _page_from_export(raw: dict) -> Page:
    raw = unwrap(raw)
    links = raw.get("link") or []
    attachments = []
    for a in raw.get("attachments") or []:
        # The export stores plain URL strings; tolerate {url: ...} too.
        if isinstance(a, str):
            attachments.append(a)
        elif isinstance(a, dict):
            url = a.get("url") or a.get("link") or a.get("attachment_url")
            if url:
                attachments.append(url)
    return Page(
        mail_id=str(raw.get("referenceId")),
        pagenum=int(raw.get("pagenum", 0)),
        date_time=raw.get("dateTime"),
        sender_name=raw.get("senderName"),
        sender_email=raw.get("senderEmailAddress"),
        to=raw.get("toRecipientsEmailAddresses"),
        subject=raw.get("subject"),
        link=links[0] if links else None,
        attachments=attachments,
        tags=list(raw.get("tags") or []),
    )


def casefile_from_export(raw: dict) -> Casefile:
    """One export document -> Casefile. Pages come back sorted by pagenum."""
    cid = unwrap(raw.get("_id"))
    pages = sorted((_page_from_export(p) for p in raw.get("pages") or []), key=lambda p: p.pagenum)
    imo = raw.get("imo")
    try:
        imo = int(imo) if imo is not None else None
    except (TypeError, ValueError):
        imo = None
    return Casefile(
        casefile_id=str(cid),
        imo=imo,
        vessel_name=raw.get("vesselName"),
        title=raw.get("casefile"),
        category=raw.get("category"),
        ui_category=list(raw.get("UICategory") or []),
        tags=list(raw.get("tag") or []),
        status=raw.get("status"),
        casefile_link=raw.get("link"),
        updated_at=unwrap(raw.get("updatedAt")),
        pages=pages,
    )


class CasefileExport:
    """The export file, indexed by casefile id."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"casefile export not found: {self.path}")
        with self.path.open(encoding="utf-8") as f:
            data = json.load(f)
        raw_list = data["casefiles"] if isinstance(data, dict) else data
        self.meta = {k: v for k, v in data.items() if k != "casefiles"} if isinstance(data, dict) else {}
        self._raw: dict[str, dict] = {}
        for raw in raw_list:
            self._raw[str(unwrap(raw.get("_id")))] = raw

    def ids(self) -> list[str]:
        return list(self._raw)

    def __len__(self) -> int:
        return len(self._raw)

    def __contains__(self, casefile_id: str) -> bool:
        return casefile_id in self._raw

    def get(self, casefile_id: str) -> Casefile:
        if casefile_id not in self._raw:
            raise KeyError(f"casefile {casefile_id} not in export {self.path.name}")
        return casefile_from_export(self._raw[casefile_id])

    def select(self, ids: Iterable[str]) -> tuple[list[Casefile], list[str]]:
        """(found casefiles in the given order, ids that are not in the export)."""
        found, missing = [], []
        for cid in ids:
            if cid in self._raw:
                found.append(self.get(cid))
            else:
                missing.append(cid)
        return found, missing


def load_id_set(path: str | Path) -> list[dict]:
    """
    A test-set file: {"casefiles": [{"casefile_id": ..., "label": ...}, ...]} or a
    bare list of ids. Returns [{"casefile_id", "label"}] in file order.
    """
    with Path(path).open(encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("casefiles", data) if isinstance(data, dict) else data
    out = []
    for item in items:
        if isinstance(item, str):
            out.append({"casefile_id": item, "label": ""})
        else:
            out.append({"casefile_id": item["casefile_id"], "label": item.get("label", "")})
    return out
