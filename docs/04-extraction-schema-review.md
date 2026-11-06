# Extraction schema review — `vessel_issues_schema.json` vs. what the data needs

> Status: **proposal, for discussion**
> Date: 2026-09-15
> Reviews `docs/vessel_issues_schema.json` (the two-collection Mongo sketch) against `02 §2.5` (CaseFact v1),
> the earlier per-casefile extract contract (`casefile_schema.json`, KING ONE worked pair) and the 40-casefile
> export `casefiles_export_20260824_110543.json`. Numbers below are measured on that export unless stated.

---

## 0. Where the three schemas stand

| Schema | Unit | Role it should keep |
|---|---|---|
| `casefile_schema.json` (KING ONE pair) | one file per casefile: header + mails[] + attachments[] | the **evidence layer** — one record per mail and per attachment link, with `attachment_sha`, `kind_of_doc`, `content_bearing`. Maps 1:1 onto the new `vessel_issues_mail_attachments` collection. |
| `02 §2.5` CaseFact v1 | one blob per problem | the **extraction contract** — ids, evidence quotes, outcome vocabulary, gates. Maps onto the new `vessel_issues_incident_reports` collection. |
| `vessel_issues_schema.json` | two Mongo collections | the **storage layout** for the two above. It should not be a third vocabulary. |

Recommendation: treat the new file as the Mongo layout only, and make each field a straight copy of a field
that already exists in one of the other two. Where the sketch is thinner than those (see §2), it loses
something the design doc already argued for.

---

## 1. Fix before anything is written to Mongo

- Collection and field names carry typos that become permanent once a consumer reads them:
  `vessel_issues_mail_attachemnts` → `vessel_issues_mail_attachments`, `equeipment` → split (see §2.1),
  `incidnet report` → `incident_reports`. Repo convention (03 §11): `snake_case`, plural.
- `attachment_type` as served is unreliable — the server labels some `.eml` files `application/pdf`
  (memory: fetch-route finding). Store `declared_type` and `detected_type` (python-magic) separately.

---

## 2. Improvements to the attachment collection

Measured on the export: **1,979 attachment links** across 1,325 mails (696 mails carry attachments),
**1,963 distinct URLs but only 920 distinct file names**. On KING ONE, 166 links resolve to **53 distinct
SHA-256s**. The URL is not a dedup key; the byte hash is.

By extension: `.jpg` 640 · `.pdf` 515 · `.png` 506 · `.xlsx` 104 · `.eml` 55 · `.docx` 41 · `.zip` 26 ·
`.xlsm` 21 · no extension 20 · `.doc` 18 · `.xls` 12 · `.jpeg` 10 · `.addr` 8 · `.manbw-spaf` 2 · `.tar.gz` 1.

### 2.1 Fields to add

| Field | Why |
|---|---|
| `sha256` | dedup key (`OSIS-26-0043 Main engine spare parts.pdf` appears on 24 links; `image001.png` on 93). Digest once per sha, link many times — this is the `attachment_digests (sha256) UNIQUE` collection 03 §4 already specifies. |
| `content_bearing: bool` | 30 % of links are signature/inline junk by file name alone (`image0NN.*`, `image.png`, `CID_*`, `Outlook-*`, `.addr`); on KING ONE, 84 of 166 by content. Filename rule vs. content judgement on KING ONE: 162/166 agree, 2 dropped wrongly, 2 missed. Keep the flag so consumers never re-derive it. |
| `kind_of_doc` | `Service report`, `Statement of Facts`, `Invoice`, `Air waybill`, `Identity document – passport`, `Email signature / logo image` … The passport class matters: 10 passports in KING ONE alone — **classify, never extract**. |
| `maker`, `model`, `equipment_details` (three strings, `"NA"` unless *this* document states them) | one free-text `equipment` string cannot be joined to PMS / machinery, which is the only reason to recover it from attachments (01 §3.2). |
| `confidence` (0–1) | already in the KING ONE contract; needed to route low-confidence digests to review. |
| `extraction_method` | `pdf_text` \| `pdf_ocr` \| `vision` \| `xlsx_table` \| `docx_text` \| `eml_parse` \| `listed_only` \| `skipped`. Tells you which reader produced the summary when a digest is wrong. |
| `page_count`, `bytes`, `detected_type` | the routing inputs for §3; cheap, deterministic. |
| `extraction_version` | 03 §6 rule 3 — stamp every record. |
| `links[] = [{mailbodyId, casefileId, url}]` on the sha-keyed digest, **or** keep one row per link with `sha256` on it | either is fine; do not do both. |

`summary` and `tags` stay. `tags` should be a closed list (config), not free text, or they fragment
exactly like symptom wording does.

### 2.2 The mail layer is missing

The sketch has attachments and incident reports, nothing for the mail itself — yet `reference: [mailbodyId…]`
on every action points at mails. Every one of the 1,325 pages has a fetchable `link[0]` HTML page. Keep the
KING ONE mail record (`mail_id`, `pagenum`, `date`, `sender`, `sender_email`, `subject`, `title`, `summary`,
`kind_of_doc`, `confidence`, `source_url`) as a `vessel_issues_mail_digests` collection, or embed it in the
incident report. Never use `pages[].summary` from the export (markdown-bold LLM summaries, negative-outcome
language on 21/1,325 pages — memory: corpus-and-grain).

---

## 3. Improvements to the incident-report collection

The sketch stores `symptoms`, `equipments`, `problems` as bare strings and actions with a three-value
status. Compared with CaseFact v1 this drops the four things 02 §2.2 says are load-bearing:

1. **Provenance on every assertion.** A symptom string with no mail id cannot be cited (01 §10.6) or
   validated by the quote-substring check (02 §2.4). Each item should be `{id, text, mail_ids[], evidence}`.
2. **`unknown` as an outcome.** `success | failure | partial` has no value for "attempted, thread went quiet",
   which 01 §8.3 expects to be the largest bucket. The model will pick `success`. Use the agreed vocabulary:
   corrective/preventive/commercial/administrative → `resolved | partial | no_effect | made_worse | unknown`;
   diagnostic → `conclusive | inconclusive | no_information | unknown` (a test that rules something out is not a
   repair and must not count as one).
3. **Order and repetition.** `seq`, `occurrence_no`, `target` (cyl 5 vs cyl 3) on each action — without
   `target`, OASIS renewing valves on cylinders 2, 4, 5 reads as three failed retries (02 §3.6).
4. **The two ends of the evidence chain.** `asserted_in_mail` and `outcome_in_mail` per action (outcome
   usually lands mails later, in a different thread). Keep the ids; derive no numbers from them in the
   extractor (working agreement).

Also missing, and cheap to add now:

- `problem_statement` (one line) + `other_issues[]` — a casefile is not one problem: 21/40 casefiles have ≥4
  subject stems; `HFO storage tank leak` holds ≥7 unrelated problems. Either allow several incident reports
  per casefile (`blob_id`) or carry `other_issues[]`.
- `gates[]` (quotation, warranty, PO, prepayment, agent) — ~70 % of OASIS elapsed time; the sketch has nowhere
  to put "PO + SGD 8,500 prepayment blocked deployment two weeks".
- `organisations[]` with `role` — resolvable from sender domains with no model call (366 `synergyship.com`,
  15 `everllence.com`, 23 `lethagencies.com` …).
- `equipment[]` as `{name, equipment_type, maker, model, source, confidence}` so it joins to PMS.
- `extraction_version`, `validation_flags[]`, `needs_review`.
- `"related_problems and equipment and symptoms"` (structure TBD) → the simplest thing that works:
  `symptom_ids[]`, `equipment_ids[]`, `problem_ids[]` on the action, referencing the ids above.
- `casefile_log` — define it as the **ordered event log**: `[{date, mail_id, one line}]`. That is what the
  HTML brief renders and it de-duplicates `casefilePlans[]` rolling snapshots (01 §8.6).

---

## 4. Attachment handling — what the comment line says, and what the data says

The comment proposes: Excel → raw text to LLM; Word/PDF ≤15 pages → page images to LLM; images → LLM.

| Type (links) | Proposed | Recommend instead | Why |
|---|---|---|---|
| PDF (515) | every page → image → vision | **text layer first** (PyMuPDF); vision only when text/page < 200 chars (scanned) or the doc is a drawing/photo report; cap vision at N pages, text has no cap | vision on every page is the most expensive route in the pipeline; most service reports, SOFs, invoices, delivery notes have a text layer |
| DOCX (41) / DOC (18) | page images | `python-docx` text; `.doc` via LibreOffice headless → docx | Word files have no "pages" until rendered |
| XLSX/XLSM/XLS (137) | raw text | sheet → markdown table, visible sheets only, row cap per sheet, drop empty/formula-only cells; `.xls` via xlrd or LibreOffice | SQRR reefer forms and RA sheets are large; raw dump blows the context and costs tokens for blank cells |
| JPG/PNG (1,156) | vision | sha + size + pixel-dimension filter **first** (under ~15 KB or ~200 px → junk), then one vision call per surviving distinct sha | ~600 are signature images by filename alone; the same photo repeats across replies |
| EML (55) | — | stdlib `email` parse as a nested mail; recurse into its attachments | forwarded vendor mails carry the customs/OEM content |
| ZIP / TAR.GZ (27) | — | **list, do not open**: record file name, member list, date (MOP dumps, decarb reports, code books, `sysLog_*.tar.gz`) | "a dump was taken on this date" is the signal (02 §2.3) |
| `.addr`, `.manbw-spaf` (10) | — | record as `telemetry / setting file`, no parse | DG setting files; binary |
| no extension (20) | — | `CID_*` → junk; else sniff with python-magic | |
| Passports / IDs | — | classify only | PII; never extract numbers |

Tier 0 (sha cache) before any of this: at fleet scale the same OEM PDF recurs across hundreds of casefiles.

---

## 5. Tools required for extraction

Deterministic (no model):

| Step | Tool |
|---|---|
| fetch mail HTML + attachments | `httpx` (async, retries). **Open question 02 §8.5 still open**: `assets-prod.siya.com` is blocked from the Cowork sandbox and the local shell (proxy 403 on CONNECT); only a browser reaches it. Confirm it is reachable from the k8s namespace before building the fetcher. |
| mail HTML → text, quote stripping | `selectolax` (or bs4+lxml), `talon` + regex for quoted replies |
| hashing / typing | `hashlib` sha256, `python-magic`, `Pillow` (image dims) |
| PDF | `PyMuPDF` (text + page render); `ocrmypdf`/`tesseract` when the text layer is thin |
| Office | `python-docx`, `openpyxl` + `pandas`, `xlrd` for `.xls`; `libreoffice --headless` for `.doc`/legacy |
| .eml | stdlib `email` |
| archives | `zipfile` / `tarfile` — listing only |
| contract | `jsonschema` + Pydantic models in `src/contract/`; the quote-substring validator |
| store | `motor` / `pymongo` (03 §4: three connections, `source` read-only) |
| resolution | `rapidfuzz`, `sentence-transformers` (alias table first) |

Model calls (exactly three kinds):

| Call | Notes |
|---|---|
| attachment digest | one structured call per surviving distinct sha; vision content parts for images/scans. `LLMClient._prepare_messages` already wraps content as parts, so image parts drop in. |
| windowed extraction | stride 5 / window 6, carried state (02 §2.1); on-disk cache keyed on (prompt version, input hash) |
| consolidate | one call over the state |

Two implementation notes on the existing client: prefer `response_format={"type":"json_schema","strict":true}`
over `json_object` so the incident report is schema-valid at the source, not after repair; and add
`src/llm/cache.py` before the first backlog run — re-running 5,000 casefiles because a prompt changed one
line should not re-pay for unchanged windows.

---

## 6. Other notes

- **Worked example.** KING ONE (218 pages) is a delivery/logistics circular, `UICategory ['purchase',
  'General', 'maintenance']`, with a defect only in its `other_issues`. It is the right stress test for the
  attachment layer (166 links, 53 files, 84 junk) and the wrong one for the incident report. Use OASIS
  `Knocking sound from main engine exhaust valve` (46 pages) for the incident-report worked example; the phase-0
  exit gate in 02 §6 is written against it.
- **The casefile boundary cuts threads.** The OASIS exhaust-valve saga spans four casefiles; 3 subject lines
  appear verbatim in 2 casefiles each; 0 mails do. `casefileId` on the incident report is provenance;
  the extraction unit is still the problem (02 §2.4 "split into multiple blobs").
- **Defect filter.** `'defect' in UICategory` keeps 34/40, `tag` 36/40, union 40/40 — it is not a filter.
  Part 1 is a black box (02 §0); do not re-implement it from these labels.
- **Volume for the cost model.** ~50 links per casefile; ~50–70 % survive the junk filter (KING ONE 82/166, export
  ~70 % by filename); about half of the survivors are distinct after sha dedup within a casefile (KING ONE:
  82 content-bearing links, 45 distinct files). Fleet-wide reuse of OEM documents will lower that further.
- **Repo state.** The repo currently holds docs 01–03, config, the LLM client and its tests. Earlier
  sessions' docs 04–10 (field inventory, dedup layer, SQLite schema) are not in this checkout; this file
  restarts the numbering at 04. The KING ONE `casefile_schema.json` and the per-casefile HTML renderer are
  also not in the repo — commit them under `schema/` and `scripts/` if they are still the evidence-layer
  contract.
