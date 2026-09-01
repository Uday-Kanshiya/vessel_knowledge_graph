# Vessel Knowledge Graph — Problem Statement

> Status: **draft / under discussion**
> Owner: Data Science
> Last updated: 2026-08-30
>
> This document describes **what we are building and why**, and the constraints
> that shape it. It deliberately does **not** describe how we will build it —
> architecture, pipeline design and implementation live in later documents.

---

## 1. The goal in one sentence

When a defect or technical issue occurs on a vessel, a shore-side user should be
able to describe it in their own words and receive an answer grounded in the
accumulated experience of **~700 vessels over ~4 years**: what to check, in what
order, what has historically worked, what has historically been a dead end, what
parts and vendors were involved, and what is likely to block progress.

**Not** "here are five similar email threads." An actual answer, with citations.

---

## 2. Domain context

This is a ship management company. When something goes wrong on a vessel, the
entire lifecycle of that problem — detection, diagnosis, troubleshooting,
escalation, vendor engagement, commercial approval, repair, closure — happens
over **email**.

A single issue generates correspondence across several axes:

- **Ship → Shore** — Chief Engineer reporting symptoms, alarms, readings
- **Shore → Ship** — superintendents issuing troubleshooting guidance
- **Shore → OEM / Maker** — technical support, warranty questions, dump-file analysis
- **Shore → Vendor / Service provider** — quotations, attendance, technician deployment
- **Shore → Agent / Owner** — PO, prepayment, berthing and logistics

These are **different mail threads with different subject lines**, often running
in parallel, sometimes months apart. The knowledge of *how the problem was
actually solved* is distributed across all of them — and across their
attachments.

---

## 3. What we already have

### 3.1 Casefiles

We have an existing system that reads all mail and groups it by topic into a
**casefile**: one casefile ≈ one issue on one vessel. Each casefile carries:

| Field | What it holds |
|---|---|
| `vesselName`, `vesselId`, `imo` | vessel identity |
| `casefile` | human-readable issue title |
| `category`, `UICategory`, `tag` | classification (defect, purchase, maintenance, …) |
| `conversationTopic[]` | every distinct mail subject folded into this case |
| `summary` | LLM-written narrative: Issue / Actions Taken / Current Status |
| `currentStatus`, `status` | short state description, active/closed |
| `importance`, `importanceReason` | scoring |
| `pages[]` | **per-mail record**: sender, recipients, datetime, subject, per-mail summary, tags, link to rendered HTML mail body, links to attachments |
| `index[]` | ordered index of pages by date |
| `casefilePlans[]` | rolling snapshots of an event log + planned actions |
| `taskAssigned[]`, `followUp` | open actions and owners |

A casefile can be large — the reference example (MT OASIS, main engine exhaust
valve) has **46 pages spanning 13 Mar → 07 Apr 2026** across 9 distinct mail
subjects and 12 conversation IDs.

### 3.2 Machinery / equipment data 

A separate MongoDB collection holds per-vessel machinery PMS: equipment, component,
maker, model. **Coverage is incomplete.** For some vessels and some equipment the
maker/model only exists inside an email attachment (service report, defect
report, OEM correspondence) and will have to be recovered from there.


---

## 4. Scope

| Decision | Value |
|---|---|
| **Corpus** | **Defect casefiles only.** We have an in-house algorithm that identifies defect casefiles; it will be plugged in as the corpus filter. Purchase/administrative/other categories are out of scope for v1. |
| **Users** | **Shore-side**: technical superintendents, electrical superintendents, and other office teams. Not crew-facing in v1. |
| **Fleet / period** | ~700 vessels, ~4 years of correspondence |
| **Stack** | **Undecided.** Graph database vs. relational + graph-shaped queries to be evaluated separately. |

---

## 5. Why simple retrieval over casefiles is not enough

This is the core technical argument for the project. Straightforward vector
search over casefiles or casefile summaries fails for four independent reasons.

### 5.1 The retrieval unit is wrong

A casefile is a **conversation**, not a **finding**. The useful knowledge —
*"we replaced the sensor and it changed nothing"* — is one sentence inside one
mail on page 32. Embedding the casefile summary loses it entirely; embedding
every page returns a wall of near-duplicate, low-signal chunks.

### 5.2 Summaries systematically discard failure

Casefile summaries are written to report *status*, so they preserve what was done
and drop what didn't work. In the OASIS example the summary states the exhaust
valve was renewed and monitoring continues. It does **not** state that:

- replacing the **exhaust valve position sensor** produced no change
- resetting **CCU4** produced no change
- replacing the **proportional valve (FIVA)** showed no progress

That negative knowledge is arguably the single most valuable thing in the
casefile — it is precisely "what *not* to waste two days on" — and the summary
layer deletes it.

### 5.3 Equipment context is invisible to the text

The answer depends heavily on **maker and model**. A remedy for a MAN B&W ME-C
electronically controlled engine is not a remedy for an ME-B or a Wärtsilä unit.
But the casefile text rarely states the model — in OASIS it is only inferable
from an attachment filename (`MOPDump … ME-ECS-SW-1312-7.1 … Engine no HE0003`)
and from the fact that the OEM contact is Everllence.

Text similarity has no way to enforce this. Retrieval must be **constrained** by
equipment/maker/model, which is a join against structured data, not a distance
computation.

### 5.4 The real question is an aggregation, not a lookup

The user's implicit question is *"across all comparable cases, what usually
turns out to be wrong and what usually fixes it?"* If 30 casefiles match a topic:

- we cannot fit 30 casefiles into a model context
- reading them one by one does not produce a distribution
- the answer we want is a **count over structured facts** ("root cause was the
  hydraulic actuator in 18 of 24 comparable cases"), which requires those facts
  to have been extracted in advance

Retrieval alone cannot produce this. The corpus has to be **pre-structured**.

---

## 6. Worked example — MT OASIS, IMO 1079199

Used throughout as the reference case. Summarised as the graph would need to hold it:

**Context** — MAN B&W ME-C type electronically controlled main engine
(OEM: Everllence / MAN Energy Solutions), engine no. HE0003, ECS software
ME-ECS-SW-1312-7.1.

**Symptoms** — knocking sound from ME exhaust valve (previously units 2 and 4,
later unit 5); `exhaust close stroke low` and `exhaust open stroke low` alarms on
unit 4; ME slowdown; fuel plunger high-stroke alarm on unit 4; intermittent
failure to start on dead slow.

**Diagnostics performed** — HCU/MOP dump upload and burst analysis; cyclic test;
NR valve check on the actuator. **Key finding:** proportional signal present and
FIVA moving, but **no exhaust valve movement** — which points away from the
electronics and toward the hydraulic actuator / valve itself.

**Actions and their outcomes** — this is the shape of knowledge we need:

| Action | Outcome |
|---|---|
| Replace exhaust valve position sensor | no effect |
| Reset CCU4 | no effect |
| Replace proportional valve (FIVA) | no progress |
| Renew exhaust valve, cylinder No. 5 | knock resolved on that unit (partial) |
| Root cause identification | **never reached** — case still active |

**Commercial path** — warranty status disputed; Everllence Operation Singapore
declined to quote because it was treated as a warranty case; PrimeServ later
confirmed it was **not** under warranty and moved it to owner's account; service
tariff issued; **PO + SGD 8,500 prepayment + local agent/berth details** required
before technician deployment.

**Elapsed time** — technical direction was established within ~2 days
(22–24 Mar). The case then sat for **two weeks** waiting on PO and prepayment,
with the same three superintendent tasks rescheduled weekly from 25 Mar to
15 Apr. The bottleneck was commercial, not technical — and that is itself a
learnable, reportable pattern.

---

## 7. What the knowledge layer has to capture

The casefile is the **source**, not the unit of knowledge. The unit of knowledge
is a chain, anchored to a specific piece of equipment:

```
Symptom / Alarm  →  Diagnostic (+ finding)  →  Action  →  Outcome  →  …  →  Resolution
```

### 7.1 Candidate entities

Combining the domain expert's list with the structure the OASIS case demands:

**Physical / identity**
- **Vessel** (IMO as key)
- **Equipment** (main engine, aux engine, boiler, purifier, …)
- **Component** / sub-component (exhaust valve, FIVA, HCU, CCU, actuator, sensor)
- **Maker** and **Model** — *open question: separate nodes, or attributes on
  Equipment?* Leaning toward separate nodes, so that cross-vessel matching can
  traverse "same model" as a first-class edge.
- **Software / firmware version** where relevant (ECS version mattered in OASIS)

**Problem description**
- **Symptom** — observable, reported by crew (knocking sound, black smoke)
- **Alarm / Fault code** — machine-generated, canonical
  (`EXH_VALVE_OPEN_STROKE_LOW`), distinct from a symptom
- **FailureMode** — canonical classification of the problem
- **Reason / RootCause** — what it actually turned out to be (may be absent)

**Response**
- **Diagnostic** — a test performed, with its **Finding**
- **Action / Intervention**, carrying an **outcome**. The domain expert's
  "helpful action" and "blocked one" are, in our view, the same node type
  distinguished by an outcome attribute rather than two separate types:
  `resolved` · `partial` · `no_effect` · `made_worse` · `blocked` · `unknown`
  — leaving one place to add outcomes later without changing the schema.
- **Part** — spares requested/consumed
- **Actor** — by **role** (Chief Engineer, Technical Superintendent, Electrical
  Superintendent, OEM support, vendor, agent), not primarily by named individual
- **Organisation** — OEM, service provider, agent, yard

**Process / commercial**
- **CommercialGate / Blocker** — quotation, warranty dispute, PO, prepayment,
  agent and berthing details. OASIS shows these dominate elapsed time and are as
  worth learning as the technical steps.

**Provenance**
- **Evidence** — every extracted assertion must point back to the specific mail
  and/or attachment it came from, so answers can be cited and audited.

### 7.2 Two kinds of connection

- **Within a vessel / within a case** — the temporal and causal chain, plus
  recurrence (in OASIS the same failure mode appeared on cylinders 2, 4 and then
  5; that migration is itself a signal).
- **Across vessels** — through the canonical layer:
  `FailureMode` + `Model` + `Component`. This is the edge that lets one vessel's
  live problem reach thirty other vessels' history — and it is the edge that
  plain text similarity cannot provide reliably.

---

## 8. Known hard problems

1. **Extraction quality.** Turning 46 mails plus attachments into a reliable
   action→outcome chain is the make-or-break step. Everything downstream inherits
   its error rate.

2. **Canonicalisation / entity resolution.** Without a controlled vocabulary for
   symptoms, alarms, failure modes, components and actions, we will end up with
   hundreds of spellings of the same node and the graph will not connect. This is
   an ongoing problem, not a one-time cleanup.

3. **No resolution signal.** Confirmed: there is **no** closure flag, closing
   report or defect-closure record. Threads simply stop. A large share of the
   corpus will have unknown outcomes. The model must be able to represent
   *"attempted, outcome unknown"* honestly, and the answer layer must never
   present an unverified attempt as a fix.

4. **Attachments.** Maker/model, OEM troubleshooting procedures, defect reports,
   dump-file analyses, quotations and tariffs are all behind attachment links
   (`.pdf`, `.doc`, `.xls`, `.jpg`, `.zip`, `.eml`). Substantial technical content
   is unreachable without processing them.

5. **Incomplete equipment master data.** Machinery collection and PMS coverage
   are partial. Equipment identity will have to be reconciled from several
   sources of varying reliability, with a confidence notion attached.

6. **Duplicate and evolving records.** `casefilePlans[]` are rolling snapshots —
   the same event is restated in every subsequent plan. Events must be extracted
   once and deduplicated, or the graph will heavily over-count.

7. **Answer safety.** These are engineering recommendations on live machinery.
   The system must distinguish "this is what the fleet's history shows" from
   "this is what you should do", cite sources, and surface how many precedents
   support a claim and how strong they are.

---

## 9. Open questions

- Do **Maker** and **Model** become first-class nodes, or attributes of Equipment?
  (Current lean: first-class nodes.)
- Should **Symptom** (crew-observed) and **Alarm/fault code** (machine-generated)
  be one entity type or two? (Current lean: two.)
- Are "helpful action" and "blocked action" separate node types, or one **Action**
  node with an outcome attribute? (Current lean: one node, outcome attribute.)
- How deep does the component hierarchy go, and where does it come from — do we
  adopt an existing taxonomy or derive one from the corpus?
- What is the granularity of a **Case** — one casefile, or can one casefile
  contain multiple distinct problems that should be split?
- How do we represent **confidence** — on extraction, on equipment identity, and
  on the causal link between an action and an outcome?
- How far back does the corpus need to go for v1, and does older data have the
  same casefile structure?

---

## 10. Definition of success (draft)

A shore-side superintendent describes a defect in free text. The system:

1. identifies the vessel's equipment, maker and model
2. finds genuinely comparable precedents — filtered by equipment, not just text
3. reports what was tried, what worked, what didn't, and how often
4. names likely root causes with supporting counts
5. flags the parts, vendors and commercial steps that are likely to be needed
6. cites the specific mails and attachments behind every claim
7. is explicit about what remains unknown

…and does so fast enough to be used while the problem is still live.
