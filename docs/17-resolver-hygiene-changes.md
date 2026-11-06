# Resolver hygiene — what changed on 2026-10-25 and the drawback it left

> Status: **record of work.** One file: the changes (§1–§4), the measured result (§5), the drawback (§6), the fix
> (§7). Design: docs/15 (review and decisions) · prompts: docs/16 · reference of the store as it now is: docs/13.

Decisions in force (docs/15 §8): **A1** — a symptom standard is the failure mode only, everything that says
*where* goes on the alias row; components resolve as the existing **`kind="part"`**; re-resolve is **rebuild +
diff**, never an in-place migration. Nothing in the resolver is fuzzy; a name the tables cannot place is
queued, not forced to its nearest neighbour.

## 1. Store pieces shared by everything else (P0)

| Piece | What it is |
|---|---|
| `pending_term` table | where a refused mint lands: `kind, raw_key, raw_term, reason, parts, source, first_seen, last_seen, hit_count`. `find()` never sees it, so the site shows such a spelling as unresolved. Third review queue (`--review`, admin pages). |
| guard hook on `resolve()` | `guard(kind, cleaned, raw) -> reason | None`, asked only for a would-be `new_term`; a reason queues instead of minting, `Resolution.method="queued"`. |
| `resolution_log` table | one row per decision that was not a plain alias hit: `ts, kind, raw_key, method_chain, before, after, standard_id, rationale`. The chain is the ordered cleaning steps that changed the text (`vessel_strip+glossary+normalised`). |
| `alias.parts` (JSON) | what the cleaner took apart for that spelling — `Cleaned.parts`. |
| `config/modifier_terms.csv` | replaces `POSITION_WORDS`; adds `left→port`, `right→starboard`. |
| `config/vessel_names.txt` (+ `_extra.txt`) | fleet stopword list, regenerated from records and the export; hand-kept extra for sister ships with no casefile (PACIFIC GARNET). |
| `scripts/diff_lookup.py OLD NEW` | standards before/after, moved alias sets, pending with reasons, residue frequency. Rebuilding the current DB and diffing against itself was empty. |
| `tests/unit/resolution_cases.csv` | one parametrised test over a table of (kind, raw, vessel_name, equipment_type, expected key, parts, method). |

## 2. Symptom decomposition (P1)

`normalize.decompose_symptom(label, equipment_type) -> LabelParts(core, component, location, modifiers,
position, residue, …)`; the key is the core. Order: lemma map → vessel strip → position → component / location
/ modifier extraction (`config/component_terms.csv`, `location_terms.csv`, `modifier_terms.csv`; longest-first,
whole-word; patterns `<core> from|at|on|in|of|… <term>`, `<term> <core>`, bare term) → equipment-type strip →
core. The equipment-type strip removes only the record's own `equipment_type` words; the old
`SYMPTOM_EQUIPMENT_WORDS` list is gone (it ate component nouns: *fuel pump spring* → *fuel spring*). The
component resolves through `resolve(kind="part")`, so a symptom's component and `action_logs[].parts[]` share
one standard id. A tail after a preposition that matches no table stays in the core and is reported as
residue. Mint guard: a core still carrying a table term, position token, equipment word or vessel name is
queued `undecomposed`. The reviewed `knocking sound > knocking sound from exhaust valve` link was retired with
its note kept in the log. Lookup page: **on** column (component · location · modifier, with counts).

Deviations recorded during the build: adjacent component terms form one component phrase (`fuel pump
spring`); in the fractured-bolts wording the component is `bolt` and the label is queued; a label that is
only a component (`Exhaust valve`) is queued.

## 3. Equipment-label hygiene (P2)

`normalize.clean_equipment_label(label, record_entry) -> EquipmentParts`, steps in firing order, each named in
the log chain: `vessel_strip` (fleet list, whole-name, sister-ship mismatch logged) → `record_strip` (the
record's own maker / model / serial, as written) → `qualifier` (`config/equipment_qualifiers.csv`) → `glossary`
(`config/equipment_glossary.csv`, zh/ja/ko; leftover non-Latin → queued `needs_translation`) → abbreviations
→ position / side. Guard reasons: `contains_vessel`, `contains_position`, `needs_translation`,
`contains_identifier`. `build_lookup.py --check`: every equipment standard needs one alias whose record has an
`equipment_type` (the type stays a view; no column added).

## 4. Extractor prompt (P3)

`window_update/v6`, `consolidate/v3`: `faults[].symptom` is "the failure mode in ≤6 words; which unit / where
goes in `position`; the sentence as written stays in `provenance.evidence_quote`". Run on the 10-casefile set
into the scratch store `data/p3` (docs/11 §3.7). **Not promoted** into `data/extractions` yet — separate
decision.

## 5. Measured result (10 records)

| | before | after P1 | after P2 | after P3 (scratch) |
|---|---|---|---|---|
| symptom standards | 37 | 30 | 30 | 30 |
| part standards | 35 | 45 (10 components) | 45 | — |
| equipment standards | 16 | 16 | 13 | — |
| wordings split / whole / residue / queued | — | 21 / 14 / 8 / 6 | — | 15 / 24 / 1 / 2 |
| symptom median words · share ≤6 | 9 · 20% | — | — | 3 · 100% |

P2 merges: `PACIFIC GARNET AE #1/#2` → Auxiliary Engine No.1 / No.2 (sister ship logged); `右主机` / `左主机` →
Main engine starboard / port; `Port-side forward chain-stopper guillotine bar` → chain stopper guillotine bar,
port forward. Nothing queued on equipment.

## 6. The drawback: decomposition splits WHERE, not HOW-SAID or WHEN

The lookup page still shows **three standards** under `fails_to_start` — `could not start` (#57), `engine
sometimes fails to start in dead slow` (#23), `failed to start during approaching berth` (#60) — and two for
`LO sump … alarm not activating`. The knocking trio merged because, once `from <component>` was removed, all
three cores were the identical string `knocking sound`. These differ **in the core itself**, on axes the layer
has no tables for:

| wording | core after decomposition | what keeps it apart |
|---|---|---|
| No.1 AE could not start | `could not start` | modal phrase |
| Engine sometimes fails to start in dead slow | `engine sometime fail to start in dead slow` | bare `engine`, frequency adverb, condition clause (`dead`, `slow` in residue) |
| M/E failed to start during approaching berth | `failed to start during approaching berth` | past tense, condition clause — `during` is not a `SYMPTOM_PREPOSITION`, so not even residue |

1. **Verb form / modality.** `LEMMA_MAP` holds seven entries (leak, crack, break, trip, fracture); `_singular`
   turns `fails`→`fail` but `failed` stays; `could not` / `unable to` / `would not` are never mapped to `fail
   to`. Stage 2 of the cascade is exact key match and there is no synonym or paraphrase stage, so three
   spellings of one verb phrase are three keys.
2. **Circumstance.** `in dead slow`, `during approaching berth`, `sometimes` are neither component, location
   nor modifier, so by design they stay in the core — under-merge is the safe direction. Because `during` /
   `while` / `after` are not prepositions the splitter knows, such a tail is invisible to the residue report,
   which is why the stats step did not surface it.
3. **Bare head noun.** `equipment_forms("main_engine")` matches `main engine` / `m/e` / `me`, not `engine`.

The family layer is where these were meant to meet, and it does group them — that is why they sit under one
heading. Whether this is also what P3's shorter wordings will produce is not yet known: the v6 records are in
the scratch store, not in `data/extractions`.

## 7. The fix (P1b, docs/16 — **built 2026-10-26**; the result is in docs/13 §8)

A `config/symptom_phrases.csv` verb-phrase table (`could not start | unable to start | failed to | fails to` →
`fail to start` / `fail to`) and the regular past/-ing forms added to `LEMMA_MAP` (still an explicit map, no
stemmer); two more parts extracted like `location` — **`condition`** (`config/condition_terms.csv`: dead
slow, approaching berth, manoeuvring, after blackout, `above <n> rpm` …) and **`frequency`**
(`frequency_terms.csv`: sometimes / intermittently → intermittent); `during | while | when | after | before`
added to the preposition list so an unknown "when" tail at least reaches residue; bare head-noun equipment
match when no component term claims the word. Guard extended to the new tables. Pinned by tests: the three
wordings → one `fail to start` standard with condition / frequency on the alias row; the LO-sump pair → one;
`knocking sound` vs `vibrating sound` **stay two** (different failure modes; the near-synonym rule in docs/15
§7 is unchanged). Expected on the current store: fails_to_start −2, LO sump −1.
