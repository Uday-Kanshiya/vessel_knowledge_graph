# Running the v2 extraction — the 10-casefile test set through the API

> Status: **operational guide**, 2026-09-30
> Runs the pipeline of `08-processing-pipeline.md` on the test set of `09-extraction-test-set.md`, producing
> `schema/casefile_extract.v2` records through the LLM API (`src/llm/llm_client.py`), **5 mails per model
> call**, resumable, with the operator deciding which casefiles and how many windows per invocation.

---

## 1. What runs

```
scripts/extract_casefiles.py ── selection ──► src/corpus/export.py       (export JSON → Casefile, pages sorted)
                              ── per casefile ► src/extraction/runner.py  (08 §1 ①–⑤)
     ① header      record := casefile_id, imo, vessel_name, problem_statement = export title, export_meta
     ② windows     pages cut into windows of 5 NEW mails (no overlap)
                     a. mail bodies   pages[].link[0] → HTML → text (quotes stripped) → cache by mail_id
                     b. attachments   URL → bytes → sha256 → cache; junk filter; byte-sniffed typed reader;
                                      OCR of scanned PDF pages (vision model, per page); identity documents
                                      classified, never read; ONE digest call per content-bearing file
                                      (cached by sha, reused across casefiles)
                     c. window call   id-keyed SUMMARY of the record so far (+ next free ids) + 5 mails +
                                      their digests → JSON DELTA (add / update)
                                      code applies it: ids checked, evidence required, enums coerced,
                                      outcome without quote → unknown, out-of-window citations dropped;
                                      non-English quotes get "original [EN: translation]" (one batched
                                      call per window for any the model left bare)
                     d. checkpoint    record.json written, progress.last_pagenum_processed = window's last page
     ③ consolidate one call over the record: duplicate merges + repeat groups; code assigns seq / occurrence_no
     ④ validate    schema · ids resolve · every quote found in cited mail/attachment text · sha join · cap
     ⑤ store       data/extractions/<casefile_id>/record.json (+ windows/*.json audit, run_log.json)
```

Three kinds of model call and no other (04 §5): window update, attachment digest, consolidate. The model
returns changes, never a rewritten record, so nothing from an earlier window can silently disappear
(02 §2.2 rule 1); `assert_no_dropped()` still runs after every merge.

### 1.1 The 5-mail window, and memory across windows

`EXTRACTION_WINDOW_SIZE=5` is the cap on mails per call; `EXTRACTION_WINDOW_OVERLAP=0`, so every window is 5
new mails. Context across windows does not come from re-sending a mail (the "window 6, stride 5" of 02 §2.2
and 06 §4) but from the **record summary** at the top of every window prompt: each equipment, problem, fault
and action found so far, with its id and the fields needed to recognise it (symptom + equipment, action +
target + current outcome, maker/model), plus `next_free_ids` per family. The model either **updates a row by
id** (an outcome for `a4`, a concluded cause on `f2`, maker/model on `e1`) or **adds a row with the next free
id**. Provenance and quotes are not in the summary — they are already on the stored rows, and the model can
only cite mails of the current window. Prompt: `src/prompts/window_update/v4.md` (v3 added: a crew member's
attribution is a `suspected_cause`, not a problem row; logistics, invoices, expense approvals and operational
events are not actions; one instruction issued in steps is one action; non-English quotes carry an
`[EN: …]` translation).

### 1.2 Attachments: sniffing, OCR, identity documents, translation

- **Byte sniffing.** A file is routed by its magic bytes before its name: the server labels forwarded `.eml`
  files as PDF (04 §1; three on KING ONE), and those now go to the mail parser.
- **OCR.** `PDF_OCR_BACKEND=vision` (default) renders each scanned page with PyMuPDF and has the chat model
  transcribe it verbatim (`src/prompts/ocr_transcribe/v1.md`), one call per page, capped at
  `PDF_OCR_MAX_PAGES=10` per file. The transcription becomes the document's `extracted_text`, so quotes
  from a scanned SOF are checked against the same text the model read; `extraction_method` is `pdf_ocr`.
  `tesseract` is available when pytesseract and a binary are installed; `off` leaves scans `listed_only`.
  A cached `listed_only` PDF digest from a run without OCR is upgraded on the next run, without refetching.
- **Identity documents** (passports, seaman's books, visas, certificates) are recognised by file name, by
  transcribed content, or by the digest model, and stored as `kind_of_doc: Identity document`,
  `extraction_method: skipped`, no text. Nothing from them is ever sent onward.
- **Translation.** An evidence quote in another script is stored as `原文 [EN: translation]`. The window
  prompt asks for it; `translate_missing()` adds it with one batched call per window when the model left a
  quote bare; the validator strips the bracket and matches the original verbatim. `--no-translate` turns
  the fallback off.

`--overlap 1` restores the one-mail overlap (the v1 prompt design; the repeated mail is marked "context only").

| Casefile | Mails | Windows (5/0, default) | Windows (5/1) |
|---|---|---|---|
| KING ONE | 218 | 44 | 55 |
| Crane incident | 87 | 18 | 22 |
| HFO tank leak | 63 | 13 | 16 |
| Knocking sound | 46 | 10 | 12 |
| AE alarm | 41 | 9 | 10 |
| Hyper mist | 36 | 8 | 9 |
| Technical forms | 21 | 5 | 5 |
| SCR relay | 12 | 3 | 3 |
| Fuel pump | 7 | 2 | 2 |
| OMD sensor | 2 | 1 | 1 |
| **total** | **533** | **113** | **135** |

### 1.3 What is kept, what is merged (v4)

- **Actions are never merged during the windows.** An action done again is a new row. One instruction issued
  in steps (running-in at 136, 145, 152 rpm) is one row whose text states the steps.
- **Consolidate merges only duplicates of one event.** A merge the model proposes is refused by code when
  the two rows state different `date_performed` values, or when either lacks one and their mails are more
  than `CONSOLIDATE_MERGE_MAX_DAYS` (3) apart. A refused pair is kept as a repeat group: both rows stay and
  get `occurrence_no` 1 and 2. Every merge and refusal is in `run_log.json`.
- **A returning symptom is a new fault row.** When the same symptom on the same equipment is reported again
  after an action on the earlier fault was stated as resolved, or after a stated gap of normal operation,
  the extractor adds a new `f` row with `recurrence_of` pointing at the earlier fault (optional field added
  to the v2 schema). The new row has its own dates, evidence and actions. An open problem discussed over
  many mails stays one fault.
- **Kept on purpose**: one commercial row per guarantee claim or purchase order, with the delivery status as
  its outcome; checks a service report lists with results (deflections, backlash, sea trial) as diagnostic
  rows; consequences for the vessel (transit cancelled, held at anchorage) as administrative rows.
- **Wrong links can be corrected.** An update may send `fault_id` to move an action to the right fault; the
  window audit records `fault_id_corrected:a13:f1>f3`.
- **Citation repair.** Before validation, a quote that is verbatim in an attachment of the casefile the row
  did not cite (the scanned copy quoted, the text copy cited) gets that sha added, with a soft flag
  `quote_in_uncited_attachment:<row>:<sha8>`. Soft flags do not set `needs_review`. A quote found nowhere
  is still `quote_not_found`.

### 1.4 Evidence and de-duplication rules (v5, after the 10-casefile review)

Four defects from the first full run, and where each is now handled:

| Defect | Cause | Prompt (window v5) | Code |
|---|---|---|---|
| One sentence proves both an action and its result (17 % of actions) | the source sentence carries both halves | action quote and outcome quote are different clauses of it | `split_shared_quotes()` cuts the ones still copied whole, one batched call per window, both parts verified verbatim; leftovers get the soft flag `same_quote_action_outcome` |
| Several action rows share one quote | one event was written once per fault | one event is one row; other faults go in `also_fault_ids` | `collapse_fanout()` at consolidate: same mail + same quote + same kind + similar text + different faults → one row |
| One quote cited under several attachment hashes | an update's attachment was unioned into the row's hash list | one quote cites the one document it was copied from | update evidence lives in `provenance.supporting[]` (own mails, sha, quote, fields); `prune_shas()` drops cited shas that do not hold the quote |
| Two faults read alike | the place (cylinder, side) sat only in the symptom text | `position` always filled; same symptom + same place = same fault, or `recurrence_of` if it came back | look-alike pairs on one equipment are judged at consolidate (same / recurrence / distinct); merges are refused when numbers or sides differ |

Also added, all general rather than casefile-specific:

- **Similarity never decides alone.** Every merge (faults, duplicate actions) passes a number-and-side guard:
  "cylinder 3" and "cylinder 6" score 0.97 on tokens and are different places (`src/extraction/textsim.py`).
- **Twin mails.** A mail stored twice in a casefile (two ids, same body) is shown to the model once; the copy
  is recorded in `run_log.runs[].mail_aliases` and cannot be cited as a second source.
- **Dates.** `date_performed` later than the reporting mail (one day of slack for vessel-local vs UTC) is a
  plan, not a performance: it is left null and flagged in the window audit. `outcome_before_action` is a
  validator flag.
- **Problems.** A problem row that only repeats its fault's symptom is flagged `problem_restates_symptom`.
- **Update evidence is validated.** Quotes in `supporting[]` go through the same verbatim check as the
  primary quote; before v5 they were kept only in the window audit files.

Schema additions are optional fields (`provenance.supporting`, `action_logs[].also_fault_ids`), so earlier
records stay valid. Left to the loader/resolver on purpose: canonical names (`M/E` vs `main engine`),
`position` vocabulary, and the equipment key across casefiles (02 §3.9) — the extractor records the
documents' wording.

## 2. Setup

```
cp .env.example .env            # fill LLM_API_KEY (and provider/model); set CASEFILE_EXPORT_PATH
uv sync --extra attachments     # or: pip install pymupdf python-docx openpyxl pillow
pytest -m "not integration"     # no credentials needed
```

`CASEFILE_EXPORT_PATH` points at `casefiles_export_20260824_110543.json` (not committed — it is company
mail). `--export PATH` overrides it per run. The mail-body and attachment host `assets-prod.siya.com` was
reachable from the local shell on 2026-09-30 (02 §8.5 is answered for local runs; k8s untested).

## 3. Controls — which casefiles, how many

Dry-run by default (03 §10): without `--run` the script prints the plan and touches nothing.

```
python scripts/extract_casefiles.py                       # plan for the whole test set
python scripts/extract_casefiles.py --list                # same, with stored progress per casefile
python scripts/extract_casefiles.py --ids 6a372fc706f7689c0eefde1d --run           # one casefile (OMD, 2 mails)
python scripts/extract_casefiles.py --limit 3 --run                                # first 3 of the set
python scripts/extract_casefiles.py --offset 3 --limit 1 --max-windows 5 --run     # KING ONE, 5 windows only
python scripts/extract_casefiles.py --offset 3 --limit 1 --run                     # ...continue KING ONE to the end
python scripts/extract_casefiles.py --ids ... --restart --run                      # throw the stored record away
python scripts/extract_casefiles.py --run --attachments list                       # no digest calls (cheaper)
python scripts/extract_casefiles.py --run --attachments off                        # mails only
python scripts/extract_casefiles.py --validate-only                                # re-run the validator on stored records
```

| Flag | Effect |
|---|---|
| `--set FILE` / `--ids ID…` | which casefiles. Default set: `eval/test_sets/extraction-10.json` (09 §5, in order) |
| `--limit N` / `--offset K` | a slice of the selection |
| `--max-windows N` | at most N windows per casefile this invocation; record stays resumable |
| `--restart` | rebuild from the header (also automatic when the stored `extraction_version` differs) |
| `--attachments off\|list\|digest` | how much attachment work; `digest` is the pipeline of 08 |
| `--ocr off\|vision\|tesseract` | OCR backend for scanned PDF pages (default `PDF_OCR_BACKEND`) |
| `--no-translate` | leave non-English quotes without the `[EN: …]` bracket |
| `--no-split-quotes` | do not cut a sentence that evidences both an action and its result |
| `--window N --overlap K` | mails per call (N is the hard cap) and mails repeated from the previous window (default 0) |
| `--model NAME` | per-run model override (`LLM_MODEL` otherwise) |
| `--no-consolidate-call` | deterministic seq only, no consolidate model call |
| `--concurrency N` | casefiles in parallel (windows within a casefile are always sequential) |
| `--keep-quotes`, `--mail-max-chars` | body text handling |
| `--data-dir DIR` | run store (default `data/`, gitignored) |

Re-running the same command resumes every paused casefile from its checkpoint; a casefile whose record is
`complete` is skipped (0 pending windows). A provider error stops that casefile after the last good window
and moves on; the summary names it and the next run resumes it.

## 4. Output

```
data/
  cache/mail_bodies/<mail_id>.json       html + text (07 §4 mail_bodies cache; the validator's source)
  cache/urls/<sha1(url)>.json            url → sha256
  cache/attachments/<sha256>.json        attachment_digest.v1 document (+ audit: name, bytes, junk reason)
  cache/attachments/blobs/<sha256>       raw bytes
  extractions/<casefile_id>/record.json  casefile_extract.v2 — the deliverable
  extractions/<casefile_id>/windows/wNNN.json   what the model saw and returned, applied ids, flags, record after
  extractions/<casefile_id>/run_log.json        per run: windows, tokens by model, attachment stats, consolidate log
  last_run_summary.json
  site/                                  static casefile browser built from the above by scripts/build_site.py (12)
```

`record.json` is schema-valid at every checkpoint (seq is provisional until consolidate). `validation_flags`
on a finished record come from the validator (`quote_not_found:a3`, `sha_not_on_cited_mail:…`, `over_cap`)
plus `window_rejected:wN` when a window's delta could not be applied after one retry. Per-window repair flags
(`outcome_downgraded`, `bad_enum`, `mail_id_out_of_window`, `ids_renumbered_after_drop`) are in the window
audit file, not on the record.

Mongo load (`vessel_issues_incident_reports`, `vessel_issues_mail_attachments`) is not part of this step:
`src/db` does not exist yet. The files above are the shapes those collections take (07 §4).

## 5. Cost levers, in order of size

1. `--attachments list` or `off` — the digest calls are one per distinct content-bearing file (KING ONE ≈ 45).
2. `--ocr off` or a lower `PDF_OCR_MAX_PAGES` — OCR is one vision call per scanned page (KING ONE: 19 scanned
   PDFs, about 35 pages).
3. `EXTRACTION_VISION_ENABLED=false` (default) — every image is `listed_only`; OASIS knocking has 105.
4. `--max-windows` — bound a run; the record resumes.
5. `EXTRACTION_MAIL_MAX_CHARS` (12 000) and quote stripping (on) — bound the prompt per mail.
6. `--model` — the carried record holds the reasoning burden; a mid-tier model is the design assumption (02 §2.2).

Token totals per casefile are printed at the end of each run and stored in `run_log.json`.

## 6. Known limits of this iteration

- OCR goes through the chat model page by page; there is no tesseract binary on the dev machine, so the
  `tesseract` backend is untested here. Pages beyond `PDF_OCR_MAX_PAGES` are noted, not read.
- `.doc`, `.xls`, `.msg` need LibreOffice / extra libraries; recorded as `listed_only`.
- Vision digests of photos exist behind `EXTRACTION_VISION_ENABLED` but were not exercised on the test set.
- The stored digest carries an `audit` block the `attachment_digest.v1` schema does not list; a Mongo loader
  strips it.
- `problem_statement` is the export title verbatim (08 §1, schema/README §1), not written at consolidate as
  06 §4 says; the two docs disagree and 08 is the later one.
- Quote stripping is conservative (explicit separators only); a long inline history is cut by length, with
  the cut marked. The validator matches against the same text the model saw.
