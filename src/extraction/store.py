"""
The local run store under EXTRACTION_DATA_DIR. Plain JSON files, one concern
per folder, all rebuildable (03 §4):

    <data>/cache/mail_bodies/<mail_id>.json     fetched body text (07 §4 mail_bodies cache)
    <data>/cache/urls/<sha1(url)>.json          url -> sha256 (so a known URL is not refetched)
    <data>/cache/attachments/<sha256>.json      attachment digest (attachment_digest.v1 shape)
    <data>/cache/attachments/blobs/<sha256>     raw bytes (re-digest without refetch)
    <data>/extractions/<casefile_id>/record.json        the v2 record; progress.* is the resume cursor
    <data>/extractions/<casefile_id>/windows/wNNN.json  per-window audit: what the model was given
                                                        (`inputs`: record summary, mails, attachments,
                                                        tokens, attempts — docs/14), raw response,
                                                        applied delta, record after
    <data>/extractions/<casefile_id>/windows/wNNN.prompt.txt  the exact user prompt of the window's
                                                        first attempt (sidecar; the site reads it)
    <data>/extractions/<casefile_id>/run_log.json       tokens, timings, flags, consolidate merges

Mongo (vessel_issues_incident_reports / vessel_issues_mail_attachments) is the
target store in 08 §1 step 5; src/db does not exist yet, so a loader from these
files is the next step, not this one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


class RunStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.cache = self.root / "cache"
        self.mail_dir = self.cache / "mail_bodies"
        self.url_dir = self.cache / "urls"
        self.att_dir = self.cache / "attachments"
        self.blob_dir = self.att_dir / "blobs"
        self.extractions = self.root / "extractions"

    # ---- mail bodies -------------------------------------------------------
    def mail_path(self, mail_id: str) -> Path:
        return self.mail_dir / f"{mail_id}.json"

    def get_mail(self, mail_id: str) -> Optional[dict]:
        return read_json(self.mail_path(mail_id))

    def put_mail(self, mail_id: str, doc: dict) -> None:
        write_json_atomic(self.mail_path(mail_id), doc)

    # ---- url -> sha ---------------------------------------------------------
    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def get_url(self, url: str) -> Optional[dict]:
        return read_json(self.url_dir / f"{self.url_key(url)}.json")

    def put_url(self, url: str, doc: dict) -> None:
        write_json_atomic(self.url_dir / f"{self.url_key(url)}.json", doc)

    # ---- attachments -------------------------------------------------------
    def digest_path(self, sha: str) -> Path:
        return self.att_dir / f"{sha}.json"

    def get_digest(self, sha: str) -> Optional[dict]:
        return read_json(self.digest_path(sha))

    def put_digest(self, sha: str, doc: dict) -> None:
        write_json_atomic(self.digest_path(sha), doc)

    def blob_path(self, sha: str) -> Path:
        return self.blob_dir / sha

    def get_blob(self, sha: str) -> Optional[bytes]:
        p = self.blob_path(sha)
        return p.read_bytes() if p.exists() else None

    def put_blob(self, sha: str, data: bytes) -> None:
        p = self.blob_path(sha)
        if p.exists():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    # ---- per-casefile record -------------------------------------------------
    def casefile_dir(self, casefile_id: str) -> Path:
        return self.extractions / casefile_id

    def record_path(self, casefile_id: str) -> Path:
        return self.casefile_dir(casefile_id) / "record.json"

    def get_record(self, casefile_id: str) -> Optional[dict]:
        return read_json(self.record_path(casefile_id))

    def put_record(self, casefile_id: str, record: dict) -> None:
        write_json_atomic(self.record_path(casefile_id), record)

    def window_path(self, casefile_id: str, index: int) -> Path:
        return self.casefile_dir(casefile_id) / "windows" / f"w{index:03d}.json"

    def put_window(self, casefile_id: str, index: int, doc: dict) -> None:
        write_json_atomic(self.window_path(casefile_id, index), doc)

    def window_prompt_path(self, casefile_id: str, index: int) -> Path:
        # `.prompt.txt`, never `.json`: FileRecordSource.windows() globs w*.json.
        return self.casefile_dir(casefile_id) / "windows" / f"w{index:03d}.prompt.txt"

    def put_window_prompt(self, casefile_id: str, index: int, text: str) -> None:
        p = self.window_prompt_path(casefile_id, index)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)

    def get_window_prompt(self, casefile_id: str, index: int) -> Optional[str]:
        p = self.window_prompt_path(casefile_id, index)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def run_log_path(self, casefile_id: str) -> Path:
        return self.casefile_dir(casefile_id) / "run_log.json"

    def get_run_log(self, casefile_id: str) -> dict:
        return read_json(self.run_log_path(casefile_id)) or {"runs": [], "consolidate": None}

    def put_run_log(self, casefile_id: str, log: dict) -> None:
        write_json_atomic(self.run_log_path(casefile_id), log)

    def list_records(self) -> list[str]:
        if not self.extractions.exists():
            return []
        return sorted(p.name for p in self.extractions.iterdir() if (p / "record.json").exists())

    def digests_for_casefile(self, casefile_id: str) -> dict[str, dict]:
        """Every cached digest with a link on this casefile, by sha (for citation repair)."""
        out: dict[str, dict] = {}
        if not self.att_dir.exists():
            return out
        for p in self.att_dir.glob("*.json"):
            doc = read_json(p)
            if doc and any(ln.get("casefileId") == casefile_id for ln in doc.get("links") or []):
                out[doc["sha256"]] = doc
        return out
