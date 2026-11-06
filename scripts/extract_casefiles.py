"""
Extract casefiles from the export into casefile_extract.v2 records, through the
LLM API, 5 mails per call, resumable. The operator's handle on 08 §1.

    python scripts/extract_casefiles.py --export PATH [--set FILE | --ids ID...] [controls] [--run]

Dry-run by default (03 §10): without --run it prints the plan — which casefiles,
how many windows each, how many are already done — and makes no network or
model call.

Which casefiles
    --set FILE          test-set JSON (default eval/test_sets/extraction-10.json)
    --ids ID [ID ...]   explicit casefile ids (overrides --set)
    --limit N           first N of the selection        --offset K   skip the first K
    --list              print the selection with stored progress and exit

How much per invocation
    --max-windows N     at most N windows per casefile this run; leaves the record resumable
    --restart           discard stored records for the selection and rebuild from the header
    --attachments MODE  off | list | digest (default from EXTRACTION_ATTACHMENT_MODE)
    --ocr BACKEND       off | vision | tesseract for scanned PDF pages (default PDF_OCR_BACKEND)
    --no-translate      do not add [EN: ...] translations to non-English quotes
    --no-split-quotes   do not cut a sentence that evidences both an action and its result
    --window N --overlap K   mails per call (default 5 / 1). N is the hard cap per call.
    --model NAME        LLM model override for this run
    --no-consolidate-call   deterministic seq only; skip the consolidate model call
    --concurrency N     casefiles processed in parallel (default 1)

Other
    --validate-only     re-run the deterministic validator on stored records and exit
    --data-dir DIR      run store (default EXTRACTION_DATA_DIR)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config
from src.attachments.digest import AttachmentDigester
from src.attachments.fetch import Fetcher
from src.attachments.ocr import PdfOcr
from src.contract.validator import (
    all_cited_shas,
    apply_validation,
    prune_shas,
    repair_citations,
    validate_record,
)
from src.corpus.export import Casefile, CasefileExport, load_id_set
from src.extraction.record import strip_sidecars
from src.extraction.runner import CasefileRunner, RunOptions, RunReport, plan
from src.extraction.store import RunStore
from src.extraction.windows import WindowSpec
from src.llm import LLMClient
from src.utils import observability

DEFAULT_SET = ROOT / "eval" / "test_sets" / "extraction-10.json"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--export", default=config.CASEFILE_EXPORT_PATH or None, help="casefile export JSON (or CASEFILE_EXPORT_PATH)")
    sel = p.add_argument_group("which casefiles")
    sel.add_argument("--set", default=str(DEFAULT_SET), help="test-set JSON file")
    sel.add_argument("--ids", nargs="+", help="explicit casefile ids")
    sel.add_argument("--limit", type=int, default=None)
    sel.add_argument("--offset", type=int, default=0)
    sel.add_argument("--list", action="store_true", help="print the selection and stored progress, then exit")
    ctl = p.add_argument_group("how much")
    ctl.add_argument("--run", action="store_true", help="actually fetch and call the model (default: dry-run plan)")
    ctl.add_argument("--max-windows", type=int, default=None)
    ctl.add_argument("--restart", action="store_true")
    ctl.add_argument("--attachments", choices=["off", "list", "digest"], default=config.EXTRACTION_ATTACHMENT_MODE)
    ctl.add_argument("--ocr", choices=["off", "vision", "tesseract"], default=config.PDF_OCR_BACKEND)
    ctl.add_argument("--no-translate", action="store_true", help="leave non-English quotes without [EN: ...]")
    ctl.add_argument("--no-split-quotes", action="store_true", help="do not cut sentences that evidence both an action and its result")
    ctl.add_argument("--window", type=int, default=config.EXTRACTION_WINDOW_SIZE)
    ctl.add_argument("--overlap", type=int, default=config.EXTRACTION_WINDOW_OVERLAP)
    ctl.add_argument("--model", default=None)
    ctl.add_argument("--no-consolidate-call", action="store_true")
    ctl.add_argument("--concurrency", type=int, default=1)
    ctl.add_argument("--mail-max-chars", type=int, default=config.EXTRACTION_MAIL_MAX_CHARS)
    ctl.add_argument("--keep-quotes", action="store_true", help="do not strip quoted earlier mails from bodies")
    oth = p.add_argument_group("other")
    oth.add_argument("--validate-only", action="store_true")
    oth.add_argument("--data-dir", default=config.EXTRACTION_DATA_DIR)
    oth.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def select(args, export: CasefileExport) -> tuple[list[Casefile], list[str], list[dict]]:
    if args.ids:
        items = [{"casefile_id": i, "label": ""} for i in args.ids]
    else:
        items = load_id_set(args.set)
    items = items[args.offset:]
    if args.limit is not None:
        items = items[: args.limit]
    found, missing = export.select([i["casefile_id"] for i in items])
    return found, missing, items


def print_plan(casefiles: list[Casefile], missing: list[str], items: list[dict], spec: WindowSpec, store: RunStore, args) -> None:
    labels = {i["casefile_id"]: i["label"] for i in items}
    print(f"\nexport: {args.export}")
    print(f"store:  {store.root.resolve()}")
    print(f"window: {spec.size} mails per call, overlap {spec.overlap} (stride {spec.stride}); attachments: {args.attachments}; ocr: {args.ocr}; model: {args.model or config.LLM_MODEL}; prompt: window_update/{config.WINDOW_UPDATE_PROMPT_VERSION}")
    print(f"mode:   {'RUN' if args.run else 'DRY-RUN (add --run to execute)'}" + (f"; max {args.max_windows} windows per casefile" if args.max_windows else "") + ("; RESTART" if args.restart else ""))
    print()
    hdr = f"{'#':>2} {'casefile_id':24} {'vessel':20} {'mails':>5} {'win':>4} {'done':>4} {'pend':>4} {'att':>4} {'junk':>4} {'rows e/p/f/a':>14} {'state':10} label"
    print(hdr)
    print("-" * len(hdr))
    tot_pending = 0
    for n, cf in enumerate(casefiles, start=1):
        pl = plan(cf, spec, store)
        stored = pl["stored"]
        state = "new" if stored is None else "stale" if stored.get("stale") else ("complete" if stored["complete"] else f"@p{stored['last_pagenum']}")
        rows = "-" if stored is None else "/".join(str(stored["rows"][k]) for k in ("equipment", "problems", "faults", "action_logs"))
        if stored and stored["flags"]:
            state += f" !{stored['flags']}"
        tot_pending += pl["windows_pending"]
        print(f"{n:>2} {cf.casefile_id:24} {(cf.vessel_name or '')[:20]:20} {pl['mails']:>5} {pl['windows_total']:>4} {pl['windows_done']:>4} {pl['windows_pending']:>4} {pl['attachments']['links']:>4} {pl['attachments']['junk_by_name']:>4} {rows:>14} {state[:22]:10} {labels.get(cf.casefile_id, '')[:60]}")
    for m in missing:
        print(f"   {m:24} NOT IN EXPORT")
    print(f"\n{len(casefiles)} casefiles, {sum(c.mail_count for c in casefiles)} mails, {tot_pending} windows pending"
          + (f" (this run: up to {min(tot_pending, len(casefiles) * args.max_windows)})" if args.max_windows else "") + "\n")


def print_report(r: RunReport) -> None:
    t = r.tokens
    state = "COMPLETE" if r.complete else f"paused at pagenum {r.last_pagenum}"
    print(
        f"  {r.casefile_id} {(r.vessel or '')[:18]:18} windows {r.windows_done_before}+{r.windows_run}/{r.windows_total}"
        f" {'(' + str(r.windows_rejected) + ' rejected) ' if r.windows_rejected else ''}-> {state};"
        f" tokens in/out {t['total_input_tokens']}/{t['total_output_tokens']} in {t['call_count']} calls;"
        f" attachments fetched {r.attachments.get('fetched', 0)} cache {r.attachments.get('cache_hits', 0)} digests {r.attachments.get('digest_calls', 0)} ocr pages {r.attachments.get('ocr_pages', 0)};"
        f" {r.seconds:.0f}s"
        + (f"; flags {len(r.flags)}" if r.flags else "")
        + (f"; ERROR {r.error}" if r.error else "")
    )


def validate_only(casefiles: list[Casefile], store: RunStore, max_actions: int) -> None:
    for cf in casefiles:
        record = store.get_record(cf.casefile_id)
        if record is None:
            print(f"  {cf.casefile_id}: no stored record")
            continue
        mail_texts, digests = {}, {}
        for coll in ("equipment", "problems", "faults", "action_logs"):
            for row in record.get(coll, []):
                prov = row.get("provenance") or {}
                ids = list(prov.get("mail_ids") or [])
                if coll == "action_logs":
                    ids += [row.get("asserted_in_mail"), (row.get("outcome") or {}).get("outcome_in_mail")]
                for mid in ids:
                    if mid and mid not in mail_texts:
                        c = store.get_mail(mid)
                        if c and c.get("text"):
                            mail_texts[mid] = c["text"]
                for sha in all_cited_shas(prov):
                    if sha not in digests:
                        d = store.get_digest(sha)
                        if d:
                            digests[sha] = d
        carried = [f for f in record.get("validation_flags", []) if f.startswith(("window_rejected", "quote_in_uncited_attachment"))]
        soft = repair_citations(record, mail_texts, digests, store.digests_for_casefile(cf.casefile_id))
        prune_shas(record, mail_texts, digests)
        soft += [f for f in record.get("validation_flags", []) if f.startswith("similar_faults")]
        flags = validate_record(strip_sidecars(record), mail_texts, digests, max_actions)
        apply_validation(record, carried + soft + flags)
        store.put_record(cf.casefile_id, record)
        print(f"  {cf.casefile_id} {(cf.vessel_name or '')[:18]:18} {len(flags)} flags, {len(soft)} repaired" + (": " + "; ".join(flags[:6]) + (" ..." if len(flags) > 6 else "") if flags else ""))


async def run_all(casefiles: list[Casefile], store: RunStore, args, spec: WindowSpec) -> list[RunReport]:
    llm = LLMClient(model=args.model)
    options = RunOptions(
        window=spec,
        max_windows=args.max_windows,
        restart=args.restart,
        strip_quotes=not args.keep_quotes,
        mail_max_chars=args.mail_max_chars,
        skip_consolidate_call=args.no_consolidate_call,
        translate_quotes=not args.no_translate,
        split_quotes=not args.no_split_quotes,
    )
    reports: list[RunReport] = []
    sem = asyncio.Semaphore(max(1, args.concurrency))
    async with Fetcher() as fetcher:
        ocr = PdfOcr(llm=llm, backend=args.ocr) if args.ocr != "off" else None
        digester = AttachmentDigester(store, fetcher, llm, mode=args.attachments, concurrency=config.EXTRACTION_ATTACHMENT_CONCURRENCY, ocr=ocr)
        runner = CasefileRunner(store, llm, fetcher, digester, options)

        async def one(cf: Casefile):
            async with sem:
                print(f"\n=== {cf.casefile_id} {cf.vessel_name} — {cf.title} ({cf.mail_count} mails)")
                rep = await runner.run(cf)
                print_report(rep)
                reports.append(rep)

        await asyncio.gather(*(one(cf) for cf in casefiles))
    observability.flush()
    return reports


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not args.export:
        print("error: no export given. Pass --export PATH or set CASEFILE_EXPORT_PATH in .env", file=sys.stderr)
        return 2
    try:
        spec = WindowSpec(args.window, args.overlap)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    export = CasefileExport(args.export)
    store = RunStore(args.data_dir)
    casefiles, missing, items = select(args, export)

    print_plan(casefiles, missing, items, spec, store, args)
    if args.list or not (args.run or args.validate_only):
        return 0
    if not casefiles:
        print("nothing selected")
        return 1
    if args.validate_only:
        validate_only(casefiles, store, config.EXTRACTION_MAX_ACTIONS)
        return 0
    if not config.LLM_API_KEY or config.LLM_API_KEY.startswith("your_"):
        print("error: LLM_API_KEY is not set (copy .env.example to .env)", file=sys.stderr)
        return 2

    reports = asyncio.run(run_all(casefiles, store, args, spec))
    total_in = sum(r.tokens["total_input_tokens"] for r in reports)
    total_out = sum(r.tokens["total_output_tokens"] for r in reports)
    complete = sum(1 for r in reports if r.complete)
    errors = [r for r in reports if r.error]
    print(f"\n=== {len(reports)} casefiles: {complete} complete, {len(reports) - complete} paused; tokens in/out {total_in}/{total_out}; records in {store.extractions.resolve()}")
    if errors:
        print(f"    {len(errors)} stopped with errors — re-run the same command to resume them")
    summary_path = store.root / "last_run_summary.json"
    store.root.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps([r.__dict__ for r in reports], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
