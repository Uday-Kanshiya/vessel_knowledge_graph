# Casefile browser UI — POC design

> Status: **built (POC)**, 2026-10-16; proposed 2026-10-08. §9 is the as-built record; §1–§8 are the design it implements.
> Run: `python scripts/build_site.py --check` → `data/site/index.html` (open the file, or `--serve 8080`).
> Input: the 10 `casefile_extract.v2` records (`data/extractions/<id>/record.json`, packaged in
> `data/casefile_extracts_20261005_152645/`), their `windows/wNNN.json` and `run_log.json` audit files, and the
> `cache/mail_bodies` / `cache/attachments` stores (10 §output layout). Field semantics: 05, 06. Pipeline: 08, 11.

---

## 1. What it is

Two audiences, one build:

| Audience | Needs | Pages |
|---|---|---|
| User (superintendent / tech team) | find casefiles by what went wrong and on what; read one casefile as a page; check a claim against its source mail | search, casefile page, mail page |
| Admin (us, debugging extraction) | see how a record was built window by window; see the raw→canonical name tables; see what the validator flagged | journey, lookup tables, review queue, raw JSON |

Rule carried over from 03/08: the UI is a **view**. It never writes to a record, never computes a number
the extractor was told not to extract into the record, and shows only what the record cites. Derived counts
(actions per casefile, date range, outcome tallies) are computed at render time, never stored (working
agreement: derivable numbers are views).

---

## 2. Approach: static site built from the run store

```
data/extractions/<id>/{record.json, windows/*.json, run_log.json}
data/cache/{mail_bodies,attachments}/
        │   scripts/build_site.py   (one pass, idempotent, re-run after every extraction run)
        ▼
data/site/
  index.html                       search page (client-side index, no server)
  search_index.json                one entry per casefile: searchable text + facet values
  casefile/<casefile_id>.html      the casefile page; anchors #e1 #f3 #a12
  mail/<mail_id>.html              mail body from cache, with cited quotes highlighted
  attachment/<sha>.html            digest + extracted text, which casefiles link it
  admin/
    lookup.html                    raw name → type tables (§5.1)
    review.html                    every validation flag across casefiles, grouped by flag kind
    journey/<casefile_id>.html     window-by-window build of one record (§5.2)
  assets/{site.css, app.js}        hand-written, no CDN, no node toolchain
```

Why static for the POC:
- Zero infrastructure; the output is a folder. It zips and hands over exactly like `package_records.py`
  does today, or serves from the nginx image already in `k8s/`.
- Everything is derived, so a bad render is fixed by re-running the build — no state to migrate.
- 10–500 casefiles fit a client-side index comfortably (one JSON file, ~2–5 KB per casefile).

Why it still scales (§7): rendering is `template(record)`, and the search index is `project(record)`. Both
read a record through one small interface (`RecordSource`: filesystem now, `vessel_issues_incident_reports`
later). Moving to a server means changing the source and the search backend, not the templates.

Stack: Python + Jinja2 (repo is Python; no second toolchain), hand-written CSS, and a ~200-line hand-written
search in `assets/app.js` (linear scan over the weighted fields — no vendored library; MiniSearch or FTS5 is the
swap when the index outgrows a scan). Code: `src/site/` (render) and `src/search/index.py` (projection), entry
point `scripts/build_site.py`. Output dir `data/site/` (gitignored like the rest of `data/`).

---

## 3. Search page

Google-style result list; one result per casefile.

**Result card**

```
OASIS · Knocking sound from main engine exhaust valve                 13 Mar 2026 → 02 Apr 2026
main_engine · 6 faults · 29 actions · 3 resolved · 1 partial · 25 unknown
…knocking sound from main engine exhaust valve **cylinder no. 4** … → renew exhaust valve (cyl 4): resolved
```

- Title = `problem_statement` (export title, verbatim). Vessel from `vessel_name` (display only; identity is
  `casefile_id`, link is `casefile/<casefile_id>.html`).
- Date range = min/max over `faults[].first_observed_at`, `action_logs[].date_performed|date_reported`.
- Snippet = the matched field with the hit highlighted; prefer a fault symptom, then an action, then a finding.
- Chips = distinct `equipment[].equipment_type`, outcome tally. `needs_review` badge shown in admin mode only.

**Indexed fields, weighted** (so a hit on the title outranks a hit in an invoice-line action):
`problem_statement` 5 · `faults[].symptom`, `faults[].alarms[]` 4 · `faults[].suspected_cause`,
`problems[].*` 3 · `action_logs[].action`, `action_logs[].finding` 2 · `equipment[].name|maker|model`,
`action_logs[].parts[]` 2 · `action_logs[].outcome.evidence_quote`, `provenance.evidence_quote` 1.

**Filters (facets)** — principle (2026-10-21): **a facet lists canonical ids only; wording is never a facet
value.** A facet must be bounded by construction — a fixed vocabulary a human maintains — never by how much
data there is. Wording stays searchable through the text index (symptom wording at weight 4) and visible on the
admin lookup page. The canonical layers come from the resolver (`LookupStore`, docs/13); the site build opens
it read-only (`find()`, nothing minted) and the index records `labels` (id → display name), `parents` and
`facet_parent_vals` so the UI can nest.

| Facet | Source | Bounded by | Parent |
|---|---|---|---|
| Equipment | `equipment[].equipment_type` | the schema enum | — |
| Equipment · standard | `equipment[].name` → `equipment_std` (standard id) | the resolver's standards; a name with no alias yet is `__unresolved__` (greyed, last). Shown only once an equipment type is selected, listing only standards seen under that type | Equipment |
| Symptom | `faults[].symptom` → resolved standard → `symptom_family` (family key) | `config/symptom_families.csv` (~20 rows, human-edited; families are never minted). No family → `unclassified` (greyed, last). The list fits without scrolling | — |
| Symptom · on / where | `faults[].symptom` → symptom table `parts.component` → `symptom_on` (the component's **part** standard id, shared with `action_logs[].parts[]`) and `parts.location` (a `config/location_terms.csv` term, shown as `in <location>`) | the resolver's part standards and the location table; shown only once a family is selected, listing only the components / locations named by that family's symptoms. A component with no part standard yet is `__unresolved__` (greyed, last). The symptom **standards are not shown** in the side filter (2026-11-02): they stay in the index (`symptom`, `symptom_broad`, `f.symptom=` in a URL still works) and on the admin lookup page, where they are reviewed | Symptom |
| Symptom · condition | `parts.condition` of the symptom wording → `condition` | `config/condition_terms.csv`; present only when some value has ≥ 2 casefiles (13 §8) | Symptom |
| Outcome | `action_logs[].outcome.value` | enum: resolved / partial / no_effect / made_worse / conclusive / inconclusive / unknown | — |
| Action kind | `action_logs[].kind` | enum: corrective / preventive / diagnostic / commercial / administrative | — |
| Performed by | `action_logs[].performed_by_role` | enum | — |
| Vessel | `vessel_name` | the fleet (audit field, fine as a display facet, not a join) | — |
| Year | from the date range | the calendar | — |
| Has attachments | any `provenance.attachment_shas` non-empty | boolean | — |

Counts are computed within the current result pool exactly as before (AND across facets, OR within);
a child value is counted only when one of its parent values is selected, and clearing a parent selection
clears its child selections. URL state carries ids (`?f.symptom=<id>`), labels render through `labels`.
Index schema `site_search_index.v2`. **Fallback:** `scripts/build_site.py --no-lookup`, or a missing resolver
DB, builds a v1 index with the wording Symptom facet (`canonical: false`) and puts a banner on the search page:
"canonical facets unavailable — run scripts/build_lookup.py". `equipment_name` is no longer in the index.

`UICategory` / `tag` are **not** facets (03: hints, never filters). Show them on the casefile page under
"export metadata" only.

---

## 4. Casefile page

One HTML per record, laid out in the order a reader thinks, not the order the JSON is stored:

1. **Header** — title, vessel + IMO, date range, outcome tally, link to `export_meta.casefile_link`.
2. **Equipment** — one card per `equipment[]`: name, type, position, maker/model/serial, details; its faults.
3. **Faults** — per fault: symptom, alarms, position, `first_observed_at`, suspected cause, linked problem
   (`problems[]` rendered inline when `problem_id` set, with `asserted_by`), `recurrence_of` link.
4. **Action log** — the main body. Table ordered by `seq`, grouped under its fault: seq · date · kind ·
   action · target · by (role / advised-by org) · parts · outcome (value + verification) · finding.
   Every row has anchor `#a12`.
5. **Evidence** — every `evidence_quote` is a link: `mail/<mail_id>.html#q-<action_id>` (or
   `attachment/<sha>.html`). The quote is also shown on hover. This is the trust feature: the validator already
   proved each quote is verbatim in the cited body, so the mail page can highlight it reliably.
6. **Footer (collapsed)** — `export_meta` (category, UICategory, tags), `extraction_version`,
   `validation_flags`, "view raw JSON".

Admin mode adds: `needs_review` banner, per-row flag badges (`quote_not_found:a4`,
`same_quote_action_outcome`), and a "journey" link.

**Mail page** — rendered from `cache/mail_bodies/<mail_id>.json` text (quotes stripped, the validator's
source): subject, date, from/to (as cached), body; cited quotes wrapped in `<mark id="q-a12">`; sidebar lists
which casefile rows cite this mail. **Attachment page** — digest (`kind_of_doc`, summary, tags, maker/model),
extracted text, `links[]` to every casefile/mail that carries it.

---

## 5. Admin pages

### 5.1 Lookup tables (`admin/lookup.html`)

Since 2026-10-21 the page reads the **resolver** (`data/resolution/lookup.sqlite`, docs/13), not the records;
the records only say which casefiles carry each spelling (`views.resolver_tables`):

- **Needs attention** at the top: ONE collapsible table (`<details class="queue">`, open when ≤ 10 rows),
  one row per standard that needs a human, merged by standard id and ranked by hits desc, then symptoms
  first (they shape the facets), then key. Reason chips: `unreviewed` (minted by the rules — approve, merge or
  pin a spelling) and `no family` (a symptom the facet shows as *unclassified*); a standard that is both appears
  once with two chips. The summary line carries the two counts and per-kind toggle chips (multi-select, all on)
  that hide rows of other kinds; the first 10 rows are visible, the rest `hidden` until *show all*. Spellings are
  truncated at 3 with `+n`. A *copy fix* button puts the generated CSV row (`kind,raw_term,standard_key,family_key`)
  on the clipboard (`navigator.clipboard`, with a hidden-input fallback for `file://`; brief *copied* state, no
  `alert()`), because editing is still CSV + `scripts/build_lookup.py`, not the UI. `views.resolver_tables` returns
  `queue`, `queue_counts {unreviewed, no_family, total}`, `queue_by_kind`; the page loads `app.js`, whose search
  code is guarded on the index element and whose `initQueue()` runs only when the queue exists (so `--no-admin`
  builds are unaffected).
- **Equipment:** `equipment_type` → standard (display name, a view) → raw spellings (hits · method
  seed / normalised / new_term / human) → casefiles.
- **Symptom:** family → standard (display, and the *method* of its family assignment: human / seed /
  inherited / rule / none) → raw spellings → casefiles; families with no standard yet are listed; the
  `unclassified` pseudo-family comes last, greyed.
- Parts (standards exist, no family layer yet), performed-by roles and advising organisations stay
  record-derived.

**Fallback** (`--no-lookup`, or no DB): the earlier record-derived tables — `equipment_type` → raw names,
and the normalised-symptom groups with a narrower group indented under its broader group (`⊂ broader` chip,
link note / reviewer / date column; counts per group, nothing rolls up; dropped links listed above the table).

### 5.2 Journey (`admin/journey/<casefile_id>.html`)

"How did this casefile become this page." Everything needed is already on disk:

```
export            218 pages · 166 attachment links · UICategory […] · tags […]
  ▼
window w000  pages 0–4   mails: 5 (ids…)   attachments: 3 links → 2 sha hits, 1 fetched → digested (pdf_text)
             delta: +e1 +f1 +a1 +a2        updates: —          flags: —         tokens 4.1k / 1.2k
window w001  pages 5–9   …
             delta: +a3 +a4                updates: a1.outcome unknown→partial (p.7)   flags: outcome_downgraded:a4
  …
consolidate   merged a7+a9 → a7 (occurrence_no 2) · fault f4 ≈ f2 kept separate (…) · seq assigned
validate      quote_not_found:a4 · over_cap:29>25 → needs_review
store         record.json · packaged as OASIS__Knocking…__69b38ef8.json
```

Sources: `windows/wNNN.json` (what the model saw, what it returned, applied ids, repair flags, record after),
`run_log.json` (tokens, attachment stats, consolidate log, `mail_aliases`), `validation_flags`.

Two views on the same data:
- **Timeline** (above): one row per window, expandable to the prompt summary it was given and the raw delta.
  Each row carries a collapsed **"what the model saw"** panel (added 2026-10-21, docs/14) built from the
  window's `inputs` block and its `wNNN.prompt.txt` sidecar: a memory line (rows per family in the summary the
  model could update, `next_free_ids` it would mint from), a mails table (page · date · from · subject · body
  chars · tags `context only` / `twin of …` / `fetched now` · link to the mail page when cached; skipped twins
  greyed with the twin id, so "why wasn't this mail used" is answerable), an attachments table (name · sha link ·
  status chip cache hit / fetched + digested / junk / fetch error / not processed · read method · sent ✓/–, with
  the digest summary as sent on hover), this window's tokens in/out and attempt count, the `## CORRECTION`
  feedback of a retried window, the full raw prompt and each attempt's raw response as pretty JSON. Audit files
  written before the capture existed show one muted line instead. Sources: `RecordSource.windows()` →
  `inputs`, `RecordSource.windows_prompt()` → the sidecar; `has_page` flags from the cached mails / digests.
- **Row lineage**: pick any `a12` / `f3` / `e1` → which window minted it, which windows touched it (field,
  old → new, citing mail), and its final validator status. This is the view that answers "why does this action
  say `unknown`" in one click.

Also a **flag queue** (`admin/review.html`): every `validation_flags[]` entry across casefiles, grouped by
kind (`quote_not_found`, `quote_in_uncited_attachment`, `over_cap`, `window_rejected`, …), each linking to
the row anchor and its journey.

---

## 6. Build order for the POC

1. `src/site/source.py` — `RecordSource` with `list_ids()`, `record(id)`, `windows(id)`, `run_log(id)`,
   `mail(mail_id)`, `attachment(sha)`; filesystem implementation over `RunStore` + caches.
2. `src/search/index.py` — `project(record) → {id, title, vessel, text fields by weight, facets, dates}`;
   writes `search_index.json`. Symptom normaliser here (shared with the future resolver).
3. `src/site/render.py` + `templates/` — casefile, mail, attachment pages. Golden-file test on KING ONE.
4. `index.html` + `app.js` — MiniSearch over `search_index.json`, facet sidebar, result cards, `?q=`/`?f=`
   in the URL so searches are shareable.
5. Admin: lookup → review → journey (journey last; it is the most work and the least user-facing).
6. `scripts/build_site.py` wires 1–5; `--serve` runs `http.server` on `data/site/` for local checking.

Tests: render every record in the store without error; every anchor referenced by a link exists; every
`mail_id` cited has a mail page (or a listed "body not cached" placeholder); search index round-trips.

---

## 7. Scaling path

| Stage | Trigger | Change | Unchanged |
|---|---|---|---|
| 1 — static (POC) | now | — | — |
| 2 — served | records in Mongo (`vessel_issues_incident_reports`, 07) or > ~500 casefiles | `RecordSource` → Mongo; FastAPI renders the same Jinja templates on request; search → SQLite FTS5 or Mongo text index with the same `project()` output; facets precomputed per run | templates, index projection, URL scheme |
| 3 — resolved | resolver built (02 Part 3) | facets switch from raw names to canonical ids; lookup page reads resolver tables; "related casefiles" block on the casefile page (same vessel + equipment_type, shared gc_ref, thread merges) | everything above |
| 4 — semantic | retrieval eval exists (02 Part 4) | embedding search alongside keyword; admin side-by-side compare | pages |

Access: for the POC `admin/` is just a path. Before anything is exposed beyond the team, put admin behind the
ingress auth already used for the cluster; do not build auth into the site.

---

## 8. Decisions taken when building (were "needed before building")

1. First consumer — both were built in one pass; the POC is small enough. Polish went to search and the
   casefile page; the admin pages are plain tables.
2. Delivery — a folder. `data/site/` opens from the filesystem (the index is embedded in `index.html`, so no
   fetch is needed) and serves as static files; nothing in it assumes a host. Not decided: whether it goes into
   the `k8s/` nginx; nothing changes in the build either way.
3. Mail pages — built from the cached bodies, because the highlighting needs the exact text the validator read.
   If mail content must not be shown, build with `--no-mail` (not implemented yet; one flag in `build()`) and the
   casefile page falls back to the `assets-prod.siya.com` link it already carries.

---

## 9. As built (2026-10-16)

| | |
|---|---|
| Entry point | `scripts/build_site.py [--data-dir D] [--out DIR] [--ids …|--set FILE] [--include-incomplete] [--no-admin] [--check] [--serve PORT] [--symptom-links FILE] [--lookup-db FILE | --no-lookup]` |
| On / where sub-facet (2026-11-02) | the side filter no longer lists symptom standards under a selected Symptom family: `symptom_on` (component part-standard id · location term, `src/search/index.py` `ON_FACET`, parent `symptom_family`) replaces it in `app.js`; the standards stay in the index and on `admin/lookup.html` |
| Canonical facets (2026-10-21) | index `site_search_index.v2`: `equipment_type ▸ equipment_std`, `symptom_family ▸ symptom` (ids), `labels`, `parents`, `facet_parent_vals`, `pseudo_values`; `app.js` nests child facets under a selected parent and renders labels; `admin/lookup.html` reads `views.resolver_tables()` and shows one merged, ranked "needs attention" queue with kind toggles and copy-fix buttons (`initQueue()` in `app.js`); `--no-lookup` falls back to v1 wording facets with a banner (§3, §5.1) |
| Code | `src/site/source.py` (`RecordSource` protocol: `list_ids`, `record`, `windows`, `windows_prompt(casefile_id, index)` → the window's `.prompt.txt` or `None` (added 2026-10-21), `run_log`, `mail`, `attachment`; `FileRecordSource` over `RunStore`, `DictRecordSource` for tests) · `src/site/views.py` (dates, tallies, symptom normaliser, lookup tables, journey + lineage + `window_inputs_view`, flag parsing, mail header split) · `src/site/symptom_links.py` · `src/search/index.py` (`project()`, `build_index()`) · `src/site/render.py` (`build()`, `highlight_body()`, `check_links()`) · `src/site/templates/*.html.j2` · `src/site/assets/{site.css,app.js}` |
| Tests | `tests/unit/test_site_build.py` — 18 tests (12 original + 3 "what the model saw" + 3 canonical facets: ids not wording / labels / parent map / the bound, `symptom_broad` as ids, search page with and without a resolver), `tests/unit/test_symptom_links.py` — 10, `tests/unit/test_resolution_lookup.py` — 44 (symptom kind, families, precedence); in-memory source and in-memory `LookupStore`, no network |
| Dependency added | `jinja2>=3.1` (pyproject) |
| First build | 10 records → 10 casefile pages, 20 mail pages (the cached bodies present in the sandbox; a full build on the run store makes one per cited mail), 1 attachment page, 3 admin pages + 10 journeys; 1.4 MB; `--check` clean |

What the pages do, beyond §3–§5:

- **Quote → mail.** A row that cites several mails carries one quote; the build locates it (exact, then the
  original-script half of `原文 [EN: …]`, then whitespace-tolerant) and links the quote to the mail where it was
  found, not the first mail listed. Quotes found nowhere in the cached bodies stay on the first mail and show
  as *not in body* in that mail's sidebar — in practice these are quotes the validator found in an attachment
  of the mail (e.g. a fee schedule PDF), which is correct, not a defect.
- **Admin mode.** The `admin` toggle (top right, remembered per browser; `?admin=1` also works) reveals
  `needs_review`, per-row flag chips, supporting quotes, and the journey links. The admin pages themselves
  force it on. `--no-admin` omits the pages and every link to them.
- **Facets.** Each facet is a collapsed header (`▸ EQUIPMENT · 7`); clicking opens a scrollable list
  (230 px) of checkboxes, multi-select within a facet (OR), AND across facets. A facet with a selection
  stays open and shows the selection count. Values are sorted by count within the current results.
- **Search.** AND over the typed tokens; whole-word hit scores the field's full weight, substring 0.6×; a
  casefile's score is the sum of its best field hit per token. Facet counts are computed within the current
  query and the *other* facets' selections, so a count is always the number of results clicking it would give.
  `?q=…&f.<facet>=…` is the URL state, so a search is shareable.
- **Journey.** Reads `windows/wNNN.json` for the full view (delta ids, updated fields, repair flags, model
  note, prompt size) and falls back to `run_log.runs[].windows` (ids only) when the audit files are missing.
  Lineage: minted-in window = first `applied.added` containing the id; touched = each `raw_response.update`
  row for the id with its changed fields.
- **Not built, by design:** editing anything; auth; a server; the resolver's tables (the lookup page is derived
  from the records and says so). Open items: `--no-mail`; a `related casefiles` block once threads exist (§7
  stage 3); the symptom normaliser is the POC stand-in and should be replaced by the resolver's, not extended.
