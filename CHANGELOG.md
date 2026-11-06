# Changelog

## [Unreleased]

### Changed
- Search page side filter (docs/12 §3, 2026-11-02): under a selected Symptom family the UI no longer lists
  the symptom standards. It lists **Symptom · on / where** instead: the component each symptom wording named
  (its `part` standard id, shared with `action_logs[].parts[]`, label = display name; no part standard ->
  `__unresolved__`) and the location it named (`config/location_terms.csv` term, label `in <location>`).
  Index: new `symptom_on` facet (`ON_FACET`, parent `symptom_family`) in `site_search_index.v2`; the
  `symptom` / `symptom_broad` facets stay in the index and on the admin lookup page.

### Added
- Bounded, canonical facets for Equipment and Symptom (docs/12 §3 principle: a facet lists canonical ids
  only, wording is never a facet value). Resolver: `symptom` kind (`normalize_symptom` moved to
  `src/resolution/normalize.py`, re-exported from views), `family` table + `standard_term.family_id /
  family_method`, `find()` (read-only resolve), `families / family_of / set_family / unclassified /
  assign_families / import_families_csv`, `export_csv` with `family_key`, `fragmentation()` per kind.
  Family assignment human/seed > inherited (symptom_links) > keyword rule > none. `config/symptom_families.csv`
  (~20-family proposal) and `config/seed_symptom_aliases.csv`. `scripts/build_lookup.py` resolves symptoms and
  prints both review queues (`--review`) and `--families`. Index `site_search_index.v2`: `equipment_std`,
  `symptom_family`, `symptom` (ids), `symptom_broad` as ids, `labels`, `parents`, `facet_parent_vals`;
  `equipment_name` removed. UI: child facets nest under a selected parent (`equipment_type ▸ equipment_std`,
  `symptom_family ▸ symptom`), labels, pseudo values greyed last; `--no-lookup` fallback with a banner.
  Admin lookup page reads the resolver (`views.resolver_tables`): one merged "needs attention" queue (one row
  per standard, reasons `unreviewed` / `no family`, ranked by hits, first 10 shown, kind toggles, copy-fix
  button writing the CSV row to the clipboard), equipment_type → standard → aliases, family → standard
  (method) → aliases.
- Window audit now records what the model was GIVEN (docs/14): `wNNN.json` gains a top-level `inputs`
  block (prompt version, the record summary embedded in the prompt and its next free ids, every page of the
  window with body size / sha1 / fetched-now / twin status, every attachment URL with fetch-cache-junk status,
  read method, sent flag and the digest as sent, this window's token counts, one entry per attempt with the
  `## CORRECTION` feedback) and the exact first-attempt prompt is written as a `wNNN.prompt.txt` sidecar
  (`RunStore.put_window_prompt` / `get_window_prompt`). `run_log.runs[].windows[]` gains `mails`,
  `attachments_sent`, `tokens`. `AttachmentDigester.outcomes` exposes the per-URL outcome. The record itself
  is unchanged; old audit files without `inputs` still load. Journey page: a collapsed "what the model saw"
  panel per window (memory line, mails table with twins greyed, attachments table with status chips, tokens,
  corrections, raw prompt, raw responses per attempt); `RecordSource.windows_prompt()` added.
- Casefile browser: reviewed `broader_than` links between normalised symptom groups
  (`config/symptom_links.json`, `src/site/symptom_links.py`). The lookup page nests a narrower group under
  its broader one with a `⊂ broader` chip and the link note; the Symptom facet matches on a new
  `symptom_broad` index field so a broader term reaches its narrower groups transitively while a narrower
  term matches only itself. Groups are not merged and per-group counts are unchanged. Validation: a side
  missing from the current records drops the link with a warning; a cycle or a second broader term fails.
  Setting `SYMPTOM_LINKS_PATH`, flag `--symptom-links` on `scripts/build_site.py`.
- Component alias table, stage 1 of entity resolution (docs/13, 02 §3.9): `src/resolution/normalize.py`
  (casefold, punctuation, abbreviation expansion, position split, plural fold; deterministic key) and
  `src/resolution/lookup.py` (SQLite `standard_term` + `alias`, resolve with the three-case update rule,
  review queue, approve / merge / pin, display name computed as a view, CSV import/export).
  `src/resolution/seed_aliases.csv` is the human-edited seed; `scripts/build_lookup.py` rebuilds the table
  from the run store. Settings `RESOLUTION_DB_PATH`, `RESOLUTION_SEED_CSV`.
- Static casefile browser (docs/12): `scripts/build_site.py` renders the run store into `data/site/` —
  search page with weighted client-side search and facets, one page per casefile with every quote linked to
  its mail, mail pages with the cited quotes highlighted, attachment digest pages, and admin pages (raw→type
  lookup tables, review queue of validation flags, per-casefile build journey with row lineage).
  `src/site/` (RecordSource, views, Jinja templates, assets), `src/search/index.py` (index projection).
- Windowed v2 extraction pipeline (`src/corpus`, `src/attachments`, `src/extraction`, `src/contract`):
  export loader, mail HTML→text with quote stripping, attachment fetch/sha cache/junk filter/typed
  readers/digest, 5-mail window loop with delta application and dropped-object guard, consolidate,
  deterministic validator (schema, id links, verbatim-quote check, sha join, cap), local JSON run store.
- Prompts: `window_update` v1 (one-mail overlap) and v2 (default: no overlap; the window prompt carries an
  id-keyed summary of the record so far plus next free ids, and the model updates by id or mints the next
  id), `window_update` v3 (crew attributions are suspicions; logistics and operational events are not
  actions; non-English quotes as `original [EN: translation]`), `consolidate` v1, `attachment_digest` v1,
  `ocr_transcribe` v1, `translate_quotes` v1.
- Attachment readers route by magic bytes (eml labelled .pdf → mail parser); OCR of scanned PDF pages via the
  vision model (`src/attachments/ocr.py`, `PDF_OCR_BACKEND`), identity documents classified and never read,
  cached `listed_only` digests upgraded when a newer reader or OCR is available.
- Evidence-quote translation fallback (`src/extraction/translate.py`); validator matches on the original.
- `window_update` v4: a returning symptom is a new fault row with `recurrence_of` (optional field added to
  `casefile_extract.v2`), updates must match the row's party and subject, post-repair actions attach to the
  repaired fault, guarantee claim / PO and vessel consequences kept, repair checks as diagnostic rows,
  symptom is the first observation, equipment serial/details filled, single-line verbatim quotes.
- Updates may correct an action's `fault_id` (flagged). Consolidate refuses duplicate merges across
  different dates or more than `CONSOLIDATE_MERGE_MAX_DAYS` apart and keeps them as repeat groups.
- `repair_citations()`: a quote found in an uncited attachment of the casefile gets the sha added with a
  soft flag that does not set `needs_review`.
- After the 10-casefile dedup review — `window_update` v5, `consolidate` v2, `split_quote` v1:
  - one event is one action row (`also_fault_ids`, optional schema field); `collapse_fanout()` folds
    per-fault copies at consolidate;
  - action quote and outcome quote are different clauses; `split_shared_quotes()` fallback;
  - update evidence in `provenance.supporting[]` (optional schema field), validated; `prune_shas()` keeps
    only the attachments that hold a quote;
  - look-alike fault pairs judged at consolidate (same / recurrence / distinct) with a number-and-side
    guard (`src/extraction/textsim.py`) on every merge;
  - twin mails shown once; `date_performed` after the reporting mail nulled; validator flags
    `outcome_before_action`, `problem_restates_symptom`, soft `same_quote_action_outcome`, `similar_faults`.
- Output-token budget for window/consolidate calls doubles on a length cut-off (`EXTRACTION_WINDOW_MAX_TOKENS`).
- `scripts/package_records.py`: zip of the extracted records named by vessel, title and id, with a manifest.
- `scripts/extract_casefiles.py` — dry-run by default; `--ids/--set/--limit/--offset`, `--max-windows`,
  `--restart`, `--attachments off|list|digest`, `--window/--overlap`, `--validate-only`.
- `eval/test_sets/extraction-10.json` — the 10-casefile test set of docs/09.
- Config section for the run (`EXTRACTION_*`, `FETCH_*`, prompt versions, `get_prompt()`).
- docs/09 (test set), docs/10 (run guide); unit tests for windowing, text, junk filter, record merge,
  consolidate, validator and an end-to-end run with a scripted model.
