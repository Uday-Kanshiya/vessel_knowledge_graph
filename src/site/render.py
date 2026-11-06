"""
Static site build (12 §2): render(record) for every record a RecordSource lists, plus the
search page, mail/attachment pages, and the admin pages. Output is a folder; nothing is served.

    from src.site.source import FileRecordSource
    from src.site.render import build
    build(FileRecordSource("data"), "data/site")
"""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.resolution.lookup import LookupStore
from src.search.index import build_index
from src.site import views
from src.site.source import RecordSource
from src.site.symptom_links import SymptomLinks

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
ASSETS = HERE / "assets"

FLAG_HELP = {
    "quote_not_found": "an evidence quote is not verbatim in the cited mail body or document text",
    "quote_in_uncited_attachment": "the quote was found, but in an attachment the row does not cite",
    "over_cap": "more actions than the configured cap; the record is kept, the cap is a review signal",
    "same_quote_action_outcome": "one sentence was used as both the action quote and its outcome quote",
    "window_rejected": "a window's delta could not be applied after a retry; its mails contributed nothing",
    "outcome_downgraded": "an outcome without a quote was set to unknown",
    "mail_id_out_of_window": "a citation pointed at a mail outside the window and was dropped",
    "ids_renumbered_after_drop": "ids were renumbered after a row was dropped",
    "fault_id_corrected": "an action's fault_id was corrected by the consolidate step",
}


# --------------------------------------------------------------------------- small helpers


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v or "")


def _slug(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip()
    text = re.sub(r"[\s_]+", " ", text)
    return text[:max_len].strip() or "untitled"


def packaged_name(record: dict) -> str:
    """Same rule as scripts/package_records.py: VESSEL__title__id.json."""
    vessel = _slug(record.get("vessel_name") or "UNKNOWN", 30).upper()
    title = _slug(record.get("problem_statement") or "", 60)
    return f"{vessel}__{title}__{record['casefile_id']}.json"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["fmt_date"] = views.fmt_date
    env.filters["fmt_int"] = _fmt_int
    return env


def _json_for_script(obj) -> str:
    """JSON safe inside a <script type=application/json> block."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- quote highlighting


def _body_of(mail_doc: dict) -> str:
    _, body = views.split_mail_text(mail_doc.get("text") or "")
    return body


def _find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Locate a quote in the body: exact, then the original-script half of '原文 [EN: …]',
    then whitespace-tolerant. Returns (start, end) or None."""
    if not quote or not text:
        return None
    candidates = [quote.strip()]
    m = re.match(r"^(.*?)\s*\[EN:\s*(.*)\]\s*$", quote.strip(), flags=re.DOTALL)
    if m:
        candidates = [m.group(1).strip(), m.group(2).strip(), quote.strip()]
    for cand in candidates:
        if not cand:
            continue
        ix = text.find(cand)
        if ix >= 0:
            return ix, ix + len(cand)
        pattern = r"\s+".join(re.escape(tok) for tok in cand.split())
        try:
            mm = re.search(pattern, text, flags=re.IGNORECASE)
        except re.error:
            mm = None
        if mm:
            return mm.start(), mm.end()
    return None


def highlight_body(text: str, cites: list[dict]) -> tuple[str, list[dict]]:
    """Wrap each located quote in <mark id="q-<row>-<n>" class="<kind>">. Returns (html, cites with
    found/anchor filled in). Overlapping quotes share the first span's anchor."""
    spans: list[tuple[int, int, str, str]] = []  # start, end, id, kind
    out_cites = []
    for n, c in enumerate(cites):
        rid = c["row_id"]
        loc = _find_quote(text, c["quote"])
        cc = dict(
            c,
            n=n,
            found=loc is not None,
            anchor=f"q-{rid}-{n}",
            side_id=f"side-q-{rid}-{n}",
            row_anchor=False,
        )
        if not loc:
            cc["anchor"] = cc["side_id"]
        if loc:
            s, e = loc
            overlap = next((sp for sp in spans if not (e <= sp[0] or s >= sp[1])), None)
            if overlap:
                cc["anchor"] = overlap[2]
            else:
                spans.append((s, e, f"q-{rid}-{n}", c.get("kind", "assertion")))
        out_cites.append(cc)
    # a row with no located quote in this body gets its #q-<row> anchor on its first sidebar entry
    rows_with_span = {sid.rsplit("-", 1)[0] for _, _, sid, _ in spans}
    seen_rows: set[str] = set()
    for cc in out_cites:
        key = f"q-{cc['row_id']}"
        if key not in rows_with_span and key not in seen_rows:
            cc["row_anchor"] = True
            seen_rows.add(key)
    spans.sort()
    parts = []
    pos = 0
    first_for_row: set[str] = set()
    for s, e, sid, kind in spans:
        parts.append(html.escape(text[pos:s]))
        rid = sid.rsplit("-", 1)[0]  # q-a12
        extra = ""
        if rid not in first_for_row:
            first_for_row.add(rid)
            extra = f'<span id="{rid}"></span>'
        parts.append(f'{extra}<mark id="{sid}" class="{kind}">{html.escape(text[s:e])}</mark>')
        pos = e
    parts.append(html.escape(text[pos:]))
    return "".join(parts), out_cites


# --------------------------------------------------------------------------- the build


def build(
    source: RecordSource,
    out_dir: str | Path,
    *,
    admin: bool = True,
    symptom_links: SymptomLinks | list[dict] | None = None,
    lookup: LookupStore | None = None,
) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env = _env()
    built_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    ids = source.list_ids()
    records: list[dict] = []
    for cid in ids:
        rec = source.record(cid)
        if rec:
            records.append(rec)

    # reviewed broader_than links between normalised symptom groups (12 §3): validated against
    # the groups present in THIS build; a side that is missing drops the link with a warning
    links = (
        symptom_links
        if isinstance(symptom_links, SymptomLinks)
        else SymptomLinks(symptom_links or [])
    )
    known = {
        views.fault_symptom_key(rec, f, lookup.config if lookup is not None else None)
        for rec in records
        for f in rec.get("faults", []) or []
    }
    links.validate(known)
    for w in links.warnings:
        print(f"[site] {w}", file=sys.stderr)
    common = {
        "n_casefiles": len(records),
        "built_at": built_at,
        "admin_enabled": admin,
        "n_symptom_links": len(links),
        "symptom_link_warnings": list(links.warnings),
        # canonical facets (12 §3) need the resolver; without it the search page shows a banner
        "canonical_facets": lookup is not None,
    }

    # ---- what evidence pages can exist (cached bodies / digests only)
    mail_cites: dict[str, list[dict]] = defaultdict(list)  # mail_id -> cites across casefiles
    mail_casefiles: dict[str, list[dict]] = defaultdict(list)
    sha_casefiles: dict[str, list[dict]] = defaultdict(list)
    sha_cited_by: dict[str, list[dict]] = defaultdict(list)
    window_mail_ids: set[str] = set()
    raw_cites: list[dict] = []
    for rec in records:
        cid = rec["casefile_id"]
        for c in views.quote_cites(rec):
            raw_cites.append(dict(c, casefile_id=cid))
        for mid in views.cited_mail_ids(rec):
            mail_casefiles[mid].append(
                {
                    "id": cid,
                    "vessel": rec.get("vessel_name", ""),
                    "title": rec.get("problem_statement", ""),
                }
            )
        for sha in views.cited_shas(rec):
            sha_casefiles[sha].append({"id": cid})
        for fam in ("equipment", "problems", "faults", "action_logs"):
            for row in rec.get(fam, []) or []:
                for sha in views._shas_of(row):
                    sha_cited_by[sha].append(
                        {"casefile_id": cid, "row_id": views.row_id(row), "family": fam}
                    )
        for w in source.windows(cid):
            window_mail_ids.update(w.get("mail_ids") or [])

    mails: dict[str, dict] = {}
    for mid in sorted(set(mail_casefiles) | window_mail_ids):
        doc = source.mail(mid)
        if doc and (doc.get("text") or doc.get("html")):
            mails[mid] = doc
    # a quote that names several mails is attached to the mail(s) where it is actually found;
    # if it is found in none of the cached ones, it stays on the first so the gap is visible
    quote_home: dict[str, dict[str, str]] = defaultdict(
        dict
    )  # casefile_id -> row_id -> mail where the assertion quote lives
    for c in raw_cites:
        found_in = [
            m for m in c["mail_ids"] if m in mails and _find_quote(_body_of(mails[m]), c["quote"])
        ]
        for mid in found_in or c["mail_ids"][:1]:
            mail_cites[mid].append(c)
        if c["kind"] == "assertion":
            quote_home[c["casefile_id"]][c["row_id"]] = (found_in or c["mail_ids"][:1])[0]

    digests: dict[str, dict] = {}
    for sha in sorted(sha_casefiles):
        doc = source.attachment(sha)
        if doc:
            digests[sha] = doc

    mail_subjects, mail_dates = {}, {}
    for mid, doc in mails.items():
        hdr, _ = views.split_mail_text(doc.get("text") or "")
        mail_subjects[mid] = hdr.get("Subject", "")
        mail_dates[mid] = (hdr.get("Date") or "")[:10]
    attachment_names = {
        sha: ((d.get("audit") or {}).get("name") or "") for sha, d in digests.items()
    }
    attachment_kinds = {sha: (d.get("kind_of_doc") or "") for sha, d in digests.items()}
    titles = {r["casefile_id"]: r.get("problem_statement", "") for r in records}
    vessels = {r["casefile_id"]: r.get("vessel_name", "") for r in records}

    # ---- assets
    (out / "assets").mkdir(exist_ok=True)
    for p in ASSETS.iterdir():
        shutil.copyfile(p, out / "assets" / p.name)

    # ---- search page + index
    index = build_index(records, links, lookup)
    _write(out / "search_index.json", json.dumps(index, ensure_ascii=False, indent=1))
    _write(
        out / "index.html",
        env.get_template("index.html.j2").render(
            root="", index_json=_json_for_script(index), **common
        ),
    )

    # ---- casefile pages
    tpl_cf = env.get_template("casefile.html.j2")
    for rec in records:
        cid = rec["casefile_id"]
        fault_ids = {f["fault_id"] for f in rec.get("faults", [])}
        orphan = [a for a in rec.get("action_logs", []) if a.get("fault_id") not in fault_ids]
        cited = views.cited_mail_ids(rec)
        page = tpl_cf.render(
            root="../",
            record=rec,
            record_json=json.dumps(rec, ensure_ascii=False, indent=2),
            dates=views.date_range(rec),
            tally=views.outcome_tally(rec),
            outcome_group=views.OUTCOME_GROUP,
            equipment_types=sorted(
                {
                    e.get("equipment_type")
                    for e in rec.get("equipment", [])
                    if e.get("equipment_type")
                }
            ),
            equipment_by_id=views.equipment_by_id(rec),
            problems_by_id=views.problems_by_id(rec),
            faults_by_equipment=views.faults_by_equipment(rec),
            actions_by_fault=views.actions_by_fault(rec),
            symptom_parts=views.fault_decomposition(rec, lookup),
            canonical_position=lambda t: views.canonical_position(
                t, lookup.config if lookup is not None else None
            ),
            orphan_actions=orphan,
            flags_by_row=views.flags_by_row(rec),
            mail_ids=cited,
            mail_ids_missing=[m for m in cited if m not in mails],
            mails_available=mails,
            mail_subjects=mail_subjects,
            mail_dates=mail_dates,
            quote_home=quote_home.get(cid, {}),
            shas=views.cited_shas(rec),
            attachments_available=digests,
            attachment_names=attachment_names,
            attachment_kinds=attachment_kinds,
            **common,
        )
        _write(out / "casefile" / f"{cid}.html", page)

    # ---- mail pages
    tpl_mail = env.get_template("mail.html.j2")
    n_quotes_missing_total = 0
    for mid, doc in mails.items():
        text = doc.get("text") or ""
        headers, body = views.split_mail_text(text)
        body_html, cites = highlight_body(body, mail_cites.get(mid, []))
        missing = sum(1 for c in cites if not c["found"])
        n_quotes_missing_total += missing
        _write(
            out / "mail" / f"{mid}.html",
            tpl_mail.render(
                root="../",
                mail_id=mid,
                headers=headers,
                body_html=body_html,
                cites=cites,
                n_quotes_missing=missing,
                url=doc.get("url"),
                fetched_at=doc.get("fetched_at"),
                casefiles=mail_casefiles.get(mid, []),
                **common,
            ),
        )

    # ---- attachment pages
    tpl_att = env.get_template("attachment.html.j2")
    for sha, d in digests.items():
        audit = d.get("audit") or {}
        _write(
            out / "attachment" / f"{sha}.html",
            tpl_att.render(
                root="../",
                sha=sha,
                d=d,
                audit=audit,
                name=audit.get("name"),
                text_html=html.escape(d.get("extracted_text") or ""),
                links=d.get("links") or [],
                casefile_titles={k: f"{vessels[k]} · {v}" for k, v in titles.items()},
                mails_available=mails,
                cited_by=sha_cited_by.get(sha, []),
                digest_json=json.dumps(
                    {k: v for k, v in d.items() if k != "extracted_text"},
                    ensure_ascii=False,
                    indent=2,
                ),
                **common,
            ),
        )

    # ---- admin
    if admin:
        tables = views.lookup_tables(records, links)
        resolver = views.resolver_tables(lookup, records) if lookup is not None else None
        _write(
            out / "admin" / "lookup.html",
            env.get_template("admin_lookup.html.j2").render(
                root="../",
                lookup=tables,
                resolver=resolver,
                titles=titles,
                vessels=vessels,
                **common,
            ),
        )

        rows, by_kind_map = [], defaultdict(list)
        for rec in records:
            cid = rec["casefile_id"]
            flags = [views.parse_flag(f) for f in rec.get("validation_flags", []) or []]
            rows.append(
                {
                    "id": cid,
                    "vessel": rec.get("vessel_name", ""),
                    "title": rec.get("problem_statement", ""),
                    "n_faults": len(rec.get("faults", [])),
                    "n_actions": len(rec.get("action_logs", [])),
                    "flags": flags,
                    "needs_review": bool(rec.get("needs_review")),
                    "complete": bool((rec.get("progress") or {}).get("complete", True)),
                }
            )
            labels = {}
            for fam in ("equipment", "problems", "faults", "action_logs"):
                for row in rec.get(fam, []) or []:
                    labels[views.row_id(row)] = (
                        row.get("action")
                        or row.get("symptom")
                        or row.get("name")
                        or row.get("statement")
                        or ""
                    )
            for f in flags:
                by_kind_map[f["kind"]].append(
                    dict(
                        f,
                        casefile_id=cid,
                        vessel=rec.get("vessel_name", ""),
                        label=labels.get(f["row_id"] or "", ""),
                    )
                )
        rows.sort(key=lambda r: (not r["needs_review"], -len(r["flags"]), r["vessel"]))
        by_kind = sorted(by_kind_map.items(), key=lambda kv: -len(kv[1]))
        _write(
            out / "admin" / "review.html",
            env.get_template("admin_review.html.j2").render(
                root="../",
                rows=rows,
                by_kind=by_kind,
                resolver=resolver,
                flag_help=FLAG_HELP,
                n_review=sum(1 for r in rows if r["needs_review"]),
                **common,
            ),
        )

        tpl_j = env.get_template("admin_journey.html.j2")
        for rec in records:
            cid = rec["casefile_id"]
            j = views.journey(
                rec,
                source.windows(cid),
                source.run_log(cid),
                prompt_for=lambda i, cid=cid: source.windows_prompt(cid, i),
                mails_available=set(mails),
                attachments_available=set(digests),
            )
            _write(
                out / "admin" / "journey" / f"{cid}.html",
                tpl_j.render(
                    root="../../",
                    record=rec,
                    j=j,
                    packaged_name=packaged_name(rec),
                    mails_available=mails,
                    attachments_available=digests,
                    **common,
                ),
            )

    return {
        "out_dir": str(out),
        "casefiles": len(records),
        "symptom_links": len(links),
        "symptom_link_warnings": list(links.warnings),
        "mails": len(mails),
        "attachments": len(digests),
        "mails_cited_not_cached": len([m for m in mail_casefiles if m not in mails]),
        "quotes_not_located": n_quotes_missing_total,
    }


# --------------------------------------------------------------------------- link check

_HREF_RE = re.compile(r'href="([^"#]*)(?:#([^"]*))?"')
_ID_RE = re.compile(r'\sid="([^"]+)"')


def check_links(out_dir: str | Path) -> list[str]:
    """Every internal href resolves to a file, and every #anchor to an id in that file."""
    out = Path(out_dir)
    pages = {p: p.read_text(encoding="utf-8") for p in out.rglob("*.html")}
    ids = {p: set(_ID_RE.findall(t)) for p, t in pages.items()}
    problems: list[str] = []
    for p, t in pages.items():
        for href, frag in _HREF_RE.findall(t):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target = p if not href else (p.parent / href).resolve()
            if href and not target.exists():
                problems.append(f"{p.relative_to(out)}: missing {href}")
                continue
            if frag and target in ids and frag not in ids[target]:
                problems.append(f"{p.relative_to(out)}: #{frag} not in {target.relative_to(out)}")
    return problems
