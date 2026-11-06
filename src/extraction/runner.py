"""
Per-casefile orchestration of 08 §1: header -> window loop -> consolidate ->
validate -> store. Resumable at window granularity; bounded per invocation.

    runner = CasefileRunner(store, llm, fetcher, digester, options)
    report = await runner.run(casefile)

Control (all from the CLI):
    max_windows   stop after N windows this invocation; the record is left
                  resumable (progress.complete = false)
    restart       discard the stored record and rebuild from the header
    resume        (default) continue from progress.last_pagenum_processed

Checkpoint = the record itself (06 §4): after every window the record is
written with progress.last_pagenum_processed set to the window's last pagenum.
A window's audit file (windows/wNNN.json) holds what the model was GIVEN —
`inputs`: the record summary it could update, every page of the window with
body size/hash and twin status, every attachment URL with its fetch/junk/sent
outcome and the digest as sent, this window's token counts, one entry per
attempt with the CORRECTION feedback — and what it RETURNED (raw response,
applied delta, record after). The exact user prompt is the `.prompt.txt`
sidecar. So a bad row can be traced to the window and the evidence that
produced it (docs/14). Audit only: the record carries none of this.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.attachments.digest import AttachmentDigester, digest_for_prompt, plan_attachments
from src.attachments.fetch import Fetcher, FetchError
from src.attachments.junk_filter import attachment_name
from src.config import (
    CONSOLIDATE_PROMPT_VERSION,
    EXTRACTION_MAIL_MAX_CHARS,
    EXTRACTION_MAX_ACTIONS,
    EXTRACTION_SPLIT_SHARED_QUOTES,
    EXTRACTION_STRIP_QUOTES,
    EXTRACTION_TRANSLATE_QUOTES,
    EXTRACTION_VERSION,
    SPLIT_QUOTE_PROMPT_VERSION,
    TRANSLATE_QUOTES_PROMPT_VERSION,
    WINDOW_UPDATE_PROMPT_VERSION,
    get_prompt,
)
from src.contract.validator import (
    all_cited_shas,
    apply_validation,
    prune_shas,
    repair_citations,
    validate_record,
)
from src.corpus.export import Casefile, Page
from src.extraction.consolidate import assign_seq, consolidate
from src.extraction.mail_text import mail_body_text, normalise_for_match
from src.extraction.quote_split import split_shared_quotes
from src.extraction.record import WindowContext, collect_update_quotes, new_record, strip_sidecars
from src.extraction.store import RunStore
from src.extraction.translate import translate_missing
from src.extraction.window_update import WindowMail, run_window
from src.extraction.windows import WindowSpec, window_count, windows, windows_after
from src.llm import LLMClient
from src.utils import observability
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunOptions:
    window: WindowSpec = field(default_factory=lambda: WindowSpec(5, 1))
    max_windows: Optional[int] = None
    restart: bool = False
    strip_quotes: bool = EXTRACTION_STRIP_QUOTES
    mail_max_chars: int = EXTRACTION_MAIL_MAX_CHARS
    max_actions: int = EXTRACTION_MAX_ACTIONS
    skip_consolidate_call: bool = False  # deterministic seq only, no model call
    translate_quotes: bool = EXTRACTION_TRANSLATE_QUOTES  # add [EN: ...] to non-English quotes the model left bare
    split_quotes: bool = EXTRACTION_SPLIT_SHARED_QUOTES  # cut a sentence that evidences both an action and its result


@dataclass
class RunReport:
    casefile_id: str
    vessel: Optional[str]
    mails: int
    windows_total: int
    windows_done_before: int
    windows_run: int
    windows_rejected: int
    complete: bool
    last_pagenum: int
    flags: list[str]
    tokens: dict
    seconds: float
    attachments: dict
    error: Optional[str] = None


class CasefileRunner:
    def __init__(
        self,
        store: RunStore,
        llm: LLMClient,
        fetcher: Fetcher,
        digester: AttachmentDigester,
        options: RunOptions,
    ):
        self.store = store
        self.llm = llm
        self.fetcher = fetcher
        self.digester = digester
        self.opt = options
        self.window_prompt = get_prompt("window_update", WINDOW_UPDATE_PROMPT_VERSION)
        self.consolidate_prompt = get_prompt("consolidate", CONSOLIDATE_PROMPT_VERSION)
        self.translate_prompt = get_prompt("translate_quotes", TRANSLATE_QUOTES_PROMPT_VERSION)
        self.split_prompt = get_prompt("split_quote", SPLIT_QUOTE_PROMPT_VERSION)

    # ------------------------------------------------------------------------
    async def mail_text(self, page: Page) -> tuple[str, bool]:
        """(body text, fetched_now). Cached by mail_id; text regenerated from cached HTML when settings changed."""
        cached = self.store.get_mail(page.mail_id)
        fetched_now = False
        if cached is None or "html" not in cached:
            if not page.link:
                raise FetchError("", f"mail {page.mail_id} has no link")
            resp = await self.fetcher.get(page.link)
            html = resp.data.decode("utf-8", errors="replace")
            cached = {
                "mail_id": page.mail_id,
                "url": page.link,
                "html": html,
                "html_sha256": hashlib.sha256(resp.data).hexdigest(),
                "fetched_at": _now(),
            }
            fetched_now = True
        settings = {"strip_quotes": self.opt.strip_quotes, "max_chars": self.opt.mail_max_chars}
        if cached.get("text") is None or cached.get("settings") != settings:
            cached["text"] = mail_body_text(cached["html"], strip_quotes=self.opt.strip_quotes, max_chars=self.opt.mail_max_chars)
            cached["settings"] = settings
            self.store.put_mail(page.mail_id, cached)
        return cached["text"], fetched_now

    def _load_or_init_record(self, casefile: Casefile) -> tuple[dict, bool]:
        record = None if self.opt.restart else self.store.get_record(casefile.casefile_id)
        if record is not None and record.get("extraction_version") != EXTRACTION_VERSION:
            logger.warning(
                "%s: stored record is %s, current is %s — restarting from the header",
                casefile.casefile_id, record.get("extraction_version"), EXTRACTION_VERSION,
            )
            record = None
        if record is None:
            return new_record(casefile), True
        return record, False

    # ------------------------------------------------------------------------
    async def run(self, casefile: Casefile) -> RunReport:
        t0 = time.monotonic()
        cid = casefile.casefile_id
        pages = casefile.pages
        spec = self.opt.window
        total = window_count(len(pages), spec)
        record, fresh = self._load_or_init_record(casefile)
        last = record["progress"]["last_pagenum_processed"]
        pending = windows_after(pages, spec, last, lambda p: p.pagenum)
        done_before = total - len(pending)
        log = {"runs": [], "consolidate": None} if fresh else self.store.get_run_log(cid)
        run_entry = {"started_at": _now(), "extraction_version": EXTRACTION_VERSION, "windows": [], "tokens": None, "attachments": None}
        tracker = TokenTracker(stage="window_update", casefile_id=cid)
        mail_texts: dict[str, str] = {}
        digests: dict[str, dict] = {}
        windows_run = rejected = 0
        error = None
        # The same mail can sit in a casefile twice (two ids, seconds apart). The
        # copy is not shown to the model again, so it cannot be cited as a second source.
        seen_bodies = self._seen_bodies(pages, last)
        mail_aliases: dict[str, str] = {}

        if fresh:
            self.store.put_record(cid, strip_sidecars(record))

        with observability.trace_root("casefile_extract", metadata={"casefile_id": cid}):
            try:
                to_run = pending if self.opt.max_windows is None else pending[: self.opt.max_windows]
                all_windows = windows(pages, spec)
                for w in to_run:
                    index = all_windows.index(w)
                    with observability.span(f"window_{index}"):
                        mails: list[WindowMail] = []
                        mail_inputs: list[dict] = []  # audit: every page of the window, twins included
                        ctx = WindowContext(mail_ids=set(), mail_dates={}, shas=set(), max_actions=self.opt.max_actions)
                        for page in w:
                            body, fetched_now = await self.mail_text(page)
                            mail_texts[page.mail_id] = body
                            body_key = self._body_key(body)
                            twin = seen_bodies.setdefault(body_key, page.mail_id)
                            mail_input = {
                                "mail_id": page.mail_id,
                                "pagenum": page.pagenum,
                                "date": page.date_time,
                                "sender": f"{page.sender_name or ''} <{page.sender_email or ''}>".strip(),
                                "to": page.to,
                                "subject": page.subject,
                                "context_only": page.pagenum <= last,
                                "body_chars": len(body),
                                "body_sha1": body_key,
                                "fetched_now": fetched_now,
                                "skipped_as_twin_of": None,
                                "attachments": [],
                            }
                            mail_inputs.append(mail_input)
                            if twin != page.mail_id:
                                mail_aliases[page.mail_id] = twin
                                mail_input["skipped_as_twin_of"] = twin
                                mail_input["attachments"] = self._attachment_inputs(page, {}, sent=False)
                                continue
                            docs = await self.digester.process_mail_attachments(page.attachments, page.mail_id, cid, tracker)
                            att = []
                            for d in docs:
                                digests[d["sha256"]] = d
                                if d.get("content_bearing"):
                                    att.append(digest_for_prompt(d))
                                    ctx.shas.add(d["sha256"])
                            mail_input["attachments"] = self._attachment_inputs(page, {d["sha256"]: d for d in docs}, sent=True)
                            ctx.mail_ids.add(page.mail_id)
                            ctx.mail_dates[page.mail_id] = page.date_time or ""
                            mails.append(
                                WindowMail(
                                    mail_id=page.mail_id,
                                    pagenum=page.pagenum,
                                    date=page.date_time,
                                    sender=f"{page.sender_name or ''} <{page.sender_email or ''}>".strip(),
                                    to=page.to,
                                    subject=page.subject,
                                    body=body,
                                    attachments=att,
                                    context_only=page.pagenum <= last,
                                )
                            )
                        if not mails:  # every mail of the window is a copy of one already read
                            record["progress"]["last_pagenum_processed"] = last = w[-1].pagenum
                            self.store.put_record(cid, strip_sidecars(record))
                            run_entry["windows"].append({"index": index, "pagenums": [w[0].pagenum, w[-1].pagenum], "added": None, "updated": None, "flags": ["all_mails_duplicate"], "seconds": 0, "mails": 0, "attachments_sent": 0, "tokens": None})
                            continue
                        calls_before = len(tracker.calls)  # this window's model calls only (digests were before)
                        result = await run_window(self.llm, self.window_prompt, record, mails, ctx, tracker, index)
                        window_tokens = self._window_tokens(tracker, calls_before)
                        windows_run += 1
                        split = 0
                        if self.opt.split_quotes and result.applied is not None:
                            split = await split_shared_quotes(self.llm, self.split_prompt, record, tracker)
                        translated = 0
                        if self.opt.translate_quotes and result.applied is not None:
                            translated = await translate_missing(self.llm, self.translate_prompt, record, tracker)
                        if result.rejected:
                            rejected += 1
                            record.setdefault("validation_flags", []).append(f"window_rejected:w{index}")
                        record["progress"]["last_pagenum_processed"] = w[-1].pagenum
                        last = w[-1].pagenum
                        self.store.put_window(
                            cid,
                            index,
                            {
                                "index": index,
                                "pagenums": result.pagenums,
                                "mail_ids": result.mail_ids,
                                "attachment_shas": sorted(ctx.shas),
                                "attempts": result.attempts,
                                "rejected": result.rejected,
                                "error": result.error,
                                "seconds": round(result.seconds, 2),
                                "prompt_chars": result.prompt_chars,
                                "raw_response": result.raw_response,
                                "applied": asdict(result.applied) if result.applied else None,
                                "split_quotes": split,
                                "translated_quotes": translated,
                                "mail_aliases": {k: v for k, v in mail_aliases.items() if k in {p.mail_id for p in w}},
                                "update_quotes": collect_update_quotes(record),
                                "record_after": strip_sidecars(record),
                                # What the model was given (docs/14). The prompt text itself is the
                                # .prompt.txt sidecar; this block is the structured view of it.
                                "inputs": {
                                    "prompt_version": WINDOW_UPDATE_PROMPT_VERSION,
                                    "record_summary": result.record_summary,
                                    "next_free_ids": (result.record_summary or {}).get("next_free_ids"),
                                    "mails": mail_inputs,
                                    "tokens": window_tokens,
                                    "attempts": result.attempts_log,
                                },
                            },
                        )
                        self.store.put_window_prompt(cid, index, result.prompt_text)
                        self.store.put_record(cid, strip_sidecars(record))
                        attachments_sent = sum(1 for m in mail_inputs for a in m["attachments"] if a["sent_to_model"])
                        run_entry["windows"].append({"index": index, "pagenums": [result.pagenums[0], result.pagenums[-1]], "added": result.applied.added if result.applied else None, "updated": result.applied.updated if result.applied else None, "flags": result.applied.flags if result.applied else [result.error], "seconds": round(result.seconds, 1), "mails": len(mails), "attachments_sent": attachments_sent, "tokens": window_tokens})
                        logger.info(
                            "%s window %d/%d pages %d-%d: +%s ~%s %s",
                            cid[:8], index + 1, total, result.pagenums[0], result.pagenums[-1],
                            {k: len(v) for k, v in result.applied.added.items() if v} if result.applied else "-",
                            {k: len(v) for k, v in result.applied.updated.items() if v} if result.applied else "-",
                            "REJECTED" if result.rejected else "",
                        )

                finished_all = last >= pages[-1].pagenum if pages else True
                if finished_all and not record["progress"]["complete"]:
                    with observability.span("consolidate"):
                        co_tracker = TokenTracker(stage="consolidate", casefile_id=cid)
                        co_log = await consolidate(None if self.opt.skip_consolidate_call else self.llm, self.consolidate_prompt, record, co_tracker)
                        co_log["tokens"] = co_tracker.get_totals()
                        log["consolidate"] = co_log
                    # Validation needs every cited mail/attachment text, not just this run's.
                    self._load_all_sources(record, mail_texts, digests)
                    soft = repair_citations(record, mail_texts, digests, self.store.digests_for_casefile(cid))
                    log["postprocess"] = {"shas_pruned": prune_shas(record, mail_texts, digests)}
                    soft += [f"similar_faults:{a}~{b}" for a, b in (co_log.get("fault_pairs") or {}).get("left_distinct", [])]
                    flags = validate_record(strip_sidecars(record), mail_texts, digests, self.opt.max_actions)
                    carried = [f for f in record.get("validation_flags", []) if f.startswith("window_rejected")]
                    apply_validation(record, carried + soft + flags)
                    record["progress"]["complete"] = True
                    self.store.put_record(cid, strip_sidecars(record))
                elif not finished_all:
                    assign_seq(record)
                    self.store.put_record(cid, strip_sidecars(record))
            except Exception as e:  # keep the checkpoint, report, move on to the next casefile
                error = f"{type(e).__name__}: {e}"
                logger.exception("%s: run stopped", cid)

        run_entry["finished_at"] = _now()
        run_entry["tokens"] = tracker.get_totals()
        run_entry["attachments"] = dict(self.digester.stats)
        run_entry["mail_aliases"] = mail_aliases
        run_entry["error"] = error
        log["runs"].append(run_entry)
        self.store.put_run_log(cid, log)

        return RunReport(
            casefile_id=cid,
            vessel=casefile.vessel_name,
            mails=len(pages),
            windows_total=total,
            windows_done_before=done_before,
            windows_run=windows_run,
            windows_rejected=rejected,
            complete=record["progress"]["complete"],
            last_pagenum=record["progress"]["last_pagenum_processed"],
            flags=list(record.get("validation_flags") or []),
            tokens=tracker.get_totals(),
            seconds=time.monotonic() - t0,
            attachments=dict(self.digester.stats),
            error=error,
        )

    @staticmethod
    def _body_key(body: str) -> str:
        return hashlib.sha1(normalise_for_match(body).encode("utf-8")).hexdigest()

    def _attachment_inputs(self, page: Page, docs_by_sha: dict[str, dict], *, sent: bool) -> list[dict]:
        """Audit row per attachment URL of one page (docs/14): what the digester just did with it
        (its `outcomes`, nothing re-fetched) and whether its digest went into the prompt.

        status: fetched_digested | cache_hit | junk_skipped | fetch_error | not_processed
        (not_processed = the page is a skipped twin, or attachment mode is off)."""
        out = []
        for url in page.attachments:
            o = self.digester.outcomes.get(url) if sent else None
            sha = (o or {}).get("sha256")
            doc = docs_by_sha.get(sha) if sha else None
            if o is None:
                status = "not_processed"
            elif o["status"] == "fetch_error":
                status = "fetch_error"
            elif doc is not None and not doc.get("content_bearing"):
                status = "junk_skipped"
            elif o["status"] == "cache_hit":
                status = "cache_hit"
            else:
                status = "fetched_digested"
            content_bearing = bool(doc and doc.get("content_bearing"))
            sent_to_model = sent and content_bearing
            out.append(
                {
                    "url": url,
                    "name": (o or {}).get("name") or attachment_name(url),
                    "sha256": sha,
                    "status": status,
                    "content_bearing": content_bearing,
                    "read_method": doc.get("extraction_method") if doc else None,
                    "digested_now": bool((o or {}).get("digested_now")),
                    "error": (o or {}).get("error"),
                    "sent_to_model": sent_to_model,
                    "digest_as_sent": digest_for_prompt(doc) if sent_to_model else None,
                }
            )
        return out

    @staticmethod
    def _window_tokens(tracker: TokenTracker, calls_before: int) -> dict:
        """Token use of the calls made since `calls_before` — i.e. this window's model calls."""
        calls = tracker.calls[calls_before:]
        p = sum(c["input_tokens"] for c in calls)
        c_ = sum(c["output_tokens"] for c in calls)
        return {"prompt_tokens": p, "completion_tokens": c_, "total_tokens": p + c_, "call_count": len(calls)}

    def _seen_bodies(self, pages: list[Page], last_pagenum: int) -> dict[str, str]:
        """body hash -> first mail id, for pages already folded in (from the mail cache)."""
        seen: dict[str, str] = {}
        for p in pages:
            if p.pagenum > last_pagenum:
                break
            cached = self.store.get_mail(p.mail_id)
            if cached and cached.get("text"):
                seen.setdefault(self._body_key(cached["text"]), p.mail_id)
        return seen

    def _load_all_sources(self, record: dict, mail_texts: dict[str, str], digests: dict[str, dict]) -> None:
        """Pull cached texts for every mail/sha the record cites (earlier runs' windows)."""
        for coll in ("equipment", "problems", "faults", "action_logs"):
            for row in record.get(coll, []):
                prov = row.get("provenance") or {}
                ids = list(prov.get("mail_ids") or [])
                if coll == "action_logs":
                    ids.append(row.get("asserted_in_mail"))
                    ids.append((row.get("outcome") or {}).get("outcome_in_mail"))
                for mid in ids:
                    if mid and mid not in mail_texts:
                        cached = self.store.get_mail(mid)
                        if cached and cached.get("text"):
                            mail_texts[mid] = cached["text"]
                for sha in all_cited_shas(prov):
                    if sha not in digests:
                        d = self.store.get_digest(sha)
                        if d:
                            digests[sha] = d


def plan(casefile: Casefile, spec: WindowSpec, store: RunStore) -> dict:
    """Dry-run view of one casefile: nothing fetched, nothing called."""
    record = store.get_record(casefile.casefile_id)
    # A record built by another prompt/schema version is rebuilt from the header on the next run.
    stale = record is not None and record.get("extraction_version") != EXTRACTION_VERSION
    last = record["progress"]["last_pagenum_processed"] if record and not stale else -1
    total = window_count(len(casefile.pages), spec)
    pending = windows_after(casefile.pages, spec, last, lambda p: p.pagenum)
    urls = [u for p in casefile.pages for u in p.attachments]
    return {
        "casefile_id": casefile.casefile_id,
        "vessel": casefile.vessel_name,
        "title": casefile.title,
        "mails": len(casefile.pages),
        "windows_total": total,
        "windows_done": total - len(pending),
        "windows_pending": len(pending),
        "pending_pagenums": [[w[0].pagenum, w[-1].pagenum] for w in pending],
        "stored": None if record is None else {
            "extraction_version": record.get("extraction_version"),
            "stale": stale,
            "complete": record["progress"]["complete"],
            "last_pagenum": last,
            "rows": {k: len(record.get(k, [])) for k in ("equipment", "problems", "faults", "action_logs")},
            "flags": len(record.get("validation_flags") or []),
        },
        "attachments": plan_attachments(urls),
    }
