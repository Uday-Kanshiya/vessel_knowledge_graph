# Component lookup: the alias table (02 §3.9, stage 1)

> Status: **built, first run done**; shared hygiene pieces (pending queue, log, alias.parts, config tables,
> rebuild + diff) built 2026-10-25 (docs/16 P0); symptom decomposition (A1) built 2026-10-25 (docs/16 P1);
> equipment-label hygiene built 2026-10-25 (docs/16 P2); symptom-core hygiene (how-said / when, near-duplicates
> queue) built 2026-10-26 (docs/16 P1b)
> Date: 2026-10-21 · 2026-10-25
> Code: `src/resolution/` · `scripts/build_lookup.py` · `scripts/diff_lookup.py` · tests
> `tests/unit/test_resolution_lookup.py`, `tests/unit/test_resolution_cases.py` (+ `resolution_cases.csv`)
> Earlier note superseded: the single-table `component_lookup (raw_term, normalized_term)` sketch.

## 1. What it does

Equipment and part names come out of extraction in whatever spelling the mail used: `M/E`, `Main engine`,
`No. 3 AE`, `Auxiliary Engine No.4`, `exhaust valve safety valves`. The alias table gives every spelling a
stable **standard id**, deterministically, so records and the site can carry the id and show one name.

This is the first, cheapest stage of the resolution cascade in 02 §3.9 (alias → normalised exact →
embedding → LLM adjudicator → new-term queue). Only this stage exists. Nothing here is fuzzy, and a name
the rules cannot place is **never forced to its nearest neighbour**: it is minted as a new standard with
status `auto` and shows up in the review queue.

## 2. The tables

```
standard_term   id · kind (equipment | part | symptom) · key · status (auto | reviewed | merged) · merged_into
                · source (seed | rule | human) · first_seen · note · family_id · family_method (§8)
alias           kind · raw_key · raw_term · stem · standard_id · method · source
                · first_seen · last_seen · hit_count · parts (JSON, nullable)
family          §8
pending_term    kind · raw_key · raw_term · reason · parts (JSON, nullable) · source
                · first_seen · last_seen · hit_count            PK (kind, raw_key)
resolution_log  id · ts · kind · raw_key · method_chain · before · after · standard_id (nullable) · rationale
```

Why standards and aliases are two tables: the standard is referenced by **id**, so curating it (merge,
approve) moves nothing that points at it. Why the extra columns: `status` separates a human-approved
standard from a rule guess (the design's "fragmentation rate" metric needs that split), `hit_count` /
`first_seen` / `last_seen` let the review queue be ranked, `source` says which casefile or seed row first
produced a spelling.

**`alias.parts`** (2026-10-25) is whatever the cleaner took apart for that spelling — `Cleaned.parts`, a
dict, `None` today; P1 fills it with the decomposed symptom label (core, component, location, …), P2 with
the stripped equipment label (vessel, glossary, residue, …). `alias_rows()` returns it as a dict;
`display_name()` does not read it.

**`pending_term`** is where a refused mint lands (§4 case b′): a spelling the mint guard would not turn
into a standard. It is **not** a standard, so `find()` keeps returning `None` for it (the name stays
unresolved on the site) and it never joins a facet. `reason ∈ undecomposed | needs_translation |
contains_vessel | contains_position | contains_identifier | candidate_term`. One row per (kind, raw_key);
a repeat bumps `hit_count`.

**`resolution_log`** says *why* a spelling landed where it did: one row per decision that was not a
plain alias hit — `resolve()` (methods normalised / new_term / queued), `merge()`, `approve()`,
`set_alias()`, `queue()` (first sighting), `promote()`, `dismiss()`. `method_chain` is the ordered step
names that changed the text, `+`-joined (`normalised` alone today; `vessel_strip+normalised` once P2
records steps in `Cleaned.parts["steps"]`); `before` / `after` are the text going in and the key coming
out. `alias.method` stays one word (`seed | normalised | new_term | human`); the chain lives here and in
`alias.parts`. Read with `log(kind=None, since=None)`. Since P2 the equipment cleaner's steps lead the chain
(`vessel_strip`, `glossary`, `adjudicator`, `record_strip`, `qualifier`, `position`, `abbreviations`), so
`normalised` alone means the label needed no cleaning.

`raw_key` is the casefolded, punctuation-stripped raw spelling (position kept), so `M/E C.W. P/P` and
`m/e c.w. p/p` are one alias row, not two. `raw_term` keeps the first spelling seen for display.

## 3. The standard key is deterministic, the name is a view

**Key.** `normalize(raw).key` is a pure function of the text: casefold, expand abbreviations (`M/E`, `A/E`,
`F.O.`, `L.O.`, `S.W.`, `C.W.`, `P/P`, `GEN`, `HFO`, `EGCS`, … tables in `normalize.py`), strip
punctuation, fold plurals, drop quantity / condition words (`2 pcs`, `new`, `spare`), and move position
tokens out. So `ME CW PP`, `Main Eng Cooling Water Pump` and `No.1 M/E C.W. P/P` all mint or match the
same key `main engine cooling water pump` **whichever arrives first**. Two corpora processed in different
orders converge on the same vocabulary. There is no "first arrival picks the name" problem because no name
is stored.

**Cleaning before the key (2026-10-25, docs/15 §6).** `normalize.clean_equipment_label(label, context)` runs
first and STRIPS from the name what the v2 record already carries as fields — nothing is extracted into new
storage; the record is the occurrence. Steps, each a name in the log chain when it changed the text:

| step | what | table | kind |
|---|---|---|---|
| `vessel_strip` | a fleet vessel name, whole name only, written with spaces / hyphens / run together (`PACIFIC GARNET`, `Pacific-Garnet`, `PACIFICGARNET`); never a substring (`ENGINE STAR` leaves `engine` alone). A name that is not the record's own `vessel_name` is a **sister ship**: kept in `parts.sister_vessel` and the log rationale says `label names sister vessel X` | `config/vessel_names.txt` (generated) + `config/vessel_names_extra.txt` (hand-kept: ships with no casefile, e.g. PACIFIC GARNET) | equipment |
| `glossary` | when the label has non-Latin script: `N号` → `No.N`, then longest-first replacement; `parts.script` = lang. Non-Latin text LEFT OVER goes to the **adjudicator** (`LookupStore(adjudicator=Callable[[str], str \| None])`, the model stage; default `None`) and, untranslated, stays as `parts.untranslated` → guard `needs_translation`. A standard key never carries non-Latin characters | `config/equipment_glossary.csv` (`source_term, lang, english, kind ∈ equipment \| position \| modifier`; zh seeded from KING ONE's 右主机 / 左主机, ja / ko equivalents) | all |
| `record_strip` | whole-phrase matches of the entry's own `maker` / `model` / `serial` (as written, casefolded, punctuation-stripped); `NA` / blank skipped. No corpus-wide maker list: 12 of 19 makers are `NA` | the record entry | equipment |
| `qualifier` | condition words (`new`, `old`, `spare(s)`, `replacement`, `existing`, `damaged`, `defective`) → `parts.stripped["qualifier"]`; quantity words (`pcs`, `set`, `x`) stay in `NOISE_WORDS` | `config/equipment_qualifiers.csv` | all |
| `position` | the existing `_strip_position`: numbered units, `1S` / `(P)`, the modifier table (`left → port`, `right → starboard`); a hyphenated `Port-side` counts too | `config/modifier_terms.csv` | all |
| `abbreviations` | the existing expansion + `_singular` → the key | `PUNCTUATED_ABBREVIATIONS`, `ABBREVIATIONS` | all |

`record_strip` and `qualifier` run on the raw tokens before the expansion (so `MAN B&W` is matched as written);
the chain lists the steps in the order they fired, e.g. `vessel_strip+position+abbreviations+normalised`,
`glossary+position+normalised`. `EquipmentParts(core, position, side, stripped, script, residue, stem, vessel,
sister_vessel, untranslated, steps, issues)` goes to `alias.parts`; `residue` is what a regex (the leading
quantity) dropped. A `part` label runs glossary, qualifier, position and abbreviations only (no record entry).
On the 10-record store this folded `PACIFIC GARNET AE #1/#2` into Auxiliary Engine (No.1 / No.2), `右主机` /
`左主机` into Main engine (starboard / port) and `Port-side forward chain-stopper guillotine bar` into `chain
stopper guillotine bar` (port forward): equipment standards 16 → 13, nothing queued.

**Position.** `No.4`, `#2`, `1S`, `(S)`, `port`, `stbd`, `left`, `forward` … leave the key and come back
as `Resolution.position`. The record's own `position` field is never rewritten; the casefile page shows the
canonical form (`views.canonical_position`: `right` → `starboard`) with the wording as written beside it. The v2 record already has `equipment_type` and `position` as separate fields
(06), and two pumps are one type. `normalize("No.2")` is position only and resolves to nothing.

**Display name.** `LookupStore.display_name(id)` picks, at render time, the **longest alias stem that
contains no abbreviation** (ties broken by hit count), position tokens removed; falls back to the key when
every spelling is abbreviated. Add `Main Engine Cooling Water Pump` as a spelling and the group's shown
name improves on the next render. Nothing is renamed, ever. The site and any exported record carry the
`standard_id` and render the name from the table.

## 4. Update rule (one raw name at a time: `LookupStore.resolve`)

| | condition | action | `method` |
|---|---|---|---|
| 1 | `raw_key` already in `alias` | bump `hit_count`, `last_seen` | `alias` (or `human` if pinned) |
| 2 | not in `alias`, but `key` is an existing standard | insert alias row | `normalised` (case a) |
| 3 | not in `alias`, `key` unknown, guard says nothing | mint `standard_term` with status `auto`, insert alias | `new_term` (case b) |
| 3′ | not in `alias`, `key` unknown, guard returns a reason | upsert `pending_term`; **no** standard, **no** alias | `queued` (case b′) |

`kind` is a separate namespace (`fuel pump` as equipment and as a spare part are different ids).
`resolve(..., mint=False)` answers without minting (and never queues), for callers that want only known
standards.

**The mint guard** (2026-10-25). `resolve(raw, kind, source, mint, guard=None)` takes
`guard: Callable[[kind, Cleaned, raw], reason | None]`; `LookupStore(guard=…)` sets the default. It is
asked only for a would-be `new_term`: a spelling whose key already exists is never queued, whatever it
carries. When it returns a reason the spelling goes to `pending_term` and the caller gets
`Resolution(standard_id=None, method="queued", reason=…)` (key / position / display still filled from the
cleaned text). No guard is wired yet — every unknown key is minted as before; P1 (undecomposed symptom,
candidate term) and P2 (vessel, position, non-Latin, maker / serial in the label) supply the real ones.
`Resolution.method ∈ alias | normalised | new_term | human | queued` (checked in the dataclass). A pending
spelling that later resolves (its key gets minted by a cleaner spelling, or a human pins it) leaves the
queue by itself.

Guards wired by `build_lookup.py` (`lookup.DEFAULT_GUARD = compose_guards(symptom_guard, equipment_guard)`):
`symptom_guard` (§8, `undecomposed`) and `equipment_guard` for equipment / part, which reads
`parts.issues` — what the cleaner (§3) could not get out of the key: a fleet vessel name →
`contains_vessel`, a position / side token → `contains_position`, non-Latin text → `needs_translation`,
the entry's maker / model / serial → `contains_identifier`. The first issue is the reason.

## 5. Curation

- `approve(id, note)` — `auto` → `reviewed`.
- `merge(from_id, into_id, note)` — declares two standards one part; aliases move, the losing row stays
  with status `merged` and `merged_into`, so the decision is visible and reversible. Later resolves follow
  the chain.
- `set_alias(raw, standard_key)` — pins a spelling to a standard, overriding the rules (`method = human`);
  this is also how the later embedding / adjudicator stages write their decisions back. It also clears a
  pending row for that spelling, so a seed-CSV line is the file-based way to promote.
- `promote(kind, raw_key, standard_key)` — `set_alias` of a pending spelling + delete from `pending_term`
  (logged `promoted`); `dismiss(kind, raw_key, note)` — drop it, create nothing (logged `dismissed`).
- **Three queues**: `review_queue()` = standards with status `auto` (queue 1); `unclassified("symptom")` =
  symptom standards with no family (queue 2, §8); `pending()` = queued, not minted (queue 3, hits desc).
  `fragmentation()` carries `n_pending` per kind. `build_lookup.py --review` prints all three, each entry
  with the seed-CSV row that fixes it (`kind,raw_term,<standard_key>,reviewed[,<family_key>]` for a
  pending row); the lookup page shows the third list under "Needs attention" and `admin/review.html`
  repeats it (`views.resolver_tables()["pending"]`).
- `log(kind, since)` — the `resolution_log` rows (§2) for an audit of what moved and why.
- CSV: `import_csv` / `export_csv` with columns `kind, raw_term, standard_key, status`. The repo ships
  `src/resolution/seed_aliases.csv` (status `reviewed`); edit it, or export, edit in a spreadsheet, and
  re-import with `--seed`. The SQLite file is a rebuildable cache of CSV + what extraction has seen.

Ambiguous abbreviations (`C.W. P/P` = HT, LT or jacket cooling depending on the vessel) are **not**
expressible in a one-to-one table and are not forced here; they are the adjudicator's job.

## 6. Running it

```
python scripts/build_lookup.py --fresh --review --export data/resolution/aliases.csv
```

Regenerates the fleet list, loads the seeds, resolves `equipment[].name`, `action_logs[].parts[]` and
`faults[].symptom` of every record under `data/extractions/`, prints fragmentation numbers and the three
review queues, writes the CSV. Idempotent; re-run after each extraction. Settings: `RESOLUTION_DB_PATH`
(default `<EXTRACTION_DATA_DIR>/resolution/lookup.sqlite`), `RESOLUTION_SEED_CSV`, `RESOLUTION_CONFIG_DIR`
(default `config/`), `CASEFILE_EXPORT_PATH` (for the fleet list only).

`--check` (2026-10-25): every non-merged equipment standard that extraction produced must have at least one
alias whose record entry carries an `equipment_type` (the type is a view over the records, §3; a standard
nobody typed cannot be grouped on the lookup page). Offenders are printed and the exit code is 1. Standards
with seed aliases only are reported as "seed-only (not checked)": the seed CSV has no type column by design.

**Shared config tables** (`LookupStore(config_dir=…)`, 2026-10-25), all header-checked at load — a wrong
header (or a seed CSV with the wrong columns) raises and the build fails instead of reading nothing:

- `config/component_terms.csv` (`term,canonical_part_key`) and `config/location_terms.csv`
  (`term,canonical_location`) — the component nouns and places a symptom wording can name (§8); both optional
  (empty when missing), both header-checked.
- `config/modifier_terms.csv` (`term,canonical`) — the position / side words that leave the key and come
  back as `position`, replacing the old `POSITION_WORDS` dict in `normalize.py`. Same behaviour plus
  marine convention: `left → port`, `right → starboard` (`right main engine` → `main engine`, `starboard`).
- `config/equipment_glossary.csv` (`source_term,lang,english,kind`) and `config/equipment_qualifiers.csv`
  (`term,canonical`) — the non-Latin equipment words and the condition words the cleaner strips (§3); both
  optional, both header-checked.
- `config/vessel_names.txt` — one name per line, **generated** by `build_lookup.py` from every record's
  `vessel_name` and every `vesselName` in the casefile export (fleet, so sister ships are in; the header
  says so; `--no-vessel-names` skips it), merged with the hand-kept `config/vessel_names_extra.txt` for
  ships the export never names (PACIFIC GARNET). `vesselName` is audit-only everywhere else; this stopword
  use is its one load-bearing role, as `mail.subject_stem` is. Read by the vessel strip (§3).

**Re-resolve = rebuild + diff, never a migration.** Records carry no standard ids, so a rule change is
tried by building a second file and comparing:

```
python scripts/build_lookup.py --db /tmp/new.sqlite --fresh
python scripts/diff_lookup.py data/resolution/lookup.sqlite /tmp/new.sqlite --md reports/lookup_diff.md
```

`diff_lookup.py OLD NEW [--kind K] [--md out.md]` opens both read-only and reports per kind: standards
before / after; aliases whose standard **moved** (old key → new key, by `raw_key`); spellings now
**pending** and why (and what they resolved to before); **residue** token frequency from
`alias.parts.residue` + `pending.parts.residue` (top 30, with distinct-casefile counts); new and vanished
standards. Standards are compared by key (ids are per file). Exit 0 always: it reports, it does not judge.
Only when the diff reads right, rebuild the real DB with `--fresh`. The current DB rebuilt against itself
on 2026-10-25 diffs empty (16 / 35 / 37 standards, 29 / 38 / 41 aliases, 0 pending). Core in
`src/resolution/diff.py` (`snapshot`, `diff`, `render_md`).

**Tests are table-driven**: `tests/unit/resolution_cases.csv` (`kind, raw, vessel_name, equipment_type,
expected_key, expected_parts_json, expected_method`) is read by one parametrised test in
`tests/unit/test_resolution_cases.py` through a fresh store with the repo config and no seeds; add a row
whenever a config CSV grows. `tests/fixtures/lookup_pre_p0.sqlite` is a pre-2026-10-25 file that
`_migrate()` must still open (it adds `alias.parts` and the two new tables).

First run over the 10-casefile store (2026-10-21):

| | |
|---|---|
| raw names resolved | 15 equipment · 47 parts |
| standards | 58 (6 reviewed from seed, 52 auto) |
| aliases | 72, of which 51 standards have a single spelling |

Equipment collapses well (`M/E`, `main engine`, `Main engine`, `right main engine`, `left main engine` are
one id; `Auxiliary Engine No.4` and `No. 3 AE` are one id with two positions). Parts are mostly singletons
because the extractor copies purchase-order lines (`Steel Plate Ungalv Hot-Rolled, 12X914X1829Mm,
IMPA670715, 1 SHT`); those are exactly what the review queue and the LLM stage are for, not more regexes.

## 7. Boundaries and what is not here

- Part 3 code: `src/extraction` does not import `src/resolution` (03 §2). Ids are attached at load /
  render time, not inside the record the extractor writes.
- The admin lookup page (12 §5.1) still derives its tables from the records. Switching its source to
  `LookupStore.alias_rows()` / `review_queue()` is the next step; the page layout does not change.
- No embedding or LLM stage, no `StepType` / `Problem` trees, no PMS-derived ontology seed yet (02 §3.9).

## 8. Symptom kind and the family layer (2026-10-21; A1 decomposition 2026-10-25)

The Symptom facet listed every distinct normalised wording and grew with the corpus. A facet must be bounded
by construction (12 §3), so the resolver gained a third kind and a layer above standards.

**`symptom` kind.** `KINDS = (equipment, part, symptom)`. The key function is per kind
(`normalize.normalize_for_kind`): equipment / part keep `normalize()`; symptom uses
`normalize_symptom(wording, equipment_type)` = the failure-mode **core** of `decompose_symptom()` (below).
`resolve(raw, kind="symptom", context={"equipment_type": …})` then works exactly like equipment: alias →
normalised → new_term (status `auto`, review queue 1), or → queued when the mint guard refuses (§4, case b′).
The display stem keeps the casing of the core. `find()` is the read-only form the site build uses: by alias,
else by key, never inserts, never counts — a wording with no alias yet is unresolved, not minted.

**A symptom standard is the failure mode only (A1, docs/15 §3, decided 2026-10-25).** The earlier rule
"component nouns stay in the key — they localise the symptom" is withdrawn: `knocking sound from exhaust
valve`, `knocking sound from main engine exhaust valve` and `knocking sound` are **one** standard,
`knocking sound`, and the component is structured data on the alias row (`alias.parts`, §2). The component
resolves through the existing **`kind="part"`** (`LookupStore._resolve_component`, log chain
`symptom_component+normalised` / `+new_term`), so a symptom's component and an `action_logs[].parts[]` line
share one standard id — "knocking from exhaust valve" and "renew exhaust valve" meet there. Location and
modifiers are strings from the CSV canonical columns; no resolver kind for them.

**`decompose_symptom(label, equipment_type)` → `LabelParts(core, component, location, modifiers, position,
residue, stem, component_raw, vessel, steps, leftover)`**, in `normalize.py`; deterministic; tables, not code:

| step | what | table |
|---|---|---|
| lemma | `LEMMA_MAP` (leaking→leak, cracked→crack, broken→break, tripped→trip, fractured→fracture) + `_singular`; capitalised acronyms (EGCS, RPM) untouched. A stemmer is not acceptable | — |
| vessel strip | a fleet vessel name, whole name only | `config/vessel_names.txt` |
| position | `_SYM_POSITION_RE` (`cyl 4`, `unit no:2`, `#3`, `No. 6`) → `position` | — |
| extraction | (a) **tail**: the tokens after the LAST preposition (`from at on in of around inside below above into`) up to the end of the clause (`,` `;` end a clause) must all be table terms, articles or the record's equipment words → extracted, preposition dropped; an unmatched tail stays in the core and its unknown words go to `residue`, and its terms are left alone; (b) **head**: table terms at the start of the label; (c) **bare**: a term anywhere else fills an empty slot, else stays | `config/component_terms.csv` (term → canonical part key), `config/location_terms.csv` (term → canonical location), `config/modifier_terms.csv` |
| equipment strip | ONLY the record's own `equipment_type` words and their abbreviations (`main_engine` → main engine, m/e, m e, me), transparent during matching, dropped at the end. `SYMPTOM_EQUIPMENT_WORDS` is deleted: it ate component nouns (docs/15 §2: `fuel pump spring` became `fuel spring`) | — |
| core | what remains, stranded function words dropped, `_SYM_TRAIL_RE` last | — |

Rules worth knowing: a run of adjacent component terms is **one** component phrase (`fuel pump spring`,
`cylinder head water jacket`), resolved as that part — splitting it would put a component noun back into the
key. The component slot is single and the first filled wins (tail, then head, then bare): in `Bolts of the
support shaft cover plate for the starboard main engine idle gear were found fractured` the component is
**`bolt`** (the head term is what fractured); `cover plate`, `idle gear` and `starboard` stay in the core, so
the label is **queued as `undecomposed`**, because one standard cannot carry two components — a human
decides. A label whose core would be empty (`Exhaust valve`) passes through whole and is queued too. A label
with no table term passes through whole (residue empty, or set when it had an unmatched tail).

**Mint guard** (`lookup.symptom_guard`, wired by `build_lookup.py`): a would-be new symptom standard whose
core still carries a component / location / modifier term (`LabelParts.leftover`) is queued with reason
`undecomposed`. After the build, residue tokens seen in ≥ N distinct casefiles (`--residue-min`, default 2)
are queued once each as `candidate_term` with `parts = {token, casefiles, propose:
"component_terms|location_terms"}`: the tables grow from evidence, not guesses.

**Links retire.** A `config/symptom_links.json` row whose `narrower` key no longer exists after decomposition
is retired by `symptom_links.retire_dissolved()` during the build: removed from the file, logged
(`method_chain = link_retired`, rationale `dissolved into <core> + component <x>; note: …; reviewed_by: …`)
so the reviewer's note survives. `links.validate()` ends with no warnings. The reviewed example (knocking sound
> knocking sound from exhaust valve, 2026-10-21) was retired on 2026-10-25; the file is empty.

**HOW it is said and WHEN / HOW OFTEN (P1b, 2026-10-26; the drawback in docs/17 §6).** Decomposition split
WHERE; one failure mode still minted several standards whose cores differed in the verb phrase (`could not
start` / `fails to start` / `failed to start`) or in the circumstance (`in dead slow`, `during approaching
berth`, `sometimes`). Three more tables and one more rule, all explicit; nothing fuzzy:

| what | table | how |
|---|---|---|
| verb phrase | `config/symptom_phrases.csv` (`phrase,canonical,notes`) | applied to the CORE after extraction, before the key: longest-first, whole-word, on lemmatised tokens (`could not start`, `unable to start`, `failed to start`, `does not start` → `fail to start`; `not activating` → `fail to activate`; `not operating` / `inoperative` → `fail to operate`; `could not` / `unable to` / `failed to` → `fail to`; `shut down` / `stopped` → `stop`; `tripped` → `trip` — stop and trip stay apart, as the families do). The display stem takes the canonical words for the span, so the standard shows as `fail to start` |
| verb forms | `LEMMA_MAP` | the explicit past / -ing / 3rd-person forms of every verb in it (`failed`, `failing`, `activated`, `operating`, `stopped`, `flooded`, `chipped`, `imposed`, `stabilized`, …). Still a map, never a stemmer: a form not listed stays as written, is reported as residue (`unmapped_inflection`) and makes the label undecomposed |
| condition (WHEN) | `config/condition_terms.csv` (`term,canonical`) | extracted like a location into `parts.condition`: `dead slow`, `approaching berth`, `manoeuvring`, `after blackout`, `at sea`, `in port`, `low load`, and number patterns `above <n> rpm` / `below <n> rpm` / `at <n> load` (`<n>` matches one number and is carried: `above 80 rpm`). A one-word row that is a plain symptom word (dead, slow, hot, …) is refused at load: `battery dead` keeps `dead` |
| frequency (HOW OFTEN) | `config/frequency_terms.csv` | `sometimes` / `intermittently` / `occasionally` → `intermittent`; `always` / `every time` → `persistent`; `once` → `single`; into `parts.frequency` |
| "when" tails | `SYMPTOM_PREPOSITIONS` | `during`, `while`, `when`, `after`, `before`, `since` join the list: a tail after one of them is matched against the condition table (the preposition may be part of the row: `in port`, `above <n> rpm`) and, unknown, stays in the core and reaches residue — before this it was invisible |
| bare head noun | `config/equipment_head_nouns.csv` (`equipment_type,head_noun`) | `engine` on a main_engine record is the equipment: stripped after extraction, ONLY when no component / location term claimed the word (`engine room` stays a location). Explicit rows; the last word of a type is not a head noun by default |

Guard: `undecomposed` now also covers a core still carrying a phrase-table row (one that is not its own
canonical), a condition term, a frequency term, or an unmapped inflection. Condition and frequency join the
**on / when** column of the lookup page and the casefile fault row (`<core> · on … · when <condition> ·
<frequency>`); the search page gets a `condition` sub-facet under Symptom only when some value has ≥ 2
casefiles behind it (`search.index.CONDITION_MIN_CASEFILES`; none does on 10 records).

**Review queue 4 — possible duplicates** (`scripts/near_duplicates.py`, `src/resolution/near_duplicates.py`;
read-only). Inside each family, every pair of standard cores is scored with the thread's lexical stand-in
`(token_set_ratio + token_sort_ratio) / 200` (rapidfuzz) and kept at or above the thread bar 0.72. The two
token sets are diffed and every differing token tagged: `verb_form`, `modality`, `condition`, `frequency`,
`head_noun`, `where` (a where-term in a core: a decomposition bug), `failure_mode` (a word a family pattern
names: knock, vibrat, leak — the two cores are different failure modes, docs/15 §7), `unknown`. Verdict:
**MERGE-BY-TABLE** <tables> when every differing token is tagged and none is unknown or failure_mode; **KEEP**
when a failure-mode word differs; **REVIEW** otherwise. Pending (undecomposed) spellings are scanned against
the standards too. `build_lookup.py --review` prints it as queue 4; `admin/review.html` shows it; `--family`
and `--json` for the diff step. After Phase 2 a MERGE-BY-TABLE verdict is a bug. A finding worth knowing:
the bar misses modality pairs — `could not start` vs `fail to start` scores 0.65, the three fails-to-start
cores 0.43–0.49 — so the family grouping, not the lexical scan, is what surfaced the backlog; the scan does
catch verb-form and single-word differences.

Rebuild on 2026-10-26 (10 records): symptom standards 30 → 26 — the three fails-to-start → `fail to start`
(parts: `position no.1`; `condition dead slow` + `frequency intermittent` + head noun `engine`; `condition
approaching berth`), the LO-sump pair → `lo high/low alarm fail to activate` (`sump level` → location
`sump`), the discharge-valve sentence with three `sometimes` now queued (one frequency slot), `knocking
sound` untouched. Phase 1 before: 2 REVIEW, 0 MERGE-BY-TABLE; after: 1 REVIEW (`closing delay alarm` vs
`closing too slow alarm`, a human's call), 0 MERGE-BY-TABLE.

**Lookup page.** The Symptoms table has an **on / when** column: per standard, the distinct component ·
location · modifier · condition · frequency combinations its spellings carried, with hit counts, the component
shown by its part standard's display name (`views.resolver_tables()["symptoms"][…]["on"]`). The casefile
page's fault row shows `<core> · on <component> · in <location> · when <condition> · <frequency>` under the
wording when any is set (`views.fault_decomposition`). **Deferred:** COMPONENT / LOCATION search sub-facets —
not until the store is past ~50 records (it has 10).

First run after A1 (2026-10-25, 10 records, 41 wordings): 21 split, 14 passed through whole, 8 with residue,
6 queued undecomposed, 0 candidate terms (no residue token in two casefiles); symptom standards 37 → 30; part
standards 35 → 45 (ten components); one link retired. Most sentence-length wordings still pass through whole
or get queued: the lever is the extractor prompt (P3 in docs/16), not more tables.

**Known drawback after A1** — decomposition splits WHERE but not verb form or circumstance, so `fails_to_start`
still holds three standards; diagnosis and fix (P1b) in docs/17 §6–§7.

**`family` table** — `id, kind, key, display, description, patterns, status (reviewed), sort_order`, plus a
nullable `standard_term.family_id` and `family_method`. Families are **never minted by rules**: the only way
one comes into being is `import_families_csv()` reading `config/symptom_families.csv`
(`key,display,description,patterns,sort_order`; `#` lines are comments; ~20 rows, marked as a proposal).
API: `families(kind)`, `family_of(standard_id)`, `set_family(standard_key_or_alias, family_key, kind, source)`
(raises on an unknown family), `unclassified(kind)` (standards with no family, hits desc — review queue 2),
`assign_families(kind, ancestors_of)`, `fragmentation()` per kind with an `unclassified` count. `export_csv`
gains a `family_key` column (re-importable; blank = unchanged). The same table has `kind=equipment` as a hook
for the future equipment family — the SFI group — left unpopulated on purpose.

**Family assignment of a symptom standard**, deterministic, in precedence order:

1. human / seed — a CSV row: `config/seed_symptom_aliases.csv` (`kind,raw_term,standard_key,status,family_key`)
   or `set_family(..., source="human")`; recorded as `human` / `seed` and never re-derived;
2. inherited — walk `ancestors(key)` through the reviewed `broader_than` links (`config/symptom_links.json`);
   the nearest ancestor with a family wins, so classifying one broad term classifies its subtree;
3. rule — the first family in `sort_order` whose `patterns` (`|`-separated, case-insensitive substrings matched
   on the normalised key) matches;
4. none — shows under the built-in pseudo-family `unclassified` and sits in `unclassified("symptom")`.

The method is stored on the standard so the lookup page can say why. Re-running `scripts/build_lookup.py`
re-derives 2–3 and never overrides 1. Standards are not merged by a family; each keeps its spellings and counts (and, since A1, its components live on the alias rows, not in the key).

**Script.** `build_lookup.py` imports the family list, the equipment seed and the symptom seed, resolves
`equipment[].name`, `action_logs[].parts[]` and `faults[].symptom`, then assigns families (links validated
against the standards present). `--review` prints both queues with the CSV row that fixes each entry,
`--families` prints family → standards → hits, `--export` includes `family_key`. The site build opens the DB
read-only; the index carries ids and labels (12 §3).

First run over the 10-casefile store (2026-10-21): 20 families defined, 11 used; 37 symptom standards, 3
unclassified; the largest family (Alarm / trip, Wear / damage) holds 8 standards. Patterns are substrings, so
`stop` also matches `stopper` — the CSV is a proposal for a human to tighten, which is the point of keeping the
rules in data.
