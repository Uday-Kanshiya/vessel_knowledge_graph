# De-duplication across casefiles: same kind, same instance, same event

> Status: **proposal, for discussion** · Date: 2026-11-06
> Companion to docs/18 (the five-node spine). Builds on the September finding that node merging and
> thread merging are different decisions with different risk (kg_dedup_layer) and on 02 §3.6 (`target`,
> `occurrence_no`).
> Question answered: the same problem shows up in many casefiles; sometimes with a new solution, sometimes
> the same solution again, sometimes a solution that did not work. How does the graph tell these apart
> without either collapsing them into one or counting them several times?

---

## 1. "Same" means three different things — dedup each at its own level

| Level | Question | Where it is decided | What a wrong merge costs | Safe direction |
|---|---|---|---|---|
| **Kind** | is this the same *sort* of problem / action? | resolver → canonical `problem_id`, `action_id` (docs/13, 18 §5) | blurs a vocabulary entry | under-merge |
| **Instance** | is this the same *ongoing fault on this vessel*? | `thread` (18 §6) | invents a precedent nobody lived | under-merge |
| **Event** | is this the same *real action reported twice*? | `duplicate_of` on the hop row | double-counts one attempt as two | mark, never delete |

Hop rows are **never deleted or merged**. Every fault and every action from every casefile stays as its
own row with its own `casefile_id` and evidence. De-duplication only decides what a row *points at*
(`standard_id`, `thread_id`, `duplicate_of`); `standard_id` is set through the curated alias table at ingest
and changed only by curation, `thread_id` / `duplicate_of` are re-derived incrementally (docs/20). That is
what lets the same problem appear 30 times and still be counted once per case.

## 2. Level 1 — same kind: canonical ids

Already the resolver cascade: alias → rule key → scoped similarity → adjudicator → `pending_term`, never
forced to the nearest neighbour. For problems the rule key is `failure_mode:part`; for actions
`verb:object`. Equipment-type words and positions are stripped *before* keying because they belong on the
hop (`equipment_id`, `position`, `target`) — otherwise `renew exhaust valve cyl 3` and `… cyl 5` mint two
actions and "repeated" becomes invisible.

Scoping is the guard against over-merge: two wordings are compared only if they already share
`equipment_type` (and `kind` for actions). "Knocking" on a main engine and on a purifier are structurally
separate because `Equipment` sits on the spine, not because the resolver remembered.

## 3. Level 2 — same instance: threads

`thread(thread_id, imo, equipment_id, symptom_id, opened_at, last_signal_at)`; every
`hop_symptom_equipment` row carries `thread_id`. Candidates must share **vessel and equipment** first;
then, strongest signal first: `recurrence_of` set by the extractor → shared GC ref / subject stem →
identical `(symptom_id)` → scoped similarity ≥ 0.72. Each decision (merge *and* reject) is a row in
`thread_merge_log(method, score, rationale)`.

Two time rules so a thread does not swallow a vessel's whole history on one machine:
- a chain whose last result is `resolved` and that is followed by the same symptom **after 6+ months** of
  silence starts a **new chain in the same thread**, flagged `recurrence`; within 6 months it is the same
  chain continuing (the fix did not hold — §5).
- a different vessel is never the same thread. Same problem on another ship is Level 1 (same
  `problem_id`), which is where fleet-wide "repeated solution" lives (§6).

## 4. Level 3 — same event: one action, reported twice

Occurs when a casefile boundary cuts a thread (OASIS, 4 casefiles) or a service report restates what a
mail already said. Detection is deterministic, inside a thread only:

```
same thread_id AND same action_id AND same target
AND (same asserted_in_mail OR |date_performed₁ − date_performed₂| ≤ 2 days OR same attachment_sha in evidence)
```

The later row gets `duplicate_of = earlier (casefile_id, fault_id, seq)`. Its result, if any, is **merged
upward** (a later casefile may state the outcome the earlier one lacked — late binding, 02 §2.2); its
evidence is kept. Counts and verdicts use `WHERE duplicate_of IS NULL`. Within one casefile the extractor's
consolidate step already does this; Level 3 is the cross-casefile half.

## 5. Classifying a repeat — the user's three cases, computed not extracted

Order a thread's non-duplicate action hops by `date_performed` (else `date_reported`), recompute
`thread_seq` and `thread_occurrence_no` per `(action_id)`, and derive `attempt_class`:

| `attempt_class` | Rule (within the thread) | Reads as |
|---|---|---|
| `new` | `action_id` not seen earlier in this thread | a new solution tried |
| `novel_for_problem` | `new` **and** `(problem_id, action_id)` not seen anywhere in the fleet before this date | first time anyone tried this for this problem |
| `retry` | same `action_id`, same `target`, previous result `unknown` \| `no_effect` \| `partial` | repeated because it had not worked (churn if `no_effect` twice) |
| `did_not_hold` | same `action_id`, same `target`, previous result `resolved` | the fix failed later — evidence **against** the action for this problem |
| `spread` | same `action_id`, **different** `target` | the fault moving (OASIS cyl 2→4→5→3→6) — evidence about the **problem**, not against the action |
| `follow_up` | a diagnostic action on a target with an earlier corrective action (KING ONE bellows renewed, then investigated) | under review, not failed |

"Not working" therefore has two sources and both are kept: the **stated** result on the hop
(`no_effect`, `made_worse`, as the mail says), and the **derived** `did_not_hold` when the same symptom
comes back on the same target after a `resolved`. The stored `result_id` is never rewritten (ingestion
loads what the mail states); `effective_result` is a view that downgrades `resolved → did_not_hold`
when a later row proves it. Answers show the stated result and the downgrade side by side.

## 6. Level 1 at fleet scale — "repeated solution" across vessels

Once rows point at canonical ids, repetition across casefiles is a `GROUP BY`, with the `Equipment` on
the spine keeping machines apart:

```sql
CREATE VIEW action_efficacy AS
SELECT h2.problem_id, h3.action_id, e.equipment_type,
       COUNT(*)                                               AS n_attempts,     -- effort
       COUNT(DISTINCT h3.casefile_id)                         AS n_cases,        -- evidence
       COUNT(DISTINCT c.imo)                                  AS n_vessels,
       SUM(er.effective_result = 'resolved')                  AS n_resolved_held,
       SUM(er.effective_result = 'did_not_hold')              AS n_did_not_hold,
       SUM(r.value IN ('no_effect','made_worse'))             AS n_failed_stated,
       SUM(r.value = 'unknown')                               AS n_unknown,
       SUM(ac.attempt_class = 'spread')                       AS n_spread
FROM hop_problem_action h3
JOIN hop_equipment_problem h2 USING (casefile_id, fault_id)
JOIN hop_action_result     h4 USING (casefile_id, fault_id, seq)
JOIN result r              ON r.result_id = h4.result_id
JOIN effective_result er   USING (casefile_id, fault_id, seq)
JOIN attempt_class   ac    USING (casefile_id, fault_id, seq)
JOIN casefile c            USING (casefile_id)
JOIN equipment e           ON e.equipment_id = h2.equipment_id
WHERE h3.duplicate_of IS NULL AND h3.action_id <> 'none'
GROUP BY h2.problem_id, h3.action_id, e.equipment_type;
```

Minimum support (`n_cases ≥ 2 AND n_vessels ≥ 2`) before a row is shown as a fleet verdict; below it the
answer lists the individual precedents instead. `n_attempts` vs `n_cases` is the effort/evidence split
from 02 §3.6; `n_spread` is excluded from the failure rate because spreading is not the action failing.

## 7. What the loader adds (over docs/18 §3)

- `hop_symptom_equipment.thread_id`, `.recurrence` · `hop_problem_action.thread_seq`,
  `.thread_occurrence_no`, `.duplicate_of` · tables `thread`, `thread_merge_log` · views `effective_result`,
  `attempt_class`, `action_efficacy`, `followed_by`.
- Order of passes: resolve kinds → assemble threads → mark duplicates → number thread_seq → derive views.
  Each pass reads only the previous one's pointers, so a resolver change re-runs everything downstream by
  rebuild, nothing is patched in place.

## 8. Regression cases to pin before trusting it

| Case | Expected |
|---|---|
| OASIS `renew:exhaust_valve` cyl 5 (`partial`) then cyl 3 (`unknown`), two casefiles | one thread; second row `spread`, not `retry`; `n_did_not_hold = 0` |
| KING ONE bellows renewed (`unknown`) then "investigate bellows cracking" (diagnostic) | same chain; second row `follow_up`; bellows renewal still `unknown`, never `failed` |
| same service report attached in two casefiles stating one repair | second row `duplicate_of` the first; `n_attempts = 1` |
| `replace:exhaust_valve_position_sensor` vs `…_stroke_sensor` | two action ids (one token apart, different parts) — resolver must **not** merge; gold-set entry |
| same symptom, same vessel, same machine, 8 months after `resolved` | new chain in the same thread, `recurrence = 1`, earlier result downgraded to `did_not_hold` |
