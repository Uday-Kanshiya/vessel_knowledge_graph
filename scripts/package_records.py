"""
Zip the extracted casefile records for hand-off.

    python scripts/package_records.py [--set FILE | --ids ID...] [--all] [--include-incomplete] [--out PATH]

Each record goes in as  <VESSEL>__<casefile title>__<casefile_id>.json  so the
file name says what it is without opening it. Incomplete records (paused runs)
are skipped unless --include-incomplete. The zip lands in the run store
(EXTRACTION_DATA_DIR, default data/) as casefile_extracts_<timestamp>.zip,
with a manifest.json listing what is inside and each record's flags.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.corpus.export import load_id_set
from src.extraction.store import RunStore

DEFAULT_SET = ROOT / "eval" / "test_sets" / "extraction-10.json"


def slug(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip()
    text = re.sub(r"[\s_]+", " ", text)
    return text[:max_len].strip() or "untitled"


def record_filename(record: dict) -> str:
    vessel = slug(record.get("vessel_name") or "UNKNOWN", 30).upper()
    title = slug(record.get("problem_statement") or "", 60)
    return f"{vessel}__{title}__{record['casefile_id']}.json"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--set", default=str(DEFAULT_SET))
    p.add_argument("--ids", nargs="+")
    p.add_argument("--all", action="store_true", help="every record in the store, not just the set")
    p.add_argument("--include-incomplete", action="store_true")
    p.add_argument("--out", default=None, help="zip path (default <data>/casefile_extracts_<timestamp>.zip)")
    p.add_argument("--data-dir", default=config.EXTRACTION_DATA_DIR)
    args = p.parse_args(argv)

    store = RunStore(args.data_dir)
    if args.all:
        ids = store.list_records()
    elif args.ids:
        ids = args.ids
    else:
        ids = [i["casefile_id"] for i in load_id_set(args.set)]

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else store.root / f"casefile_extracts_{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)

    manifest, skipped = [], []
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for cid in ids:
            record = store.get_record(cid)
            if record is None:
                skipped.append({"casefile_id": cid, "why": "no record"})
                continue
            if not record["progress"]["complete"] and not args.include_incomplete:
                skipped.append({"casefile_id": cid, "why": f"incomplete (last pagenum {record['progress']['last_pagenum_processed']})"})
                continue
            name = record_filename(record)
            z.writestr(name, json.dumps(record, ensure_ascii=False, indent=2))
            manifest.append(
                {
                    "file": name,
                    "casefile_id": cid,
                    "vessel_name": record.get("vessel_name"),
                    "problem_statement": record.get("problem_statement"),
                    "extraction_version": record.get("extraction_version"),
                    "complete": record["progress"]["complete"],
                    "rows": {k: len(record.get(k, [])) for k in ("equipment", "problems", "faults", "action_logs")},
                    "validation_flags": record.get("validation_flags", []),
                    "needs_review": record.get("needs_review"),
                }
            )
        z.writestr(
            "manifest.json",
            json.dumps({"created_at": stamp, "schema": "casefile_extract.v2", "records": manifest, "skipped": skipped}, ensure_ascii=False, indent=2),
        )

    print(f"wrote {out}  ({len(manifest)} records, {len(skipped)} skipped)")
    for m in manifest:
        r = m["rows"]
        print(f"  {m['file']}  e/p/f/a {r['equipment']}/{r['problems']}/{r['faults']}/{r['action_logs']}  flags {len(m['validation_flags'])}")
    for s in skipped:
        print(f"  skipped {s['casefile_id']}: {s['why']}")
    return 0 if manifest else 1


if __name__ == "__main__":
    sys.exit(main())
