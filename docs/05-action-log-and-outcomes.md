# Action log — linking actions to faults and recording their outcomes

> Status: **proposal, for discussion** (revised 2026-09-16: derived outcomes dropped, see §5)
> Date: 2026-09-16
> Answers the open item in `vessel_issues_schema.json` (`"related_problems and equipment and symptoms" → structure
> to be decided`) and 02 §8.2 (is `Problem` a required hop?). Builds on 02 §2.2, §3.4, §3.6.

---

## 1. Link actions to a *fault*, not to three lists

An action is taken against one thing: a symptom on a piece of equipment, with a problem if one has been
named. That triple is already the canonical key of the graph (02 §3.4, `(Symptom + Equipment) → Problem`).
Mint it once per blob as a **fault** row and give each action one foreign key.

```
problem     { problem_id, casefile_id, blob_id, statement, asserted_by, concluded_at, evidence }
                                                                -- only when a document CONCLUDES a cause

fault       { fault_id, casefile_id, blob_id,
              symptom, equipment_id, problem_id | null,        -- problem is optional; symptom+equipment is not
              suspected_cause | null,                          -- a suspicion, never promoted to problem_id
              alarms[], first_observed_at, evidence }

action_log  { action_id, casefile_id, blob_id, fault_id,
              seq, occurrence_no, target,                        -- order, retry, "cyl 5" vs "cyl 3"
              action_text, kind,                                 -- kind ∈ corrective|diagnostic|preventive|administrative|commercial
              action_canonical | null,                           -- filled by the resolver at load, never by the extractor
              performed_by_role, advised_by_org, parts[],
              asserted_in_mail, at_reported,                     -- where the action was announced
              outcome,                                           -- see §2
              finding_id | null }                                -- diagnostics only
```

Why one `fault_id` instead of `symptom_ids[] + equipment_ids[] + problem_ids[]` on the action:

- three arrays lose the pairing — you cannot tell *which* symptom was on *which* equipment when an action
  cites two of each, and the pairing is exactly what cross-vessel matching keys on;
- an action linked to three symptoms is counted three times in "worked in 7 of 9";
- `problem_id` on the fault is the explicit link to a `problems[]` row; several faults may share one. Nullable, which settles 02 §8.2: actions always hang off a symptom+equipment, and
  acquire a problem when one is concluded — no giant `undetermined` hub.

An action that addresses two faults (rare — a decarb that clears two cylinders' symptoms) gets a second
`action_log` row with the same `action_text` and a different `fault_id`, sharing `asserted_in_mail`.

Ids inside a blob are short (`f1`, `a3`, `s2`) so the windowed extractor carries them cheaply in state;
the loader maps them to global ids. The validator rejects any `fault_id` that does not resolve.

---

## 2. Outcome — what the mails state, on the action itself

```
outcome { value,                -- corrective/preventive/commercial/administrative:
                                --   resolved | partial | no_effect | made_worse | unknown
                                -- diagnostic: conclusive | inconclusive | no_information | unknown
          verification,         -- confirmed_by_vessel | confirmed_by_oem | reported_only | none
          outcome_in_mail,      -- the mail that states it; usually later than asserted_in_mail
          evidence_quote }
```

Rules the extractor is told and the code checks (02 §2.2):

- an outcome that arrives in a later window **updates** the existing action, never creates a new one
  (late binding; the carried state is what makes this possible);
- `unknown` is a value, and the expected majority — the prompt says so explicitly, or the model invents
  tidy resolutions;
- no quote, no outcome: `value != unknown` requires `outcome_in_mail` and an `evidence_quote` that the
  validator finds verbatim in that mail;
- the quote may be indirect ("alarm again on unit 4 after sensor change") — the model judges it, and
  `verification` records how strong that judgement is. A vague mail stays `unknown`.

`asserted_in_mail` and `outcome_in_mail` are the two ends of the evidence chain and both stay; the lag
between them is a view, not a field (working agreement).

---

## 3. Storage and indexes (Mongo, per `vessel_issues_schema.json`)

Flat collections, not arrays embedded in the incident report — the fleet query is "for this
(equipment_type, symptom) what was tried and how did it go", which is one index scan on a flat log and a
`$unwind` over every casefile on an embedded one. The incident report keeps `fault_ids[]` and
`action_ids[]` for the per-case view.

```
vessel_issues_faults        (casefile_id, blob_id, fault_id)          UNIQUE
                            (equipment_id, symptom_id)                -- cross-case entry point
vessel_issues_action_log    (casefile_id, blob_id, seq)               UNIQUE
                            (fault_id, seq)                           -- per-fault chain, read by sorting (02 §3.6)
                            (action_canonical, outcome.value)         -- efficacy counts
```

All carry `extraction_version`; `action_canonical` is written by `src/resolution/`, and a test greps the
extractor output schema to assert it never emits it.

---

## 4. What this asks of the extractor

Per window, three things and no more: new faults (symptom + equipment [+ problem]); new actions with
`fault_id`, `kind`, `target`, `asserted_in_mail`; and outcome updates to existing actions where a mail
states one. No counts, no durations, no canonical names. Consolidate (02 §2.4) merges duplicate actions
and assigns `seq`; the loader mints global ids.

Phase-0 check on OASIS: `problem_id = null` on the fault, the SGD 8,500 gate, the cyl-5 renewal `partial`,
and the sensor swap / CCU reset / FIVA replacement carrying `no_effect` or `unknown` — never `resolved`.

---

## 5. Reviewed and not adopted (2026-09-16)

**Derived outcomes** — a per-mail `symptom_status` (present / reduced / absent) timeline joined to the
action date, plus recurrence retro-tagging and a `superseded_by` flag — were proposed as a way to fill the
`unknown` majority. Not adopted: the mails are too vague for a mechanical join to be trusted, and an
inferred `no_effect` that is wrong is worse than an honest `unknown`. Outcomes come from the action log
only, as stated by a mail with a quote. If the `unknown` share turns out to make the counts unusable at
fleet scale, revisit with a measured error rate on the gold set rather than by assumption.
