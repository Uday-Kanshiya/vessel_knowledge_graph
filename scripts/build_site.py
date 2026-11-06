"""
Build the static casefile browser from the run store (docs/12).

    python scripts/build_site.py                       # every complete record under data/ -> data/site/
    python scripts/build_site.py --ids ID [ID ...]     # a subset
    python scripts/build_site.py --set eval/test_sets/extraction-10.json
    python scripts/build_site.py --out /tmp/site --serve 8080   # build, then serve for a local look
    python scripts/build_site.py --check               # build, then verify every internal link and anchor

Idempotent: re-run after every extraction run. Output is a folder of HTML + one JSON index;
no server, no database. Open data/site/index.html directly or serve the folder as static files.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.resolution.lookup import LookupStore
from src.site.render import build, check_links
from src.site.source import FileRecordSource
from src.site.symptom_links import load_symptom_links


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--data-dir",
        default=config.EXTRACTION_DATA_DIR,
        help="run store (default: EXTRACTION_DATA_DIR)",
    )
    p.add_argument("--out", default=None, help="output folder (default <data-dir>/site)")
    p.add_argument("--ids", nargs="+", help="only these casefile ids")
    p.add_argument("--set", help="JSON test-set file with the ids to include")
    p.add_argument("--include-incomplete", action="store_true", help="also render paused records")
    p.add_argument(
        "--no-admin", action="store_true", help="skip admin/ pages (lookup, review, journey)"
    )
    p.add_argument(
        "--check", action="store_true", help="verify internal links and anchors after the build"
    )
    p.add_argument(
        "--serve", type=int, metavar="PORT", help="serve the output folder after building"
    )
    p.add_argument(
        "--symptom-links",
        default=config.SYMPTOM_LINKS_PATH,
        help="reviewed broader_than links between symptom groups (default: SYMPTOM_LINKS_PATH); "
        "a missing file means no links",
    )
    p.add_argument(
        "--lookup-db",
        default=config.RESOLUTION_DB_PATH,
        help="resolver DB (scripts/build_lookup.py)",
    )
    p.add_argument(
        "--no-lookup",
        action="store_true",
        help="build without the resolver: wording facets and a banner on the search page",
    )
    args = p.parse_args(argv)

    out = Path(args.out) if args.out else Path(args.data_dir) / "site"
    ids = None
    if args.set:
        from src.corpus.export import load_id_set

        ids = load_id_set(args.set)
    elif args.ids:
        ids = args.ids

    source = FileRecordSource(args.data_dir, ids)
    if not args.include_incomplete:
        complete = [
            i
            for i in source.list_ids()
            if ((source.record(i) or {}).get("progress") or {}).get("complete", True)
        ]
        source = FileRecordSource(args.data_dir, complete)

    lookup = None
    if not args.no_lookup and Path(args.lookup_db).exists():
        lookup = LookupStore(args.lookup_db)  # read-only use: find(), never resolve()
    elif not args.no_lookup:
        print(
            f"[site] no resolver DB at {args.lookup_db}: wording facets, run scripts/build_lookup.py",
            file=sys.stderr,
        )
    try:
        summary = build(
            source,
            out,
            admin=not args.no_admin,
            symptom_links=load_symptom_links(args.symptom_links),
            lookup=lookup,
        )
    finally:
        if lookup is not None:
            lookup.close()
    print(json.dumps(summary, indent=2))
    if summary["casefiles"] == 0:
        print("no records found — run scripts/extract_casefiles.py first", file=sys.stderr)
        return 1

    if args.check:
        problems = check_links(out)
        if problems:
            print(f"{len(problems)} broken link(s):", file=sys.stderr)
            for pr in problems[:50]:
                print("  " + pr, file=sys.stderr)
            return 2
        print("links ok")

    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        print(f"serving {out} at http://localhost:{args.serve}/  (Ctrl-C to stop)")
        with http.server.ThreadingHTTPServer(("", args.serve), handler) as httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
