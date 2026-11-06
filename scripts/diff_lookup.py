"""
Diff two lookup DBs (docs/13 §6): the re-resolve loop is rebuild + diff, never a migration.

    python scripts/build_lookup.py --db /tmp/new.sqlite --fresh
    python scripts/diff_lookup.py data/resolution/lookup.sqlite /tmp/new.sqlite [--kind K] [--md out.md]

Per kind: standards before / after; aliases whose standard moved (old key -> new key, by
raw_key); spellings now pending and why; residue token frequency from alias.parts / pending.parts
(top 30, with casefile counts); new standards; vanished standards. Both files are opened
read-only. Exit 0 always: it reports, it does not judge.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.resolution.diff import diff_paths, render_md


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("old", help="the current DB (e.g. data/resolution/lookup.sqlite)")
    p.add_argument("new", help="the candidate built with build_lookup.py --db NEW --fresh")
    p.add_argument("--kind", choices=("equipment", "part", "symptom"), help="one kind only")
    p.add_argument("--md", help="also write the report to this Markdown file")
    args = p.parse_args(argv)

    report = diff_paths(args.old, args.new, args.kind)
    text = render_md(report)
    if args.md:
        out = Path(args.md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
