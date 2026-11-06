# Resolver hygiene: review of the two prompts (symptom decomposition, equipment cleaning)

> Status: **review — no code changed.** Date: 2026-10-25
> Input: `resolver_hygiene_prompts.md` (Prompt A: decompose symptom labels; Prompt B: equipment-label hygiene)
> Against: `src/resolution/normalize.py`, `src/resolution/lookup.py`, `scripts/build_lookup.py`,
> `src/site/views.resolver_tables`, docs/13, the 10-record store.
> Outcome: the adapted prompts in §5–§7 are what to run; §3 needs a decision first.

## 1. The prompts were written against a codebase that is not this checkout

They assume `src/resolution/dedup.py`, an `ops` table, `symptom_occurrence` / `alarm_occurrence` rows, a
loader with a grep test, an injected LLM adjudicator and an "OASIS alarm-inside-symptom regression test".
None of that exists here (those names belong to the September graph-loader design). What exists:

| Prompt assumes | This repo has |
|---|---|
| `dedup.py` cascade alias → normalised → embedding → adjudicator → new_term | `lookup.LookupStore.resolve`: alias → normalised → new_term (mint, status `auto`). Nothing fuzzy. |
| `ops` row per change | no log table at all; `merge()` / `approve()` mutate in place with a `note` |
| occurrence rows with `position`, `component_id` … | the **record is the occurrence**: `equipment[]` already carries `equipment_type, position, maker, model, serial`; `faults[]` carries `position` |
| standards referenced by id from records | records carry **no** `standard_id`; the site resolves at build time with `find()`; the SQLite is "a rebuildable cache" (`--fresh`) |
| `re_resolve.py` migrating standards in place | unnecessary: `build_lookup.py --fresh` is the re-resolve; what is missing is a **diff of two DBs** |
| adjudicator to translate CJK | no model stage; `translate` exists in extraction (`test_ocr_translate_sniff`) only |

Consequences carried into the adapted prompts: parts go on the **alias row** (one JSON column) and in a new
`resolution_log` table; no occurrence tables; the adjudicator is an injectable `Callable | None` that, when
`None`, queues; re-resolve is rebuild-and-diff, not migration; the OASIS regression test is to be *written*,
not "kept".

## 2. What the store actually contains (10 records, 8 vessels, 18 equipment names, 40 symptom wordings)

**Equipment.** Noise classes present: CJK (`右主机`, `左主机`, KING ONE, position `right`/`left`); a
**sister-vessel** prefix (`PACIFIC GARNET AE #1` sits on a PACIFIC PEARL record — stripping the record's own
`vessel_name`, as Prompt B step (a) leads with, would **not** catch it; only the fleet list does, and the
mismatch is itself worth a log line); ordinals in both orders (`No.1 AE`, `AE No. 2 SCR`, `Auxiliary Engine
No.4`). **Not present**: maker / model / serial tokens inside a name (maker is "NA" on 12/19), free-text
qualifiers. Prompt B steps (e)–(f) are for noise the corpus has not produced; keep them table-driven and
empty rather than seeding guesses.

**Symptoms.** Most wordings are sentences, not labels ("Exhaust close stroke low and exhaust open stroke
low, followed by slowdown; exhaust valve position and fuel plunger position readings show only zero."). The
`<core> from|at|on <term>` patterns of Prompt A catch the short ones (`Knocking sound from exhaust valve`,
`Water leaking from the cylinder-head water jackets`) and leave the sentences whole — correct (under-merge is
safe) but it means decomposition at resolution time fixes ~⅓ of the vocabulary. The lever for the rest is
upstream: the extractor's `faults[].symptom` is a sentence because the prompt does not ask for a short
failure-mode phrase. Run A's stats step first; if residue dominates, the fix is one line in
`src/prompts/window_update/v1.md` ("symptom: the failure mode in ≤6 words; where it is goes in `position`"),
not more regex.

**An existing bug the prompts do not name.** `SYMPTOM_EQUIPMENT_WORDS` strips generic component nouns
(`pump`, `sensor`, `relay`, `actuator`, `tank`) from every symptom: `Unit No. 7 fuel pump spring found broken`
→ key `fuel spring found broken …`. That list was meant to drop the *equipment* ("main engine") and is now
eating *components*. Prompt A's decomposition should **replace** it: strip only the record's own
`equipment_type` words from the symptom; everything else is a component candidate or stays in the core.

## 3. Prompt A reverses a reviewed decision — decide before building

Three places say component nouns **stay** in the symptom key: the `normalize.py` docstring ("they localise
the symptom, they are not the equipment"), docs/13 §8, and `config/symptom_links.json` — a `broader_than`
link reviewed by Uday-Kanshiya on 2026-10-21, note: *"valve is the localised component, not a different
symptom class"*, with the seed CSV deliberately **not** aliasing the two. So #23 beside #7 on the lookup page
is the current design, classified into one family through the link, not a fragmentation bug.

Prompt A collapses both into `knocking sound` + `component = exhaust valve`. The link note's own reasoning
supports that (same class); what it costs is the link itself (its `narrower` key stops existing →
`links.validate()` warns) and the ability to see the two as separate standards without a component column.

| Option | What changes | When right |
|---|---|---|
| **A1 decompose** (Prompt A) | symptom standard = failure mode; component on the alias row and as a lookup column; `symptom_links.json` rows whose narrower key dissolves are retired with a log row | the team wants one standard per failure mode and will read "where" from a column |
| **A2 keep** | nothing; family inheritance already classifies the pair | the team wants `knocking from exhaust valve` and `knocking from bearing` as separately countable standards |
| **A3 hybrid** | decompose only when the tail resolves to an existing `part` standard; otherwise keep whole | wants A1 but refuses to mint component vocabulary from symptom text |

Recommendation: **A1**, with one change to the prompt — components resolve as the existing **`kind="part"`**,
not a new `component` kind. `action_logs[].parts[]` already mints `exhaust valve` as a part; a symptom's
component resolving to the **same standard id** is what lets "knocking from exhaust valve" and "renew exhaust
valve" join later. A new kind would split that vocabulary on day one. Get the reviewer of the link to agree
to A1 before running §5.

## 4. Shared pieces both adapted prompts need (build once, in `lookup.py`)

1. **`pending_term`** table — `kind, raw_key, raw_term, reason, parts (json), source, first_seen, last_seen,
   hit_count`, PK `(kind, raw_key)`. This is the mint guard's target: a refused mint lands here, **not** in
   `standard_term`, so `find()` keeps returning `None` for it (unresolved on the site) and `review_queue()`
   gains a third list. `reason ∈ undecomposed | needs_translation | contains_vessel | contains_position |
   contains_identifier`. `resolve()` returns a `Resolution` with `method="queued"`, `standard_id=None`.
2. **`resolution_log`** table — `ts, kind, raw_key, method_chain, before, after, standard_id, rationale`.
   One row per `resolve()` whose method is not `alias`, per `merge()`, per queue decision. `method_chain` is
   the ordered step names that changed the text (`vessel_strip+glossary+normalised`), so the lookup page can
   say *why* a spelling landed where it did. `alias.method` keeps its one-word vocabulary; the chain lives here
   and in `alias.parts`.
3. **`alias.parts`** JSON column (nullable) — the `LabelParts` / `EquipmentParts` the cleaner produced for that
   spelling. Read by `display_name()` (unchanged), `resolver_tables()` (new ON column), and the stats report.
4. **Re-resolve = rebuild + diff**: `scripts/diff_lookup.py OLD.sqlite NEW.sqlite` → standards per kind
   before/after, standards whose alias set moved (merges), aliases now queued and why, residue token
   frequency. Run as `build_lookup.py --db new.sqlite --fresh && diff_lookup.py data/resolution/lookup.sqlite
   new.sqlite`. No in-place migration script; `merge()` stays a human tool.
5. **Tests are table-driven**: `tests/unit/resolution_cases.csv` (`kind, raw, vessel_name, expected_core,
   expected_parts_json, expected_method`) read by one parametrised test, so the config CSVs and the test
   table grow together. Residue threshold default **N=2** casefiles while the store has 10.

## 5. Adapted Prompt A — symptom label → failure mode + where

```
Context: vessel_knowledge_graph, resolver = src/resolution/normalize.py (normalize_symptom,
SYMPTOM_EQUIPMENT_WORDS) + lookup.py (LookupStore: standard_term / alias / family; resolve → alias |
normalised | new_term). Records carry no standard ids; the site resolves at build time with find().
Decision taken (docs/15 §3): A1 — a symptom standard is the FAILURE MODE only. Components resolve as the
existing kind="part". docs/15 §4 items 1–5 are prerequisites; build them first.

1. decompose_symptom(label, record_equipment_type) -> LabelParts(core, component, location, modifiers,
   position, residue). Order: lemma map → vessel-name strip (config/vessel_names.txt) → position extraction
   (existing _SYM_POSITION_RE) → component / location / modifier extraction → equipment-type strip → core.
   Equipment-type strip removes ONLY the words of the record's own equipment_type (and its abbreviations from
   ABBREVIATIONS), replacing the SYMPTOM_EQUIPMENT_WORDS list, which currently eats component nouns
   (docs/15 §2). Tables, longest-first, whole-word, case-insensitive after lemma:
     config/component_terms.csv   term, canonical_part_key      (canonical = a normalize() key of kind part)
     config/location_terms.csv    term, canonical_location
     config/modifier_terms.csv    term, canonical_modifier       (port/starboard/fwd/aft/upper/lower/inner/outer;
                                                                  left→port, right→starboard)
   Patterns: `<core> (from|at|on|in|of|around|inside|below|above) <term…>`, `<term…> <core>`, bare term.
   A tail after a preposition that matches no table stays in the core and is copied to residue.
2. Storage: alias.parts JSON; component resolved through store.resolve(term, kind="part") so it shares the
   standard id with action_logs parts; its raw spelling becomes a part alias with method chain
   symptom_component+normalised. No new tables beyond docs/15 §4.
3. Mint guard: refuse a new_term symptom whose core still contains a component/location/modifier term, a
   position token, the record's equipment_type words, or a fleet vessel name → pending_term,
   reason=undecomposed. Residue tokens seen in ≥N casefiles (N=2) → pending_term reason=candidate_term with
   the proposed table.
4. config/symptom_links.json: a row whose narrower key no longer exists after decomposition is retired
   (removed from the file, one resolution_log row with rationale "dissolved by decomposition into <core> +
   <component>"); families still assign through the surviving links and rules. links.validate() must end
   with no warnings.
5. Lookup page (views.resolver_tables, templates): Symptoms table gains an ON column (distinct
   component · location · modifier per standard, with counts). Defer the COMPONENT / LOCATION search
   sub-facets until the store is past ~50 records; note that in docs/13 §8.
6. Tests (table in tests/unit/resolution_cases.csv): component tail, component head, position+component,
   location only, modifier+component, no known terms (whole), unknown tail (whole, residue set), the fuel-pump
   case (`Unit No. 7 fuel pump spring found broken` must keep "fuel pump spring"), an alarm clause inside a
   symptom (`Exhaust valve closing delay alarm on cylinder No.5` → core keeps "alarm", position cyl 5),
   and a golden test on the ON column. pytest + python scripts/build_site.py --check.
7. Report: run build_lookup.py --fresh to a scratch DB, then diff_lookup.py against the current DB:
   labels split / whole / residue, top residue tokens, standards before/after per kind, links retired. If
   most symptom wordings pass through whole, say so: the next fix is the extractor prompt (one line:
   "symptom = failure mode in ≤6 words"), not more tables.
```

## 6. Adapted Prompt B — equipment-label hygiene before resolution

```
Context: vessel_knowledge_graph, resolver = src/resolution/normalize.py (normalize: abbreviations, position,
NOISE_WORDS) + lookup.py. equipment[] in the record already carries equipment_type, position, maker, model,
serial per entry (schema v2) — the cleaner STRIPS these from the name, it does not extract them into new
storage. The lookup page groups equipment standards by the majority equipment_type of their aliases' records
(views.resolver_tables); there is no equipment_type column on standard_term and none is added.
docs/15 §4 items 1–5 are prerequisites.

1. clean_equipment_label(label, record_equipment) -> EquipmentParts(core, position, side, stripped (dict of
   step → removed text), script, residue). Steps, each a method name in the chain:
   a. vessel_strip — remove every name in config/vessel_names.txt (regenerated at build time from the
      records' vessel_name AND the export's vesselName so sister ships are covered; vesselName is audit-only
      everywhere else — this stopword use is its one load-bearing role, as for mail.subject_stem).
      Whole-name match, with/without spaces and hyphens, never substrings. When the stripped vessel ≠ the
      record's vessel, log it (rationale "label names sister vessel <X>").
   b. glossary — if the label has non-Latin script (unicodedata), replace longest-first from
      config/equipment_glossary.csv (source_term, lang, english, kind ∈ equipment|position|modifier), seeded
      with the zh terms the store has (主机 main engine, 右 starboard, 左 port, 辅机 auxiliary engine, 锅炉
      boiler, 泵 pump, 发电机 generator) plus ja/ko equivalents; set script=<lang>. Non-Latin text left over →
      adjudicator if one is injected, else pending_term reason=needs_translation. Never mint.
   c. existing: PUNCTUATED_ABBREVIATIONS / ABBREVIATIONS, _singular.
   d. position / side — existing _strip_position plus config/modifier_terms.csv (shared with Prompt A);
      left→port, right→starboard canonicalised here and in the record's position view.
   e. record_strip — remove tokens equal to the record entry's own maker / model / serial values when they
      appear inside the name (whole-token). No maker list is built from the corpus: 12 of 19 makers are
      "NA" and none appears inside a name today. Keep the step; let the test table drive its growth.
   f. qualifier strip — move NOISE_WORDS' condition words (new, spare, old, replacement, existing) to
      config/equipment_qualifiers.csv; quantity words stay in NOISE_WORDS.
   g. core = what remains; residue = anything removed that matched no table.
2. Mint guard: refuse a new_term equipment standard whose core contains a fleet vessel name, a position/side
   token, non-Latin characters, or the record's maker/model/serial → pending_term with the reason. Every
   equipment standard must have ≥1 alias whose record has an equipment_type; build_lookup.py --check fails
   otherwise (replaces "seeds without equipment_type fail": the seed CSV has no type column; the type is a
   view and the extractor already supplies it per record).
3. alias.parts holds EquipmentParts; alias.method stays one word; the chain goes to resolution_log.
4. Re-resolve: build_lookup.py --fresh into a scratch DB + diff_lookup.py (docs/15 §4.4). Expected on the
   current store: PACIFIC GARNET AE #1/#2 → Auxiliary Engine (No.1 / No.2); 右主机 / 左主机 → Main engine
   (starboard / port); standards for kind=equipment drop by 3.
5. Tests (tests/unit/resolution_cases.csv): vessel prefix (own vessel); sister-vessel prefix; vessel name
   containing an equipment word ("ENGINE STAR" must not remove "engine"); CJK full glossary hit; CJK partial
   hit (queued, not minted); ordinal before and after the noun; maker inside the name (from record field);
   qualifier; clean label unchanged. Golden test on the lookup page. pytest + build_site.py --check.
6. Report: standards before/after, merges, queued with reasons, top residue tokens, sister-vessel log lines.
```

## 7. Smaller corrections to carry

- `POSITION_WORDS` maps `left→left`, `right→right`; marine convention facing forward is port/starboard. The
  KING ONE record stores `right`/`left`. Canonicalise in `modifier_terms.csv` and apply to both kinds.
- Prompt A's "component / location resolve through the same resolver (kind='component', 'location')": use
  `part` for components (§3); `location` is a small closed list (pump room, engine room, void space, steering
  flat …) — a CSV column, not a resolver kind, until it needs aliases.
- Drop from both prompts: "the loader never inserts them directly (existing grep test must pass)",
  "ops row", "occurrence rows", "re-attach spellings" — see §1.
- Near-synonym note (knocking vs vibrating) stands: separate standards, one family, alias merges only via
  the reviewed seed CSV. Unchanged by this review.

## 8. Decisions (taken 2026-10-25)

1. **A1** — symptom standard = failure mode; where it is goes on the alias row. Link reviewer to be told.
2. Components resolve as the existing **`kind="part"`**.
3. Re-resolve = **fresh rebuild + diff**; no in-place migration.

The prompts to run, in order, are in `docs/16-resolver-hygiene-prompts.md` (P0 shared pieces → P1 symptoms →
P2 equipment → P3 extractor prompt, conditional). §5–§6 above are superseded by that file.
