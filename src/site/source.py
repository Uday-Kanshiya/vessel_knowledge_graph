"""
RecordSource — the one interface the site build reads through (12 §2, §7).

The filesystem implementation wraps the local run store (src/extraction/store.py).
When records move to Mongo (vessel_issues_incident_reports / vessel_issues_mail_attachments,
07) a second implementation of the same five methods replaces this one; nothing in
src/site or src/search reads a path directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from src.extraction.store import RunStore, read_json


class RecordSource(Protocol):
    def list_ids(self) -> list[str]: ...
    def record(self, casefile_id: str) -> dict | None: ...
    def windows(self, casefile_id: str) -> list[dict]: ...
    def windows_prompt(self, casefile_id: str, index: int) -> str | None: ...
    def run_log(self, casefile_id: str) -> dict: ...
    def mail(self, mail_id: str) -> dict | None: ...
    def attachment(self, sha: str) -> dict | None: ...


class FileRecordSource:
    """Reads the run store under <data_dir> (10 §output layout)."""

    def __init__(self, data_dir: str | Path, ids: Iterable[str] | None = None):
        self.store = RunStore(data_dir)
        self._ids = list(ids) if ids else None

    def list_ids(self) -> list[str]:
        ids = self.store.list_records()
        if self._ids is not None:
            keep = set(self._ids)
            ids = [i for i in ids if i in keep]
        return ids

    def record(self, casefile_id: str) -> dict | None:
        return self.store.get_record(casefile_id)

    def windows(self, casefile_id: str) -> list[dict]:
        wdir = self.store.casefile_dir(casefile_id) / "windows"
        if not wdir.exists():
            return []
        out = []
        for p in sorted(wdir.glob("w*.json")):
            doc = read_json(p)
            if doc is not None:
                out.append(doc)
        return out

    def windows_prompt(self, casefile_id: str, index: int) -> str | None:
        return self.store.get_window_prompt(casefile_id, index)

    def run_log(self, casefile_id: str) -> dict:
        return self.store.get_run_log(casefile_id)

    def mail(self, mail_id: str) -> dict | None:
        return self.store.get_mail(mail_id)

    def attachment(self, sha: str) -> dict | None:
        return self.store.get_digest(sha)


class DictRecordSource:
    """In-memory source for tests and for rendering a single record handed over as JSON."""

    def __init__(
        self,
        records: dict[str, dict],
        windows: dict[str, list[dict]] | None = None,
        run_logs: dict[str, dict] | None = None,
        mails: dict[str, dict] | None = None,
        attachments: dict[str, dict] | None = None,
        prompts: dict[str, dict[int, str]] | None = None,
    ):
        self._records = records
        self._windows = windows or {}
        self._run_logs = run_logs or {}
        self._mails = mails or {}
        self._attachments = attachments or {}
        self._prompts = prompts or {}  # casefile_id -> window index -> prompt text

    @classmethod
    def from_files(cls, paths: Iterable[str | Path]) -> DictRecordSource:
        records = {}
        for p in paths:
            with Path(p).open(encoding="utf-8") as f:
                rec = json.load(f)
            records[rec["casefile_id"]] = rec
        return cls(records)

    def list_ids(self) -> list[str]:
        return sorted(self._records)

    def record(self, casefile_id: str) -> dict | None:
        return self._records.get(casefile_id)

    def windows(self, casefile_id: str) -> list[dict]:
        return self._windows.get(casefile_id, [])

    def windows_prompt(self, casefile_id: str, index: int) -> str | None:
        return self._prompts.get(casefile_id, {}).get(index)

    def run_log(self, casefile_id: str) -> dict:
        return self._run_logs.get(casefile_id, {"runs": [], "consolidate": None})

    def mail(self, mail_id: str) -> dict | None:
        return self._mails.get(mail_id)

    def attachment(self, sha: str) -> dict | None:
        return self._attachments.get(sha)
