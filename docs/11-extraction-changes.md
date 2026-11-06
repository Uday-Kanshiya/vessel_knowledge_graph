# Extraction pipeline — what was built and what changed

> Status: **record of work**, 2026-10-06
> Covers everything added to this repo to run the `casefile_extract.v2` extraction through the LLM API on the
> 10-casefile test set (09), and every change made after each review of the output.
> How to run it: `10-extraction-run-guide.md`. Design it implements: 05, 06, 08.

---

## 1. Where things stand

| | |
|---|---|
| Pipeline | complete end to end: export → 5-mail windows → consolidate → validate → local JSON store |
| Prompts in use | `window_update/v5`, `consolidate/v2`, `attachment_digest/v1`, `ocr_transcribe/v1`, `translate_quotes/v1`, `split_quote/v1` |
| Tests | 125 unit tests, no network or credentials needed; lint clean |
| Records on disk | 10 of 10 complete. 2 built with the current prompts (MAERSK EL PALOMAR, KMARIN RESOLUTION); 8 built with `window_update/v4` and shown as `stale` by `--list` |
| Zip | `data/casefile_extracts_20261005_152645.zip` — the `v4` run. Re-run and re-package to refresh (§7) |
| Not built | Mongo load (`src/db` does not exist), name resolution, graph. Nothing is committed to git yet |

---

## 2. The pipeline as built

```
scripts/extract_casefiles.py            which casefiles, how many windows, dry-run by default
  └ src/corpus/export.py                export JSON → Casefile; LLM-written summary fields never exposed
  └ src/extraction/runner.py            per casefile, resumable at window granularity
       ① header        casefile_id, imo, vessel_name, problem_statement = export title, export_meta
       ② per window of 5 NEW mails
            mail bodies   src/extraction/mail_text.py   HTML → text, quoted replies cut, cached by mail_id
            twin mails    a mail stored twice (same body, two ids) is shown once
            attachments   src/attachments/              fetch → sha256 cache → junk filter → byte-sniffed
                                                        reader → OCR of scanned pages → one digest call
            window call   src/extraction/window_update.py   id-keyed SUMMARY of the record + 5 mails
                                                        → JSON delta (add / update)
            apply         src/extraction/record.py      ids checked, evidence required, enums coerced,
                                                        out-of-window citations dropped
            split quotes  src/extraction/quote_split.py action clause / result clause
            translate     src/extraction/translate.py   "original [EN: translation]"
            checkpoint    record.json + windows/wNNN.json
       ③ consolidate   src/extraction/consolidate.py   fan-out collapse, look-alike faults, duplicate
                                                        actions, repeats, seq
       ④ validate      src/contract/validator.py       citation repair, hash pruning, then the checks
       ⑤ store         src/extraction/store.py         data/extractions/<casefile_id>/
scripts/package_records.py              zip of records named VESSEL__title__id.json + manifest
```

Model calls, and nothing else: window update, attachment digest, OCR transcription (per scanned page),
consolidate, and two small fallbacks that fire only when needed (quote translation, quote split).

---

## 3. Changes in the order they were made

### 3.1 First build

- Export loader, mail text, attachment layer (fetch, sha cache, junk filter, PDF/DOCX/XLSX/EML/archive readers,
  digest), windowed extraction with a **delta** output instead of a rewritten record, consolidate, validator,
  local run store, CLI with `--ids / --set / --limit / --offset / --max-windows / --restart / --attachments`.
- The model returns changes only, so a row from an earlier window cannot silently disappear (02 §2.2 rule 1).
  The dropped-object check still runs after every merge.
- `assets-prod.siya.com` is reachable from the local shell, which answers 02 §8.5 for local runs.

### 3.2 No mail overlap; the record summary is the memory (`window_update/v2`)

- Window is 5 new mails, overlap 0 (was 5 with 1 repeated). 113 windows for the test set instead of 135.
- Every window prompt opens with an id-keyed summary of all equipment, problems, faults and actions found so
  far, plus the next free id per family. The model updates a row by id or mints the next id.
- The summary carries no provenance or quotes, which shrinks the prompt.

### 3.3 After the first KING ONE run (`window_update/v3`)

Finding: 19 of KING ONE's 30 PDFs are scans with no text layer, including the service report and all three
SOFs that carry the repair chain. Three more "PDFs" were `.eml` files mislabelled by the server.

- **Byte sniffing**: files route by magic bytes before file name.
- **OCR**: scanned pages are rendered with PyMuPDF and transcribed by the vision model, one call per page,
  capped per file. The transcription is the document's text, so quotes from it are validated.
  Cached `listed_only` digests are upgraded on the next run without refetching.
- **Identity documents** (passports, seaman's books, visas) are classified by name, content or digest and never
  read or sent onward.
- **Prompt**: a crew member's attribution is a `suspected_cause`, not a problem row; logistics, invoices and
  operational events are not actions; one instruction issued in steps is one action.
- **Translation**: non-English quotes are stored as `原文 [EN: translation]`. The validator strips the bracket
  and checks the original.

### 3.4 After the KING ONE comparison review (`window_update/v4`)

- **Recurrence**: a symptom that returns after a resolved action or a stated gap is a **new fault row** linked
  by `recurrence_of`. An open problem discussed over many mails stays one fault.
- An update must concern the same party and subject as the row. Running-in and monitoring attach to the fault
  that was repaired. The guarantee claim or purchase order is kept as one commercial row with delivery status
  as its outcome. Repair checks listed in a service report are diagnostic rows. Vessel consequences (transit
  cancelled) are administrative rows.
- An update may correct an action's `fault_id`; the window audit records it.
- **Citation repair**: a quote found in an attachment of the casefile the row did not cite gets that hash
  added, with a soft flag.
- **Merge guard**: consolidate refuses a duplicate merge when the two rows state different `date_performed`
  values or their mails are more than `CONSOLIDATE_MERGE_MAX_DAYS` apart; the pair is kept as a repeat group.

### 3.5 During the first full run

- Three casefiles paused when a reply hit the 8,192 output-token limit (reasoning tokens count against it).
  Window and consolidate calls now start at 16k and double on a cut-off, up to 64k. Checkpoints held; the
  three resumed from the last good window.
- `scripts/package_records.py` added.

### 3.6 After the 10-casefile de-duplication review (`window_update/v5`, `consolidate/v2`)

| Defect reported | Cause found in the records | Change |
|---|---|---|
| One sentence proves both an action and its result (33 of 193 actions) | the source sentence carries both halves | prompt asks for two different clauses; `split_shared_quotes()` cuts any still shared, both parts verified verbatim |
| Several action rows share one quote | the earlier prompt said to write one row per fault for a single event | one event is one row, other faults in `also_fault_ids`; `collapse_fanout()` folds per-fault copies at consolidate |
| One quote cited under several attachment hashes | an update's attachment was unioned into the row's hash list | update evidence moved to `provenance.supporting[]`; `prune_shas()` drops cited hashes that do not hold the quote |
| Two faults read alike | the cylinder sat in the symptom text while `position` was empty | `position` always filled; look-alike pairs on one equipment judged at consolidate as same / recurrence / distinct |

General guards added with them:

- Similarity never decides a merge alone. "Cylinder No.3" and "Cylinder No.6" score 0.97 on wording, so every
  merge also requires the numbers and sides in both texts to agree (`src/extraction/textsim.py`).
- A "same" verdict on two faults becomes a recurrence when an action was stated resolved in between.
- Twin mails are shown to the model once and recorded in `run_log.runs[].mail_aliases`.
- A `date_performed` later than the mail reporting it (one day of slack for vessel-local versus UTC) is a
  plan, so it is left null.
- Quotes in `supporting[]` are validated like primary quotes. Before this they sat only in audit files.

---

### 3.7 After the resolver's symptom decomposition (`window_update/v6`, `consolidate/v3`, 2026-10-25)

| Defect reported | Cause found in the records | Change |
|---|---|---|
| Symptom wordings are sentences: under the resolver's A1 decomposition (13 §8) 14 of 41 passed through whole and 6 were queued undecomposed; median 9 words, only 8 of 41 at ≤6 words | the prompt asked for "the first thing observed, as reported" and never bounded it, so the failure mode, the component and the place sat in one sentence | `faults[].symptom` is described, in the schema and once in each prompt's field notes, as *the failure mode as a short phrase (≤6 words …); which unit / where goes in `position`; the full sentence as written goes in `provenance.evidence_quote`*. No examples beyond the three. Validator soft flag `symptom_too_long:<fid>` past 12 words. v5 / v2 kept for the stored runs |

**Real run under `v6` / `v3`, all 10 casefiles, 2026-10-25**, into a scratch store (`data/p3`, caches copied so
only the 113 window calls ran): 2.26M input / 0.29M output tokens, 10 of 10 complete, flags
`quote_not_found` ×5, `over_cap` ×3, `sha_not_on_cited_mail` ×1 (soft: `quote_in_uncited_attachment` ×2,
`same_quote_action_outcome` ×1), no `symptom_too_long`. Rows e/p/f/a per casefile: OASIS tank 8/1/13/46, OASIS
knocking 1/0/6/20, CAP SAN crane 1/1/1/27, KING ONE 2/1/6/25, PACIFIC PEARL 3/0/3/9, CORAL PEARL 2/0/5/26, CAP
SAN forms 1/0/4/14, WHITE ALBA 1/1/1/5, MAERSK 1/1/1/4, KMARIN 1/0/1/3 — the same 41 faults as under v5.

| symptom wordings (41 faults) | v5 records | v6 records |
|---|---:|---:|
| median / mean words | 9 / 11 | 3 / 3.3 |
| at ≤6 words | 8 (20%) | 41 (100%) |
| over 12 words | 12 | 0 |
| decomposed (13 §8): split / whole / with residue / queued | 22 / 13 / 11 / 6 | 15 / 24 / 1 / 2 |
| symptom standards (lookup build) | 30 | 30 |

The two still queued are genuine two-component labels ("fractured cover-plate bolts", "broken spring and
casing damage"). The place moved into `position` as asked ("cyl 6 exhaust expansion bellows", "bottom floor",
"Unit #4 piston ring groove packs"), so more wordings now pass through whole with a short core ("knocking
sound", "water leakage", "fails to start"); some cores are thin ("trouble", "failure", "damage") and seven
symptom standards have no family (three before) — the family patterns were written for sentences. The v6
records were not promoted into `data/extractions`; `reports/p3_diff.md` holds the lookup diff.

## 4. Schema: three optional fields added to `casefile_extract.v2`

All additive, so earlier records and the KING ONE sample remain valid.

| Field | Meaning |
|---|---|
| `faults[].recurrence_of` | id of the earlier fault this one is a return of; null for a first occurrence |
| `action_logs[].also_fault_ids` | other faults the same single event also serves; never contains `fault_id` |
| `provenance.supporting[]` | evidence for later updates to a row: `{mail_ids, attachment_shas, evidence_quote, fields}` |

---

## 5. Validation flags

Hard flags set `needs_review`. Soft flags record a repair or an advisory and do not.

| Flag | Kind | Meaning |
|---|---|---|
| `schema_error`, `duplicate_id` | hard | record shape |
| `unresolved_equipment_id` / `_problem_id` / `_fault_id` / `_recurrence_of` | hard | a link that does not resolve |
| `quote_not_found:<row>` | hard | a quote (row, `supportingN`, or `.outcome`) is not verbatim in any source it cites |
| `sha_unknown`, `sha_not_on_cited_mail`, `non_content_bearing_sha` | hard | attachment citation problems |
| `outcome_inconsistent`, `outcome_before_action` | hard | outcome without mail or quote; outcome dated before the action |
| `problem_restates_symptom:<p>:<f>` | hard | a problem row that only repeats its fault's symptom |
| `over_cap:<n>>25` | hard | more action rows than `EXTRACTION_MAX_ACTIONS`; nothing is truncated |
| `window_rejected:w<N>` | hard | a window's delta could not be applied after one retry |
| `quote_in_uncited_attachment:<row>:<sha8>` | soft | quote found in another attachment of the casefile; hash added |
| `same_quote_action_outcome:<a>` | soft | one sentence still evidences both action and result |
| `similar_faults:<f>~<f>` | soft | two look-alike faults judged distinct |

Per-window repair flags (`outcome_downgraded`, `bad_enum`, `mail_id_out_of_window`, `fault_id_corrected`,
`future_date_performed`, `update_without_quote`, `ids_renumbered_after_drop`) are in the window audit files.

---

## 6. Results so far

**First full run, `window_update/v4`, 2026-10-05.** 533 mails, 113 windows, about 2.74M input and 0.46M output
tokens including the resume.

| Casefile | equipment / problems / faults / actions | Flags |
|---|---|---|
| OASIS — HFO tank leak | 7 / 1 / 14 / 60 | over cap, 2 quotes not found; 2 recurrence rows |
| OASIS — knocking sound | 1 / 0 / 6 / 29 | over cap |
| CAP SAN ARTEMISSIO — crane incident | 1 / 1 / 1 / 25 | 1 quote not found, 2 citation repairs |
| KING ONE | 2 / 3 / 4 / 17 | none |
| PACIFIC PEARL | 1 / 0 / 1 / 8 | none |
| CORAL PEARL | 1 / 0 / 1 / 30 | over cap, 2 quotes not found |
| CAP SAN ARTEMISSIO — technical forms | 1 / 1 / 5 / 14 | none |
| WHITE ALBA | 1 / 1 / 1 / 3 | none |
| MAERSK EL PALOMAR | 1 / 1 / 1 / 4 | none |
| KMARIN RESOLUTION | 1 / 0 / 1 / 3 | none |

**Effect of the §3.6 changes, measured on those records without re-running the model:** OASIS knocking drops
from 29 to 19 actions through fan-out collapse (under the cap); 32 shared quotes are queued for the split
step; both look-alike fault pairs are held apart by the number guard; CORAL PEARL gains
`outcome_before_action` and KING ONE gains `problem_restates_symptom`.

**Real run under `v5` on the two smallest casefiles:** zero flags, zero shared quotes (four before), `position`
filled on both faults. The model cut the quotes itself; the split fallback did not need to fire.

---

## 7. To refresh the eight stale records and the zip

```
python scripts/extract_casefiles.py --list                       # shows which are stale
python scripts/extract_casefiles.py --run --concurrency 3        # rebuilds stale records, skips current ones
python scripts/package_records.py                                # new zip in data/
```

Mail bodies, attachment bytes, OCR text and digests come from cache, so the cost is the window calls.

---

## 8. Deliberately not done here

- **Canonical names** (`M/E` versus `main engine`, `unit 5` versus `cyl 5`), the `position` vocabulary, and the
  equipment key across casefiles. The extract keeps the documents' wording; resolution is the loader's job
  (02 §3.9). `textsim.py` folds abbreviations for comparison only and never writes them.
- **"Values outside the fixed set"** in the review: `vendor`, `class`, `agent`, `inconclusive`,
  `no_information` and `administrative` are all valid in this schema.
- **Attaching the casefile title to every fault as its problem**: a loader concern; the record holds the title
  once, as `problem_statement`.

## 9. Known limits

- OCR runs through the chat model; the `tesseract` backend exists but is untested (no binary on the dev machine).
- `.doc`, `.xls` and `.msg` files are listed, not read. Photos are listed unless `EXTRACTION_VISION_ENABLED`.
- The technical-forms negative control produced 5 faults and 14 actions under `v4`; doc 09 expected a
  near-empty record. It has not been reviewed row by row.
- Attachment counters printed per casefile are shared when casefiles run concurrently.
- `problem_statement` is the export title verbatim (08 §1), not written at consolidate as 06 §4 says.
- The stored attachment digest carries an `audit` block that `attachment_digest.v1` does not list.

---

## 10. Files

| Area | Files |
|---|---|
| Config | `src/config/__init__.py` (extraction section, `get_prompt()`), `.env.example`, `pyproject.toml` (`httpx`, `jsonschema`, `attachments` extra), `.gitignore` (`data/`) |
| Corpus | `src/corpus/export.py` |
| Attachments | `src/attachments/{fetch,junk_filter,readers,ocr,digest}.py` |
| Extraction | `src/extraction/{mail_text,windows,record,window_update,quote_split,translate,textsim,consolidate,runner,store}.py` |
| Contract | `src/contract/validator.py`; `schema/casefile_extract.v2.schema.json` (three optional fields) |
| Prompts | `src/prompts/window_update/v1…v6.md`, `consolidate/v1…v3.md`, `attachment_digest/v1.md`, `ocr_transcribe/v1.md`, `translate_quotes/v1.md`, `split_quote/v1.md` |
| Scripts | `scripts/extract_casefiles.py`, `scripts/package_records.py` |
| Test set | `eval/test_sets/extraction-10.json` |
| Tests | `tests/conftest.py`, `tests/fixtures/casefile_mini.json`, `tests/unit/test_{windows_and_text,export_and_junk,record_and_consolidate,validator,pipeline_fake_llm,ocr_translate_sniff,v4_rules,v5_quality}.py` |
| Docs | `docs/10-extraction-run-guide.md`, this file, `docs/README.md`, `README.md`, `CHANGELOG.md` |
