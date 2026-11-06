# One graph from the casefile extracts: the five-node spine and how to load it

> Status: **proposal, for discussion** · Date: 2026-11-03, revised 2026-11-06 (spine fixed as
> `Symptom → Equipment → Problem → Action → Result`; SQL tables are the store of record, the graph is a view)
> Input: `data/extractions/<casefile_id>/record.json` (v2 extract), `data/resolution/lookup.sqlite`
> (13 equipment, 44 part, 26 symptom standards). Design of record for nodes/edges: docs/02 §3, amended here.
> Code target: `src/graph/` (empty), `scripts/build_graph.py`, `schema/kg.sqlite.sql`.
> **Amended by docs/20 (2026-11-06):** the store is the system of record, not a cache — ids are graph-minted,
> `--fresh` becomes `rederive` (derived layer only), ingest is assert/retract. Node and hop shapes below stand.

---

## 1. Principle: one direction, five node kinds, every hop a row

```
Symptom ──▶ Equipment ──▶ Problem ──▶ Action ──▶ Result
                                        ▲          │
                                        └── next ──┘   (a result leads to the next action, same fault, seq+1)
```

- A traversal always reads left to right; nothing points backwards. "How was X solved" is a walk from a
  Symptom to a Result, and the path it took **is** the answer.
- Each arrow is one table. A hop row carries `casefile_id`, `fault_id`, `seq` and an `evidence_id`, so any
  answer traces to the mail quote that supports it.
- Nodes are **fleet-wide canonical** (one row per standard id). Case-specific detail — raw wording,
  position, target, parts, dates, who did it — lives on the hop row, never on the node. That is what
  makes one node per thing possible (02 §3.1) and keeps the node tables small.
- `Result` is a node, not an edge property: a closed vocabulary (05 outcomes) so `GROUP BY result_id`
  is the fleet verdict and the chain always terminates.
- The SQL store is the system of record (docs/20); NetworkX (`GraphStore`) is loaded from it for rendering
  and path queries and never written to directly. This amends kg_build_decisions ("NetworkX only").

## 2. Node tables (canonical, one row per standard)

| Table | Key | Columns | Source of rows |
|---|---|---|---|
| `symptom` | `symptom_id` | `key`, `display`, `family_id` | `lookup.standard_term` kind=symptom (exists) |
| `equipment` | `equipment_id` | `key`, `display`, `equipment_type`, `family_id` | `lookup.standard_term` kind=equipment (exists); `equipment_type` is the v2 enum |
| `problem` | `problem_id` | `key` (`failure_mode:part`), `display`, `family_id` | new resolver (§5); row `undetermined` is seeded |
| `action` | `action_id` | `key` (`verb:object`), `display`, `verb`, `object_part_id`, `kind` | new resolver (§5) |
| `result` | `result_id` | `value`, `applies_to` (corrective… \| diagnostic) | seeded: resolved, partial, no_effect, made_worse, unknown, conclusive, inconclusive, no_information |

Support tables, not on the spine: `part`, `organisation`, `role` (from the v2 enums), `vessel(imo)`,
`casefile(casefile_id, imo, …)`, `evidence(evidence_id, mail_id, attachment_sha, quote)`,
`mention(kind, casefile_id, local_id, raw_text, standard_id, method)` — every raw wording and the standard
it resolved to, so re-resolution is a rebuild of `mention.standard_id`, nothing else.

## 3. Hop tables (one per arrow; the occurrence data lives here)

```
hop_symptom_equipment  casefile_id · fault_id · symptom_id · equipment_id
                       · position · first_observed_at · alarms (JSON) · recurrence_of_fault · evidence_id
hop_equipment_problem  casefile_id · fault_id · equipment_id · problem_id
                       · asserted_by · concluded_at · suspected_cause_text · evidence_id
hop_problem_action     casefile_id · fault_id · problem_id · action_id · seq
                       · occurrence_no · target · parts (JSON) · performed_by_role · advised_by_org
                       · date_performed · asserted_in_mail · evidence_id
hop_action_result      casefile_id · fault_id · action_id · seq · result_id
                       · verification · outcome_in_mail · outcome_date · finding_text · evidence_id
```

Keys: `(casefile_id, fault_id)` identifies a chain; `(casefile_id, fault_id, seq)` identifies one action in
it and is unique in both action hops. `result → next action` is **not stored** — it is
`hop_action_result(seq) ⋈ hop_problem_action(seq+1)` on the same `(casefile_id, fault_id)`, which is the
`NEXT` edge when the graph is built. Every fault has at least one `hop_action_result` row: a fault with no
actions gets a synthetic `seq 0`, action `none`, result `unknown`, so no chain is open-ended.

Why four narrow tables rather than one `edge(src, dst, type, attrs)`: each hop has different columns,
foreign keys are enforceable, and the retrieval query is plain joins with no JSON parsing.

## 4. Loading the v2 record into the hops (deterministic, no model)

| v2 row | Produces |
|---|---|
| `faults[f]` | `hop_symptom_equipment` — `symptom_id` = resolve(`f.symptom`), `equipment_id` = resolve(`equipment[f.equipment_id].name`, type), `position` = `f.position` |
| `faults[f].problem_id` | `hop_equipment_problem` — `problem_id` = resolve(`problems[p].statement`) if set, else `undetermined`; `suspected_cause_text` kept as text |
| `action_logs[a]` with `fault_id = f` | `hop_problem_action` (seq = `a.seq`, target, parts, role, org, dates) **and** `hop_action_result` (result = `a.outcome.value`, verification, mail, date, `finding_text` = `a.finding`) |
| `also_fault_ids` | the same two action hops repeated under each extra fault — same action, two chains |
| every `provenance` / `outcome.evidence_quote` | one `evidence` row (dedup on `sha256(mail_id\|quote)`) |
| every resolved wording | one `mention` row with the method that resolved it |

Invariants the loader asserts: ids resolve; `seq` contiguous per fault after consolidation; a `conclusive`
result has `finding_text`; a non-`unknown` result has `outcome_in_mail`; a diagnostic action never carries
a corrective result value (the CHECK from kg_dedup_layer). Rebuild is `--fresh`; the output is diffed
against the previous build like `diff_lookup.py`.

## 5. Resolving problem and action to canonical nodes (the only hard part)

Same cascade as docs/13 — alias → rule key → scoped similarity → adjudicator → queue, **never force**:

- **Rule key first.** Action = `verb_lemma:object_part` (`renew:bellows`, `inspect:bearing`,
  `reduce:speed`); problem = `failure_mode:part` (`spalling:bearing_bush`, `crack:bellows`). Needs
  `config/action_verbs.csv` (verb → lemma → default kind) and `config/failure_modes.csv`; the object/part
  comes from the existing `component_terms.csv`. Position, target, value ("136 rpm"), place stay on the hop.
- **Scoped similarity second** (sentence-transformers, pinned): candidates must share `equipment_type`
  (and `kind` for actions) before wording is compared; auto ≥ 0.90, queue 0.72–0.90, else mint `auto`.
- **Families on top**, bounded lists like `symptom_families.csv`: `action_families` (replace_part,
  adjust_setting, clean, inspect, reset, supply_part, escalate_to_maker, operational_restriction),
  `problem_families` (wear, contamination, quality_defect, operating_condition, electrical, undetermined).
  Rule-matched; a cached LLM picks from the list only for leftovers. Families are the back-off tier (§7).

Equipment and symptom already resolve through `lookup.sqlite`; the two new kinds go into the same
`standard_term` / `alias` / `pending_term` / `resolution_log` tables with `kind ∈ {problem, action}`.

## 6. Threads across casefiles

Casefile boundaries cut problems (OASIS = 4 casefiles). A `thread(thread_id, imo, equipment_id, symptom_id)`
table groups chains; `hop_symptom_equipment.thread_id` is set when two chains share vessel + equipment
+ symptom standard, or `recurrence_of` / subject stem / GC ref says so (scoped before scored, ≥ 0.72,
every decision logged). Ordering a thread's chains by `first_observed_at` makes 2→4→5→3→6 one path.
De-duplication across casefiles (same kind / same instance / same event, repeat classification) is docs/19.

## 7. Retrieval — the walk is SQL

The spine makes the main question one query:

```sql
-- how was this symptom on this equipment handled before, and with what result
SELECT c.imo, h1.casefile_id, h1.fault_id, h1.position, p.display AS problem,
       h3.seq, a.display AS action, h3.target, r.value AS result, h4.verification, e.quote, e.mail_id
FROM hop_symptom_equipment h1
JOIN hop_equipment_problem h2 USING (casefile_id, fault_id)
JOIN hop_problem_action    h3 USING (casefile_id, fault_id)
JOIN hop_action_result     h4 USING (casefile_id, fault_id, seq)
JOIN problem p ON p.problem_id = h2.problem_id
JOIN action  a ON a.action_id  = h3.action_id
JOIN result  r ON r.result_id  = h4.result_id
JOIN casefile c USING (casefile_id)
JOIN evidence e ON e.evidence_id = h4.evidence_id
WHERE h1.symptom_id = :symptom AND h1.equipment_id = :equipment
ORDER BY c.imo, h1.casefile_id, h1.fault_id, h3.seq;
```

- The question's wording is resolved with the same `lookup.resolve` before it hits `WHERE`.
- **Fleet verdict** = the same joins with `GROUP BY a.action_id, r.result_id`, counting
  `COUNT(DISTINCT casefile_id)` and `COUNT(DISTINCT imo)` (cases are evidence; occurrences are effort);
  minimum support before anything is shown.
- **What followed** = self-join of the two action hops on `seq`/`seq+1` → `followed_by(action, next_action,
  n, n_cases, n_led_to_resolved)`, a view.
- **Back-off** when the exact `(symptom, equipment)` has no rows: drop `position` → `equipment_type` →
  symptom `family_id` → problem `family_id` → same model / sister vessel. Each tier is a `WHERE` on a
  coarser column; the answer states which tier matched.
- **Free-text entry**: embed `mention.raw_text`, map hits to `standard_id`, then run the query above. The
  vector index finds the entry node; the SQL walk is the answer.
- **Answer safety** (02 §4.5): `result = unknown` is shown as *attempted*, never as a fix.

The NetworkX view is built from the same hops (`Symptom → Equipment → Problem → Action → Result`,
`Result → Action` for `seq+1`) for rendering and bounded path queries; cycles can only appear across
casefiles and are read with `ORDER BY seq`, never walked.

## 8. What to measure before trusting it

| Metric | Why |
|---|---|
| fragmentation: standards per 100 mentions, per kind | too high → under-merging; too low → over-merging |
| merge precision/recall on `resolution_cases.csv` (extend to problem, action) | the only check on §5 |
| chain completeness: % `hop_action_result` ≠ `unknown`; result distribution per kind | `unknown` share is the corpus health number |
| OASIS regression: 4 casefiles → 1 thread, path 2→4→5→3→6 | the test §6 exists for |
| precedent recall: for each held-out casefile, does §7 return a same-symptom precedent, and at which tier | the retrieval claim |

## 9. Order

1. `schema/kg.sqlite.sql` (5 node + 4 hop + support tables) and the §4 loader on the 10 records, with
   `undetermined` / `unknown` fallbacks — a traceable graph in one script.
2. §5 rule keys for action and problem; scoped similarity on the `auto` leftovers; families.
3. §6 threads with the OASIS test.
4. §7 queries as named functions (`precedents_for`, `verdict`, `what_followed`) and the NetworkX view.
5. §8 report, then thresholds.
