"""
Phase 1 of the symptom-core hygiene (docs/13 §8): symptom standards that are probably one
failure mode said differently, with the table that would merge them. Read-only.

    python scripts/near_duplicates.py                          # the current DB
    python scripts/near_duplicates.py --db data/p3/resolution/lookup.sqlite
    python scripts/near_duplicates.py --family fails_to_start
    python scripts/near_duplicates.py --json reports/near_duplicates.json

Scores every pair of cores inside a family with (token_set_ratio + token_sort_ratio) / 200 and
keeps pairs at or above 0.72; tags each differing token (verb_form, modality, condition,
frequency, head_noun, where, failure_mode, unknown) and prints one verdict per pair:
MERGE-BY-TABLE <table>, REVIEW or KEEP. Pending (undecomposed) spellings are scanned against the
standards too. Exit 0 always; the same list is review queue 4 of build_lookup.py --review and the
"possible duplicates" section of admin/review.html.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.resolution import LookupStore
from src.resolution.near_duplicates import THREAD_BAR, render_json, render_text, scan


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=config.RESOLUTION_DB_PATH)
    p.add_argument("--config-dir", default=config.RESOLUTION_CONFIG_DIR)
    p.add_argument("--family", help="one symptom family key (or 'unclassified')")
    p.add_argument("--bar", type=float, default=THREAD_BAR, help="lexical score threshold")
    p.add_argument("--json", help="also write the report as JSON to this file")
    args = p.parse_args(argv)

    with LookupStore(args.db, config_dir=args.config_dir) as store:
        report = scan(store, family=args.family, bar=args.bar)
    sys.stdout.write(render_text(report))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_json(report), encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
