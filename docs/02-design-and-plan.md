# Vessel Knowledge Graph — Design & Plan

> Status: **current design of record**
> Date: 2026-09-01
> Companion to `01-problem-statement.md` (why we are building this).
> Replaces the earlier docs 02–05, now in `_superseded/` for history only.

---

## 0. How to read this

This is the single reference for **what we are building and how**. It has three parts that map
onto the agreed split of the work:

| Part | What | Status |
|---|---|---|
| 1 | Identify defect casefiles | already solved — existing classifier, treated here as a black box emitting `casefile_id`s |
| 2 | Casefile → structured facts | designed here, §2 |
| 3 | Structured facts → graph + search | designed here, §3–§4 |

The boundary between parts 2 and 3 is a **frozen JSON contract** (§2.5). Neither side reaches
across it.

---

## 1. Scope

| | |
|---|---|
| **Corpus** | Defect casefiles only, selected by the existing classifier. Purchase / admin / PSC out of scope for v1. |
| **Users** | Shore-side — technical and electrical superintendents, office teams. Not crew-facing in v1. |
| **Fleet** | ~700 vessels, ~4 years of correspondence |
| **Inputs** | casefiles (mails + attachments), machinery collection, PMS (partial coverage) |
| **Not available** | any resolution / closure signal. Threads simply stop. This shapes the whole design. |

---

## 2. PART 2 — Extraction

### 2.1 The constraint, and why it forces a reduce

A casefile can hold 50+ mails. We process **5 at a time**. The problem this creates: an action's
outcome frequently appears many mails after the action.

> OASIS: the FIVA proportional valve is replaced around mail 30; "no progress" arrives later, in a
> different thread. Independent per-batch extraction produces two disconnected fragments.

So each batch does not produce an independent result — it **updates a carried case state**.

```
state := empty
for each window of 5 mails, in date order:
    context := the 5 mail bodies
             + attachment digests for those mails, inlined
             + the current state
    state := LLM_update(state, context)
```

### 2.2 Rules the prompt states and the code verifies

1. **Update, never regenerate.** Objects created in earlier windows keep their ids. Code asserts no
   previously-seen `step_id` / `gate_id` has disappeared; if one has, restore it and raise
   `validation_flag: dropped_object`. *LLMs silently dropping items from a rewritten JSON document is
   the #1 failure mode of this pattern — do not trust it, check it.*
2. **Late outcome binding.** If a window reports the result of a step opened earlier, it **updates**
   that step. It does not create a new one.
3. **Every assertion carries evidence** — `[mail <id>]` or `[attachment <sha8>]` plus a verbatim
   quote. No evidence, no field.
4. **Unknown is a value, not a gap.** `outcome: unknown` and `problem.status: undetermined` are
   expected and correct. The prompt must say so explicitly, or the model will invent tidy
   resolutions — which is precisely the bias we are building this system to escape.
5. **Bounded state.** Cap ~25 steps / 15 gates per blob; exceeding it sets `needs_review` rather than
   truncating silently.
6. **Stride 5, window 6** (one mail of overlap) to preserve immediate reply context cheaply.

Model choice: a mid-tier model is adequate because the carried state holds the reasoning burden.
Sample 5% into a stronger model and compare — that measurement picks the production model, not
preference.

### 2.3 Attachments — three tiers

Attachments are where equipment identity and OEM procedure live, and also where most of the waste is.

**Tier 0 — global sha256 cache.** OEM documents repeat across the whole fleet; the same photos repeat
within one casefile. Hash everything, reuse anything already digested.

**Tier 1 — deterministic junk filter, no model call.** Drop inline signature images
(`image0NN.png`, `CID_image*`, `Outlook-*`, under ~15 KB or ~200 px), `.ics`, `.vcf`, `winmail.dat`,
same-sha duplicates within a mail. On the OASIS casefile this removes roughly two thirds of rows.
**Measure the real ratio early — it is the single biggest cost lever in Part 2.**

**Tier 2 — typed extraction on survivors.**

| Input | Handling |
|---|---|
| PDF / DOC / DOCX | text extract; if thin (<200 chars/page) treat as scanned → OCR; then one structured call |
| XLS / XLSX / CSV | convert to markdown table, then structured call |
| Images (real) | one vision call |
| `.eml` | parse as a nested mail, fold into the manifest |
| `.zip`, telemetry dumps | **do not parse.** Record that a dump was taken on a date — that fact is the signal |

Digest carries: `sha256, file, mail_id, kind, on_topic, title, summary, equipment, maker, model,
part_ids[], readings, defect, findings[], confidence`.

> Evidence this pays for itself: in the JEFFREYS BAY case, `maker: HYUNDAI MIPO` and part ids
> `CO2-01…CO2-08` existed **only** in a shipyard drawing PDF. No mail body contained them.

### 2.4 Consolidate, validate, gate

**Consolidate (one call over the state only — cheap, constant size):** merge duplicate steps across
windows; order by timestamp and assign `seq`; separate the administrative/approval chain out of
corrective work into `gates[]`; derive blob-level status; split into multiple blobs if the state holds
distinct problems; set `problem.status`.

> This is also where **false loops** are removed. A step described in window 2 and referred back to in
> window 4 can be re-opened as a fresh occurrence. Collapse two occurrences of the same action with no
> intervening action, or with overlapping evidence mail ids. Flag any case producing >3 occurrences of
> one action for review — real ones exist, but it is also the signature of an extraction bug.

**Validate (deterministic, no model):**
- schema valid
- every `outcome != unknown` has evidence with a resolvable `mail_id`
- **every quoted evidence string actually occurs in the mail it cites** (substring match after
  normalisation) — this catches fabricated quotes, the highest-risk hallucination here. Target rate: 0
- blob status consistent with the step outcomes
- at least one equipment with `role: subject`

Failures set `validation_flags[]` and `needs_review: true`. **Records still load, tagged
`low_confidence`** — excluding them would bias the corpus toward easy cases.

Stamp `extraction_version` on everything so the backlog can be re-run when the prompt improves.

### 2.5 The contract — `CaseFact` v1

```json
{
  "casefile_id": "...", "blob_id": "...:0", "extraction_version": "1.0.0",
  "vessel": { "name": "OASIS", "imo": 1079199 },
  "casefile_kind": "technical_problem",
  "opened_at": "...", "last_signal_at": "...",

  "symptoms":  [{ "id":"sym1", "text":"...", "first_observed_at":"...", "evidence":"[mail 0] ..." }],
  "alarms":    [{ "id":"alm1", "raw":"exhaust open stroke low", "position":"cyl 4", "evidence":"..." }],

  "equipment": [{ "name":"Main Engine", "equipment_type":"main_engine",
                  "maker":"Everllence", "model":"ME-C",
                  "maker_source":"attachment:d22a153c",
                  "serial":"HE0003", "sw_version":"ME-ECS-SW-1312-7.1",
                  "position":"cyl 5", "role":"subject|mentioned",
                  "components":[{ "name":"exhaust valve", "condition":"failed" }],
                  "confidence":"high|medium|low" }],

  "problem": { "text":"...", "status":"confirmed|suspected|undetermined",
               "asserted_by":"vessel|superintendent|oem|vendor|class|inferred",
               "confidence":"high|medium|low", "evidence":"[mail 27] ..." },

  "steps": [
    { "id":"s1", "seq":1, "occurrence_no":1,
      "action":"Replaced exhaust valve position sensor",
      "kind":"corrective|diagnostic|preventive|administrative|commercial",
      "target":"cyl 4",
      "rationale":"stroke-low alarm suspected sensor fault",
      "outcome":"resolved|partial|no_effect|made_worse|unknown",
      "verification":"confirmed_by_vessel|confirmed_by_oem|reported_only|none",
      "note":"issue persisted after sensor change",
      "finding":null,
      "at_reported":"...", "at_performed":null,
      "parts_used":[], "performed_by":"vessel_crew", "advised_by":null,
      "evidence":"[mail 27] ...", "mail_ids":[8,27] }
  ],

  "gates": [
    { "id":"g1", "type":"warranty_dispute|quotation|po|prepayment|permit|
                          risk_assessment|agent_arrangement|spare_delivery",
      "status":"raised|approved|rejected|pending", "blocking":true,
      "party":"Everllence PrimeServ", "amount":{"value":8500,"currency":"SGD"},
      "opened_at":"...", "closed_at":null, "evidence":"[mail 39] ..." }
  ],

  "organisations": [{ "name":"Everllence PrimeServ Singapore", "role":"oem|vendor|agent|yard|class" }],
  "terminal": "resolved|abandoned|open",
  "source_mail_ids": [...], "mail_oid_map": {...},
  "attachments_read": 15, "attachments_injected": 15,
  "validation_flags": [], "needs_review": false
}
```

**Note for the loader.** Extraction emits each step's outcome **on the step**, which is the natural
shape for a per-case list. The graph stores it **on the outgoing edge** (§3.5). The loader does that
shift. This is deliberate — do not push graph storage decisions back into the extraction schema.

**Blob status precedence** (derived, first match wins): verified resolve → `fix_verified`;
unverified resolve → `fix_unverified`; any partial → `partial`; steps exist, all no-effect/unknown,
last signal < 6 months → `open`; otherwise → `abandoned`.

---

## 3. PART 3 — The graph

### 3.1 Governing principle

**One node per thing, for the whole fleet. Nothing is duplicated per case.**

There is exactly one `Symptom` node for exhaust-valve knocking, one `Action` node for
`reset_ccu`, across all 700 vessels and four years. What separates the cases is `casefileIds`,
carried on nodes and — critically — **on edges**.

The graph is a union of per-case subgraphs. Filter to one id and that case comes back exactly;
aggregate the same edges and you get the fleet's verdict.

### 3.2 Four layers

| Layer | Contents | casefileIds | Traversed at query time |
|---|---|---|---|
| 1 · Reference | Vessel, EquipmentInstance, Component, Model, Maker | no | no — filtered against, and rolled up for back-off |
| 2 · Registry | one `Casefile` node per casefile | n/a | resolved once, up front |
| 3 · Reasoning | Alarm, Symptom, Equipment, Problem, Action, Finding | **yes** | yes — this is the spine |
| 4 · Support | Part, Organisation, Role, Gate, Evidence | **yes** | yes, from Actions |

### 3.3 Nodes

**Layer 1 — Reference (static).** `Vessel` (imo) · `EquipmentInstance` (imo, type, serial; carries
`id_source` ∈ pms > machinery > attachment > mail_text, with that precedence, plus a confidence) ·
`Component` (slug, a tree via `PART_OF` — this is what makes back-off roll-up possible) ·
`Model` / `Maker`.

Never traversed from a case. Its ids are **denormalised onto the `Casefile` node** so scoping is an
indexed filter, not an array intersection per hop.

**Layer 2 — Registry.** `Casefile` (casefile_id) — vessel, model, component, opened_at,
last_signal_at, terminal status, stale flag. Not a duplicate of anything: exactly one per casefile,
and it is **what every `casefileIds` entry points at**.

**Layer 3 — Reasoning.**

| Node | Notes |
|---|---|
| `Symptom` | what the crew reports. Keep `raw_texts[]` so re-canonicalisation is possible without re-extraction |
| `Alarm` | separate from Symptom — machine-generated, matches exactly |
| `Equipment` | the **type** (`main_engine`), on the reasoning path — see §3.4 |
| `Problem` | the reason. `undetermined` is a real value and will be common |
| `Action` | `kind` ∈ corrective, diagnostic, preventive, administrative, commercial |
| `Finding` | output of a diagnostic — the branch-point vocabulary |
| `Resolved` / `Abandoned` / `Open` | terminal nodes closing every chain |

**Layer 4 — Support.** `Part` · `Organisation` (`domains[]` lets sender addresses resolve
deterministically, no LLM) · `Role` · `Gate` (carries duration) · `Evidence` (mail oid or attachment
sha + verbatim quote).

### 3.4 The triangle

```
Symptom ──OCCURS_ON {cf}──▶ Equipment ──INDICATES {cf}──▶ Problem
   └╌╌╌╌╌╌╌╌╌╌ derived shortcut ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌▶
```

`(Symptom + Equipment) → Problem` is the **canonical key**. Equipment sits on the path so that
knocking on a main engine and knocking on a purifier can never be averaged together — the separation
is structural, not dependent on someone remembering a filter.

The direct `Symptom → Problem` edge is kept as a **materialised shortcut** and must be labelled as
one. Otherwise someone queries it, silently loses the equipment discrimination, and gets mixed
results.

### 3.5 The action chain — result on the way out

**The status of an action rides on the edge leaving it.** Combined with terminal nodes, every action
has exactly one outgoing edge per case, therefore exactly one status, with no gaps:

```
(Problem)-[:TRIED {casefile, seq:1}]->(Action)            -- entry; no status yet

(a1)-[:NEXT {casefile:"c1", seq:1, occurrence_no:1, target:"cyl 4",
             status:"F", note:"alarm persisted after reset",
             evidence:"mail 27"}]->(a2)                   -- a1's result

(a6)-[:DONE {casefile:"c1", seq:4, status:"S",
             note:"knock cleared, confirmed by vessel"}]->(Resolved)
```

This reads the way the mails read — *"reset the CCU, no change, so we replaced the sensor"* — and one
edge holds both the result and the reason for the next move.

**Three terminals, not one.** There is no resolution signal in the corpus; most chains end in silence.
If everything terminated at a single `Completed`, the most common ending in the data would be labelled
success and that error would propagate into every recommendation.

- `Resolved` — last action succeeded and was verified
- `Abandoned` — quiet for 6+ months. **Likely the largest bucket**
- `Open` — still active

The share landing in `Abandoned` is a headline health metric for the corpus.

### 3.6 Cycles

**A single case's chain is always linear. Cycles appear only when cases are merged.** So all cycle
problems live in the aggregate layer, and the fix is to do the real work on the per-case layer.

1. **Storage** — allow it. Two outgoing edges from `a1` with the same casefile at different `seq`.
2. **Key on the occurrence.** `(casefile, seq)` is unique. `occurrence_no` makes *"does retrying ever
   work?"* askable. `target` distinguishes:
   - same action, **same target** repeated → the fix did not hold (evidence against the action)
   - same action, **different target** repeated → the fault is spreading (evidence about the problem)

   OASIS renewing valves on cylinders 2, 4, 5 is the second kind. Without `target` it reads as three
   failed retries and corrupts that action's fleet-wide efficacy.
3. **Read back by sorting, never walking** — `MATCH ()-[r {casefile:"c6"}]->() RETURN r ORDER BY r.seq`.
   No traversal, so a cycle cannot trap it. If anyone does walk `NEXT`, bound it (`[:NEXT*1..8]`).
4. **Count two numbers** — occurrences measure effort, distinct cases measure evidence. Minimum
   support uses distinct cases and vessels.
5. **Analyse on sequences, not on the graph.** "Fastest route to a fix" is frequent-sequence mining
   over the per-case lists, not shortest path over the cyclic aggregate graph.
6. **Render cycles explicitly** as a self-arc labelled with how many cases went round.

Loops are an output, not just a nuisance: `F, F` then escalation is **churn** (quantified wasted
time); a loop ending `S` is a legitimate **iterative procedure**. The outcome pattern separates them.

### 3.7 Full edge list

```
-- reference (static)
(Vessel)-[:HAS]->(EquipmentInstance)-[:HAS_PART]->(Component)-[:PART_OF]->(Component)
(EquipmentInstance)-[:OF_MODEL]->(Model)-[:MADE_BY]->(Maker)

-- registry
(Casefile)-[:ON_VESSEL]->(Vessel)          (Casefile)-[:RECURRENCE_OF]->(Casefile)

-- reasoning
(Alarm)-[:ACCOMPANIES {cf}]->(Symptom)
(Symptom)-[:OCCURS_ON {cf}]->(Equipment)
(Equipment)-[:INDICATES {cf}]->(Problem)
(Symptom)-[:INDICATES_DIRECT {cf}]->(Problem)          -- derived shortcut

(Problem)-[:TRIED {casefile, seq}]->(Action)
(Action)-[:NEXT {casefile, seq, occurrence_no, target, status, note, evidence}]->(Action)
(Action)-[:DONE {casefile, seq, status, note, evidence}]->(Resolved|Abandoned|Open)

(Action)-[:YIELDED {cf}]->(Finding)-[:POINTS_TO {cf}]->(Problem)

-- aggregate, recomputed on a schedule
(Action)-[:FOLLOWED_BY {n, n_cases, n_vessels, n_led_to_fix}]->(Action)

-- support
(Action)-[:USED_PART {cf}]->(Part)
(Action)-[:PERFORMED_BY|:ADVISED_BY|:AUTHORISED_BY {cf}]->(Organisation)
(Action)-[:BY_ROLE {cf}]->(Role)
(Action)-[:OPENS|:CLOSES {cf}]->(Gate)
(*)-[:EVIDENCED_BY {cf}]->(Evidence)
```

### 3.8 The three rules that make it work

1. **Outcome is the edge, never a node property.** That is what lets one `replace_position_sensor`
   node be a failure in c1 and a success in c4. Put it on the node and you must keep action nodes
   case-local — and then nothing counts across vessels, which is the entire product.
2. **One edge per (casefile, transition).** All properties stay scalar; no parallel arrays.
   ~5 transitions per case → about 25,000 edges for a 5,000-case corpus.
3. **Multi-hop queries must resolve the casefile id set first** (from the registry), or intersect
   `casefileIds` along the path. Without this a union graph splices unrelated cases into a
   recommendation nobody ever made.

### 3.9 Entity resolution & ontology

Seed ontology v0 from data we already own — PMS and the machinery collection give a real controlled
vocabulary of equipment types, makers and models. Grow it from extraction after that.

Cascade per mention, cheapest first: **alias table** (plain, human-editable, deterministic — resolves
the majority) → normalised exact match → embedding nearest neighbour at cosine ≥ 0.85 → LLM
adjudicator given the top-10 candidates → else `new_term_queue`. **Never force to nearest.**

`StepType` and `Problem` should be **trees, not flat lists**, so near-duplicates roll up
(`overhaul_actuator → actuator_work → hydraulic_work`) and you can aggregate at whichever level has
support. **Fragmentation rate is a first-class health metric** — at 90+ vessels, canonicalisation
quality is what the whole system's value rests on.

---

## 4. Search & answer

### 4.1 Query path

1. **Resolve scope once** — vessel → instance → model; query the `Casefile` registry. One indexed
   filter, one id set.
2. **Enter the spine** — match query text to `Symptom`, alarm codes to `Alarm` exactly.
3. **Fan out to reasons** — `Symptom + Equipment → Problem`, weighted by count. This is the
   differential: what it usually turns out to be, with numbers.
4. **Find the discriminator** — which `Action{kind:diagnostic}` yielded `Findings` pointing at those
   Problems? That is what to run first; its answer picks the branch.
5. **Read outcomes per branch** — count outgoing edge statuses per Action within the scope.
6. **Read the route** — `FOLLOWED_BY` within scope, or frequent-sequence mining on the per-case lists.
7. **Read the commercial path** — aggregate `Gate`: what blocks, and for how long.
8. **Cite** — pull `Evidence`. Only 3–5 exemplar cases get their full text read.

**Output size is bounded by vocabulary, not corpus.** Step 5 returns ~12 rows whether the candidate
set is 3 cases or 300. That is the property that makes this scale where plain retrieval does not.

### 4.2 Back-off tiers (this replaces GCN cold-start)

| Tier | Scope | Use |
|---|---|---|
| 1 | same Model + same Symptom | strongest precedent |
| 2 | same Maker + same equipment type | if tier 1 thin |
| 3 | same equipment type, any maker | last resort — **labelled as such in the answer** |

Vector similarity ranks *within* a tier; it never selects across equipment boundaries.

### 4.3 Statistical discipline

- **Counts, never percentages.** "Worked in 7 of 9", not "78%".
- **Minimum support**: n ≥ 3 cases **and** ≥ 2 distinct vessels before anything is presented as fleet
  evidence. Below that, label it anecdote.
- **Smoothed estimates** (Wilson lower bound) so 1/1 does not outrank 8/10.
- **Correlated-evidence discount** — group by `ADVISED_BY`. One OEM circular followed by 60 vessels
  is *one* advisory vote, not sixty. Without this the system systematically overstates confidence
  wherever OEM guidance was widely followed.
- **Homogeneity check** — if efficacy varies significantly across model / sw version / age, split the
  row and show both rather than averaging. Averaging across a heterogeneous set produces confidently
  wrong advice.
- **Asymmetry** — `no_effect` is *strong* negative evidence; `resolved` + `reported_only` is only
  medium positive. A reported failure is directly observed; a reported success may be premature, and
  recurrence detection often proves it was.
- **Recency** — 4 years of data; superseded software and pre-retrofit equipment can mislead. At
  minimum a recency facet, probably a time decay.

### 4.4 Status, staleness, recurrence

**Staleness.** Non-terminal case with `last_signal_at` older than 6 months → tag `stale`. Append
`_stale` to open edges; **never** reclassify to resolved — silence is abandonment, not success.
Annotate in the graph layer only, never write back to the source system. **Do not exclude from
retrieval** — a stale unresolved case is real precedent; flag it in the answer. Reversible on new
signal. Track the `_stale` fraction as a health metric.

**Recurrence.** A new case matching an existing `(EquipmentInstance, Symptom)` with an earlier open or
resolved case → write `RECURRENCE_OF` and retro-tag. **This is the only mechanism that recovers real
outcome ground truth from a corpus with no resolution signal** — it turns "we thought that fixed it"
into "it came back in six weeks". Build it in from day one.

### 4.5 Answer safety

The answer must distinguish *"3 of 3 confirmed-fixed precedents"* from *"1 unverified attempt in a
stale case"*, and must **refuse or hedge explicitly** when the candidate set is below support or all
evidence is weak. This is a requirement, not prompt garnish — the recommendations act on live
machinery.

---

## 5. Evaluation — start this first

Without this, no design argument in this repo is falsifiable.

**Gold set: 50–100 defect cases**, spread across equipment types, sampled from *closed* and
*recurring* cases where the truth is knowable in hindsight. For each, a superintendent writes —
**before seeing any system output** — likely causes, correct first checks, known dead ends, parts
needed, expected commercial path.

| Layer | Metric |
|---|---|
| Attachments | % junk filtered; maker/model recovered only from attachments |
| Extraction | step recall & outcome accuracy vs hand annotation; **fabricated-quote rate (target 0)** |
| Resolution | precision/recall of entity merges; new-term queue volume; fragmentation rate |
| Retrieval | precedent precision@5, judged by a superintendent |
| **Answer** | **% of gold dead-ends correctly warned about** ← the headline metric |
| Safety | % of thin-evidence questions correctly refused or hedged |

The dead-end warning rate is the number that says whether this project achieved anything a search box
could not.

---

## 6. Phasing

| Phase | Work | Exit gate |
|---|---|---|
| **0 · Pilot** | Run **OASIS** + 20 hard cases through §2. Start the gold set. Get the defect-only casefile count and build the cost model. | OASIS output shows 3 `no_effect` steps, `problem: undetermined`, and the SGD 8,500 gate. **If it says "renewed valve, resolved", stop and fix the prompt before scaling.** |
| **1 · Contract freeze** | `CaseFact` v1 as a versioned JSON Schema + validator in this repo. Ontology v0 exported from PMS/machinery. Alias table started. | Both teams building against one schema. |
| **2 · Extraction at scale** | §2 over the defect backlog; attachment cache warm. | ≥95% schema-valid, 0 fabricated quotes on audit, cost within model. |
| **3 · Graph** | §3 load, entity resolution, ontology growth loop. | Recurrence detection reproduces known repeat-failure chains. |
| **4 · Search** | §4 with back-off tiers, aggregation, evidence weighting. | Beats a plain vector-search baseline on the gold set — the baseline must actually be built and measured. |
| **5 · Hardening** | Refusal behaviour, monitoring (`_stale` fraction, fragmentation rate, new-term rate, review queue depth). | Superintendent pilot with live questions. |
| **6 · Optimise** | Distil extraction to a cheaper model using accumulated labels. | — |

---

## 7. Decisions log

**Adopted**

| Decision | Why |
|---|---|
| Deduplicated nodes, `casefileIds` on nodes and edges | node count bounded by vocabulary, not corpus; counts are free |
| Outcome as the **edge**, on the way **out** | one node holds different results per case; reads the way the mails read; the causal trigger is implicit |
| Three terminal nodes | no resolution signal exists — labelling silence as "completed" would poison every recommendation |
| Equipment **on** the reasoning path | `(Symptom + Equipment) → Problem` prevents cross-equipment averaging structurally |
| `Casefile` registry node | gives `casefileIds` something to point at; scope resolves once, not per hop |
| `Finding` as a first-class node | turns the symptom→problem fan-out from a list into a decision |
| `Problem` (not `RootCause`) | better word for the domain; `undetermined` is a first-class value |
| Gate / commercial layer | in OASIS this was ~70% of elapsed time and why the case never closed |
| `seq` + `occurrence_no` + `target` on every step edge | cycles, retries, and fault migration all handled |

**Rejected**

| Rejected | Why |
|---|---|
| 8-algorithm keyphrase ensemble | inherited ceremony; clustering runs on embeddings and labelling is an LLM call, so it fed nothing load-bearing |
| Community detection (Louvain/Leiden) | mostly rediscovers `UICategory` once scope is defect-only; optimises "shares a vendor", not "same failure mode" |
| GCN link prediction | trained model needing retraining each full update; reported BLEU gain does not map to answer quality. Back-off tiers do the cold-start job |
| `Solution` as an extracted node | it is a judgment the aggregate makes better from data — derive it by frequent-set mining instead |
| Per-case mention nodes | rejected in favour of the deduplicated design. Mitigation: keep `raw_texts[]` on canonical nodes |
| Extraction from casefile summaries | summaries systematically delete failed attempts, which is the knowledge we are after |

---

## 8. Open

1. **Defect-only casefile count** — drives the entire cost model. (75k is *all* casefiles.)
2. **Is `Problem` a required hop?** In most cases it is never confirmed. If every Action must hang off
   a Problem, you get a giant `undetermined` hub. Safer shape: Actions attach to the Symptom always,
   to a Problem when concluded. **Decide from the pilot output, not in the abstract.**
3. **Store** — Neo4j holds this shape natively; Postgres holds it as a node table plus an edge table
   where scoping and aggregation are indexed rather than array operations. Decide on measured query
   patterns.
4. **`Finding` vocabulary** — hand-seed the common ECS/alarm findings first, or bootstrap from corpus?
5. **Attachment URLs** — are `assets-prod.siya.com` links fetchable from the processing environment,
   and is there a rate limit?
6. **PMS / machinery export** — available now, to build ontology v0 in phase 1?
7. **Gold set ownership** — who, and can superintendent time be booked in phase 0?
