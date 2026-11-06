# Resolver hygiene, 2026-10-25: what changed (P0 → P3)

Session scope: the four prompts of `docs/16-resolver-hygiene-prompts.md`, decided in `docs/15`. Nothing was
committed; all changes are in the working tree. Test suite: 205 → 281 passing. `python scripts/build_site.py --check`
clean after every step.

| prompt | what it built | measured on the 10-record store |
|---|---|---|
| P0 | the shared store pieces: pending queue, resolution log, `alias.parts`, mint-guard hook, shared config CSVs, rebuild + diff tool, table-driven tests | rebuild diffed empty against the existing DB |
| P1 | symptom decomposition (A1): a symptom standard is the failure mode; component resolved as a `part`; location / modifiers on the alias row; link retirement; ON column | symptom standards 37 → 30; part standards 35 → 45; 21 split / 14 whole / 6 queued; 1 link retired |
| P2 | equipment-label hygiene: vessel strip (sister ship logged), CJK glossary + adjudicator hook, record maker/model/serial strip, qualifiers, `--check` | equipment standards 16 → 13; nothing queued |
| P3 | extractor prompt v6: `faults[].symptom` is the failure mode in ≤6 words; real re-run of the 10 casefiles into a scratch store | symptom median 9 → 3 words; ≤6 words 20% → 100%; queued 6 → 2 |

---

## 1. Resolver core — `src/resolution/`

### `lookup.py`
- New tables: `pending_term` (a spelling the mint guard refused: kind, raw_key, raw_term, reason, parts, source, dates,
  hit_count) and `resolution_log` (ts, kind, raw_key, method_chain, before, after, standard_id, rationale).
- New column `alias.parts` (JSON, nullable), added by `_migrate()` so a pre-existing DB still opens.
- `resolve()` gains `guard` (callable `(kind, Cleaned, raw) -> reason | None`) and `context` (the record entry:
  `equipment_type`, `vessel_name`, `maker`, `model`, `serial`). A would-be new standard the guard refuses is queued
  instead; the result is `Resolution(standard_id=None, method="queued", reason=…)`. `find()` never sees pending rows.
- For a symptom with a component, `resolve()` also resolves the component as kind `part` (`_resolve_component`), so
  "knocking from exhaust valve" and an action-log line "renew exhaust valve" share one part id.
- New API: `queue`, `pending`, `dismiss`, `promote`, `log`, `log_event`, `clean(raw, kind, context)`.
- Guards: `symptom_guard` (reason `undecomposed`), `equipment_guard` (`contains_vessel`, `contains_position`,
  `needs_translation`, `contains_identifier`), `compose_guards`, `DEFAULT_GUARD`.
- `LookupStore(config_dir=…, guard=…, adjudicator=…)`; `adjudicator` is the injectable translator for non-Latin
  leftovers (default `None` → queued `needs_translation`).
- `Resolution.method` vocabulary gains `queued` and is checked in the dataclass. `fragmentation()` gains `n_pending`.
- Every seed / config CSV is header-checked at import; a wrong header fails the build.
- `set_alias` (and so a seed-CSV row) clears a pending row for that spelling; `merge`, `approve`, `set_alias`,
  `queue`, `promote`, `dismiss` all write log rows. `method_chain` lists the cleaner's steps in firing order
  (`vessel_strip+position+abbreviations+normalised`); `alias.method` stays one word.

### `normalize.py`
- `POSITION_WORDS` now comes from `config/modifier_terms.csv`; `left → port`, `right → starboard`.
- `ResolverConfig` / `load_config(config_dir)`: modifier terms, component terms, location terms, equipment glossary,
  equipment qualifiers, vessel names (generated list + hand-kept extra list).
- `Cleaned.parts` (dict, nullable) carries what the cleaner took apart.
- **Symptoms (P1)**: `decompose_symptom(label, equipment_type) -> LabelParts(core, component, location, modifiers,
  position, residue, stem, component_raw, vessel, steps, leftover)`. Order: lemma map (explicit domain map +
  `_singular`, acronyms untouched, no stemmer) → vessel strip → position (`_SYM_POSITION_RE`, now also accepts
  `unit no:2`) → term extraction (tail after the last preposition to the end of the clause, then head, then bare) →
  strip of the record's OWN equipment words only → core. `SYMPTOM_EQUIPMENT_WORDS` deleted. A run of adjacent
  component terms is one component phrase. `normalize_symptom(text, equipment_type)` = the core.
- **Equipment (P2)**: `clean_equipment_label(label, context, kind) -> EquipmentParts(core, position, side, stripped,
  script, residue, stem, vessel, sister_vessel, untranslated, steps, issues)`. Steps: `vessel_strip` (whole name,
  with / without spaces / hyphens, never a substring) → `glossary` (CJK → English, `N号 → No.N`; leftover → adjudicator
  or `needs_translation`) → `record_strip` (the entry's maker / model / serial) → `qualifier` → `position` →
  `abbreviations`. A `part` label skips the vessel and record steps. `normalize()` now runs the part steps, so the
  condition words left `NOISE_WORDS` without changing any existing key.
- `normalize_for_kind(raw, kind, config, context, adjudicator)` is the one entry point the store uses.

### `diff.py` (new) and `scripts/diff_lookup.py` (new)
- `diff_lookup.py OLD NEW [--kind K] [--md out.md]`: both files opened read-only; per kind: standards before / after,
  aliases whose standard moved (old key → new key), spellings now pending and why, residue token frequency (top 30
  with casefile counts), new and vanished standards. Exit 0 always. Standards compared by key, not id.

### `__init__.py`
- Exports `PendingRow`, `ResolverConfig`, `load_config`; docstring lists the five tables and `diff.py`.

---

## 2. Config — `config/`

| file | status | purpose |
|---|---|---|
| `modifier_terms.csv` | new | position / side words (`term,canonical`); replaces the dict in `normalize.py` |
| `component_terms.csv` | new | component nouns a symptom can name → canonical part key; seeded from the store's parts + the corpus |
| `location_terms.csv` | new | places a symptom can name (pump room, engine room, crankcase, sump, …) |
| `equipment_glossary.csv` | new | non-Latin equipment words (`source_term,lang,english,kind`); zh seeded from KING ONE, ja / ko equivalents |
| `equipment_qualifiers.csv` | new | condition words stripped from equipment / part names (new, old, spare, damaged, …) |
| `vessel_names.txt` | new, **generated** | the fleet list from the records' `vessel_name` and the export's `vesselName`; rewritten by `build_lookup.py` |
| `vessel_names_extra.txt` | new, hand-kept | sister ships the export never names (PACIFIC GARNET) |
| `symptom_links.json` | changed | the one reviewed link (knocking sound > knocking sound from exhaust valve) was retired: its narrower key dissolved under A1; the reviewer's note moved into the resolution log. File is now `[]` |
| `seed_symptom_aliases.csv` | changed | header comment no longer says the knocking link is "not listed on purpose" |

---

## 3. Scripts — `scripts/`

### `build_lookup.py`
- Regenerates `config/vessel_names.txt` (records + export; `--export-file`, `--no-vessel-names`), loads the store
  with `config_dir` (`--config-dir`) and `DEFAULT_GUARD`, passes the record context per equipment entry and per fault.
- After feeding: queues residue tokens seen in ≥ N casefiles as `candidate_term` (`--residue-min`, default 2),
  prints the decomposition summary (split / whole / residue / queued, top residue tokens), retires dissolved symptom
  links (`symptom_links.retire_dissolved`).
- `--review` prints a third queue (queued, not minted) with the seed-CSV row that promotes each entry.
- `--check`: every equipment standard built from the records must have an alias whose record entry carries an
  `equipment_type`; offenders → exit 1; seed-only standards are reported, not failed.

### `extract_casefiles.py`
- Unchanged code; now runs prompt `window_update/v6` and `consolidate/v3` by default (config).

---

## 4. Site — `src/site/`, `src/search/`

- `views.py`: `resolver_tables()` adds `pending` / `pending_counts` (queue 3), an `on` list per symptom standard
  (component · location · modifiers with counts; component shown by its part standard's display name), and per
  equipment alias the cleaning `chain` and `stripped` text. New helpers: `equipment_type_of`, `fault_context`,
  `equipment_context`, `fault_symptom_key`, `fault_decomposition`, `canonical_position`. `normalise_symptom` now takes
  the equipment type.
- `render.py`: the review page gets the resolver; the casefile page gets `symptom_parts` and `canonical_position`;
  link validation uses the per-fault key.
- `search/index.py`: `find()` calls pass the record context for equipment and symptoms.
- Templates: `_pending_queue.html.j2` (new, included on `admin/lookup.html` and `admin/review.html`); the lookup
  page's "Needs attention" summary adds "· N queued"; Symptoms table gains an **on** column; equipment spellings show
  their chain and what was stripped; `casefile.html.j2` fault rows show `<core> · on <component> · in <location>` and
  equipment position shows the canonical side with the record's wording beside it.
- `assets/site.css`: small styles for the pending table, the decomposition line, the chain text.

---

## 5. Extraction — P3

- `src/prompts/window_update/v6.md` (from v5) and `src/prompts/consolidate/v3.md` (from v2): one field note each —
  `symptom` is *the failure mode as a short phrase (≤6 words: what is wrong, e.g. 'knocking sound', 'water leak',
  'fails to start'); which unit / where goes in `position`; the full sentence as written goes in
  `provenance.evidence_quote`*. v5 / v2 kept for the stored runs.
- `src/config/__init__.py`: defaults `WINDOW_UPDATE_PROMPT_VERSION=v6`, `CONSOLIDATE_PROMPT_VERSION=v3`; new
  `RESOLUTION_CONFIG_DIR` (default `config/`). `.env` and `.env.example` pins moved to v6 / v3.
- `schema/casefile_extract.v2.schema.json`: the `faults[].symptom` description.
- `src/contract/validator.py`: soft flag `symptom_too_long:<fid>` past 12 words (`SYMPTOM_MAX_WORDS`); never sets
  `needs_review`.
- Real run (2.26M input / 0.29M output tokens, 113 window calls) into `data/p3/` with the caches copied in. The v6
  records were **not** promoted into `data/extractions`; `data/` and the real DB still hold the v5 output.

---

## 6. Tests — `tests/`

| file | status | covers |
|---|---|---|
| `unit/resolution_cases.csv` | new | table-driven cases (`kind, raw, vessel_name, equipment_type, maker, model, serial, expected_key, expected_parts_json, expected_method`), 39 rows across equipment / part / symptom |
| `unit/test_resolution_cases.py` | new | the parametrised table test; queue / promote / dismiss; guard path; log rows; `_migrate()` on `fixtures/lookup_pre_p0.sqlite`; config tables; header checks; `diff` on in-memory stores; the queue-section golden |
| `unit/test_symptom_decomposition.py` | new | one standard for the three knocking wordings; guard on a two-component label; only the record's equipment words stripped; link retirement logged; candidate terms; ON column + fault row golden |
| `unit/test_equipment_cleaning.py` | new | whole-name vessel strip + sister log; glossary full / partial / adjudicator; record strip; qualifiers; each guard reason; `--check`; position view; chain / stripped golden |
| `unit/test_prompt_short_symptom.py` | new | v6 / v3 carry "≤6 words" once; schema description matches; prompts name no field outside the schema |
| `unit/test_v5_quality.py` | changed | long symptom is a soft flag |
| `unit/test_resolution_lookup.py`, `test_site_build.py`, `test_symptom_links.py`, `test_pipeline_fake_llm.py` | changed | expectations moved to the A1 keys, the context argument, right → starboard, prompt versions |
| `fixtures/lookup_pre_p0.sqlite` | new | a pre-P0 schema file for the migration test |

---

## 7. Docs

- `docs/13-component-lookup.md`: §2 the five tables and `alias.parts`; §3 the equipment cleaning steps and the
  config CSVs; §4 the guard / queued path and the guard reasons; §5 promote / dismiss and the three queues; §6 the
  rebuild + diff loop, `--check`, the shared config; §8 rewritten for A1 (key function, ON column, link retirement,
  sub-facet deferral, first-run numbers).
- `docs/06-casefile-extract-v2.md`: §3.2 `faults[].symptom` field description.
- `docs/11-extraction-changes.md`: §3.7 the v6 / v3 change with the review finding and the run results; files table.
- `docs/README.md`: row 13 and the `config/` paragraph.
- `docs/16-resolver-hygiene-prompts.md`: status line and each prompt marked "built 2026-10-25" with its outcome and
  deviations.
- `.gitignore`: `reports/` (the diff reports: `reports/p1_diff.md`, `p2_diff.md`, `p3_diff.md`).

---

## 8. Deviations from the prompts, with the reason

- **P0**: `Cleaned.parts["steps"]` drives the log chain; the guard is asked only for a would-be new term; a seed-CSV
  row clears a pending spelling. PACIFIC GARNET is not in the export, so the generated fleet list cannot know it.
- **P1**: adjacent component terms are one component phrase (the fuel-pump case gives `fuel pump spring`, not
  `fuel pump`); in the bolts case the component is `bolt` and the label is queued because `cover plate` / `idle gear`
  stay in the core; a label that is only a component is queued. `expected_parts_json` is a subset check.
- **P2**: record strip and qualifier strip run before abbreviation expansion so `MAN B&W` matches as written;
  PACIFIC GARNET comes from the hand-kept extra list; `--check` reports seed-only standards instead of failing them.
- **P3**: the field path is `provenance.evidence_quote` (the schema's name), versions went to v6 / v3 (v2 to v5 already
  existed), and there is no on-disk LLM cache in this repo, so every window call re-ran.

## 9. Open items

- Promote the v6 records (`data/p3/extractions`) into `data/extractions` and rebuild the DB, or re-run; the current
  store is still v5 output.
- Family patterns in `config/symptom_families.csv` were written for sentences; seven v6 symptom standards have no
  family (three before).
- Two v6 labels stay queued as two-component symptoms; a human promotes or dismisses them.
- The six P1 pending rows on the v5 store are the sentence-length labels; they disappear once the store is v6.
