# docs/ — index

Numbered, one topic each. Later numbers supersede earlier ones only where they say so.

| Doc | What |
|---|---|
| `01-problem-statement.md` | why — goal, domain, constraints |
| `02-design-and-plan.md` | what — extraction (Part 2), graph (Part 3), search, eval, phasing. Design of record. |
| `03-repo-guide.md` | where things go and the conventions |
| `04-extraction-schema-review.md` | review of `vessel_issues_schema.json` against 02 §2.5 and the 40-casefile export; attachment handling; tool list |
| `05-action-log-and-outcomes.md` | fault row (symptom+equipment[+problem]) as the one FK on each action; outcomes stated on the action with quote; indexes; derived outcomes reviewed and not adopted |
| `06-casefile-extract-v2.md` | field list of record for the per-casefile extract: v1 fields removed, `equipment[]`/`faults[]`/`action_logs[]` added, the 5-mail window build loop. Schema + KING ONE sample in `schema/` |
| `07-mapping-to-mongo-collections.md` | the v2 record is `vessel_issues_incident_reports`; the attachment collection is a sha-keyed digest store (`schema/attachment_digest.v1.schema.json`); the join contract between the two; field-by-field mapping to the team's sketch |
| `08-processing-pipeline.md` / `.drawio` | the end-to-end order of operations (header → 5-mail window loop → consolidate → validate → store) and KING ONE through it |
| `09-extraction-test-set.md` | the 10 casefiles for the first real v2 run, what each one tests, coverage; ids in `eval/test_sets/extraction-10.json` |
| `10-extraction-run-guide.md` | how to run it: `scripts/extract_casefiles.py`, controls (which casefiles, how many windows), output layout, cost levers, known limits |
| `11-extraction-changes.md` | record of work: the pipeline as built, every change in order with the review finding that caused it, schema additions, validation flags, results of the first full run, what was left to the loader |
| `12-casefile-browser-ui.md` | the static casefile browser (POC, built): `scripts/build_site.py` renders the run store into `data/site/` — search + facets, casefile/mail/attachment pages, admin lookup tables, review queue, per-casefile build journey; scaling path to served/resolved |
| `13-component-lookup.md` | the alias table (02 §3.9 stage 1): rule-based cleaning, the tables (`standard_term`, `alias` + `parts`, `family`, `pending_term`, `resolution_log`), deterministic standard key, display name as a view, three review queues (auto-minted, no family, queued-not-minted), the mint-guard hook, shared config (`config/modifier_terms.csv`, `component_terms.csv`, `location_terms.csv`, generated `config/vessel_names.txt`), re-resolve as rebuild + diff (`scripts/diff_lookup.py`), table-driven tests; §8 symptom decomposition (A1: the standard is the failure mode, component resolved as a `part`, location / modifiers on the alias row, the ON column, link retirement); §3 equipment-label hygiene (vessel strip with sister-ship log, glossary + adjudicator, record strip, qualifiers; `equipment_glossary.csv`, `equipment_qualifiers.csv`, `vessel_names_extra.txt`; `--check`); §8 symptom-core hygiene (verb-phrase canonicalisation, condition / frequency parts, head noun, review queue 4 `scripts/near_duplicates.py`); `src/resolution/`, `scripts/build_lookup.py` |
| `14-window-inputs-audit.md` | what the model was given per window: the `inputs` block of `windows/wNNN.json` and the `wNNN.prompt.txt` sidecar, schema as stored, one real example, how the journey page shows it |
| `15-resolver-hygiene-review.md` | review of the two resolver-hygiene prompts (symptom decomposition, equipment cleaning) against the real `normalize.py`/`lookup.py`; what the prompts assumed that does not exist here; the reviewed `broader_than` decision Prompt A would reverse; adapted prompts to run; `pending_term` / `resolution_log` / rebuild-and-diff as the shared pieces |
| `16-resolver-hygiene-prompts.md` | the four prompts that implement the resolver hygiene decided in 15 (A1, components as `part`, rebuild + diff): P0 `pending_term` / `resolution_log` / `alias.parts` / `diff_lookup.py` / test table, P1 symptom decomposition, P2 equipment cleaning, P3 extractor short-symptom (conditional) — P0–P3 built 2026-10-25, P1b open |
| `17-resolver-hygiene-changes.md` | record of work for the 2026-10-25 resolver changes in one place: the P0–P3 pieces as built, the numbers before/after, the drawback (decomposition splits WHERE, not verb form or circumstance — `fails_to_start` still three standards) and the P1b fix |
| `18-graph-node-extraction-methods.md` | proposal (rev. 2026-11-06): one graph from the v2 extracts on a fixed spine `Symptom → Equipment → Problem → Action → Result` — five canonical node tables, four hop tables carrying the occurrence data (`casefile_id`, `fault_id`, `seq`, `evidence_id`), SQL as the store of record with NetworkX as a view, v2-row → hop mapping, action/problem resolution (rule key, scoped similarity, families), threads, the retrieval query, metrics, build order |
| `19-cross-casefile-dedup.md` | proposal: de-duplication across casefiles at three levels — same kind (canonical ids), same instance (threads, scoped before scored, 6-month recurrence rule), same event (`duplicate_of`, never delete); `attempt_class` view (new / novel_for_problem / retry / did_not_hold / spread / follow_up), `effective_result`, the `action_efficacy` view with minimum support, regression cases |
| `20-graph-as-its-own-store.md` | proposal: the graph is the system of record, not a cache of the extracts — graph-owned ids, four memory layers (assertions with assert/retract, vocabulary & curation, derived experience, citations incl. the extract snapshot), the idempotent `ingest` contract, stored `next` edges and incremental aggregates, curation ops, what it amends in 18/19/02, acceptance tests |
| `vessel_issues_schema.json` | Mongo layout sketch for the attachment and incident-report collections (under review in 04) |

Hand-maintained vocabularies live in `config/`: `symptom_families.csv` (the bounded Symptom facet, ~20 rows),
`seed_symptom_aliases.csv` (reviewed wording → standard → family), `symptom_links.json` (reviewed broader_than
links — empty since the reviewed example dissolved under A1), `modifier_terms.csv` (position / side words, left → port,
right → starboard), `component_terms.csv` and `location_terms.csv` (what a symptom wording names, docs/13 §8), `symptom_phrases.csv`,
`condition_terms.csv`, `frequency_terms.csv` and `equipment_head_nouns.csv` (how it is said, when, how often, the bare equipment noun — P1b),
`equipment_glossary.csv` and
`equipment_qualifiers.csv` (what the equipment cleaner strips, docs/13 §3), `vessel_names_extra.txt` (hand-kept sister ships); `vessel_names.txt` there is
**generated** by `scripts/build_lookup.py` (the fleet list, a resolver stopword table). Equipment seeds in
`src/resolution/seed_aliases.csv`. Every CSV is header-checked at load. A facet lists canonical ids only, never
wording (12 §3).

Conventions: casefile `_id` is the identity, IMO identifies the vessel, `vesselName` is audit only;
`UICategory` / `tag` are hints, never filters; extraction reads fetched `pages[].link` bodies and attachment
bytes, never export summary fields; every stored assertion carries `mail_ids`.
