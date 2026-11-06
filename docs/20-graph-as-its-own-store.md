# The graph as its own store: ownership, ingestion, memory layers, citation

> Status: **proposal, for discussion** · Date: 2026-11-06
> Amends docs/18 §1 and §8 and docs/19 §1: the graph is **not** a rebuildable cache of the JSON extracts.
> It is the system of record, with its own ids and its own accumulated knowledge; a casefile extract is an
> **ingest event** that is applied once, and afterwards only **cited**. Traversal and answering read graph
> tables only — never a record.json, never the Mongo export.

---

## 1. Ownership rules

1. **The graph owns its ids.** Every node and hop gets a graph-minted surrogate id (`sym_0001`,
   `act_0042`, `hop_000917`), assigned once, never renumbered, never derived from `casefile_id` or the
   extractor's `e1/f1/a1`. Those local ids survive only as a *citation alias* (§4).
2. **Extracts are inputs, not sources.** After `ingest`, deleting `data/extractions/` must change nothing
   the graph can answer. The test: run every §5 query with the extracts directory removed.
3. **Nothing in the graph is overwritten by an ingest.** Assertions are added or retracted with a time
   stamp; curation (merges, approvals, families) lives in tables an ingest never touches; derived knowledge
   (threads, attempt classes, verdicts) is re-derived incrementally from the graph's own tables.
4. **Rebuild is for derived layers only.** `--fresh` in docs/18–19 is replaced by `rederive`, which
   recomputes layer C from layers A + B. There is no command that drops A or B.
5. **Citation is a pointer out, never a dependency in.** A hop cites `casefile_id`, `mail_id`,
   `attachment_sha`, the quote, and the extract snapshot it came from; the graph can *show* the source
   on request but never needs it to traverse.

## 2. Four memory layers in one store (`data/kg/kg.sqlite`, later Neo4j/Postgres behind `GraphStore`)

| Layer | Holds | Written by | Survives re-ingest | Rebuildable |
|---|---|---|---|---|
| **A · Assertions** | the five node kinds' *occurrences* and the four hops (docs/18 §3), each with `asserted_at`, `retracted_at`, `source_id` | `ingest` only | yes (append / retract) | **no** — this is the data |
| **B · Vocabulary & curation** | `standard_term`, `alias`, `family`, `pending_term`, `resolution_log`, human merges / splits / approvals, `thread_merge_log` decisions a person confirmed | resolver + humans | yes | **no** — this is what the graph has learned |
| **C · Experience (derived)** | `thread`, `thread_seq`, `duplicate_of`, `attempt_class`, `effective_result`, `action_efficacy`, `followed_by` — stored as tables/edges, updated incrementally after each ingest | `rederive` | recomputed | yes, from A + B |
| **D · Citations** | `source(source_id, casefile_id, extract_version, extract_sha, ingested_at)`, `extract_snapshot(extract_sha, json)`, `evidence(mail_id, attachment_sha, quote)` | `ingest` | yes | no |

Layer A is bitemporal-lite: a row is live while `retracted_at IS NULL`. Every query filters on that, so the
graph can also answer "what did we know about this thread in March" by filtering on `asserted_at`.

## 3. Ingest contract (one casefile extract → graph deltas)

```
ingest(record.json):
  src   := source row (casefile_id, extraction_version, sha256(record))      -- idempotent: same sha ⇒ no-op
  snap  := extract_snapshot(sha, json)                                        -- the extract is now citable from inside the graph
  new   := assertions derived from the record (docs/18 §4 mapping), each keyed by
           assertion_key = sha256(casefile_id | local_id | kind | normalised content)
  live  := assertions currently live for this casefile_id
  for a in new  − live : INSERT with asserted_at = now, source_id = src       -- resolve wording → standard via layer B
  for a in live − new  : SET retracted_at = now                               -- re-extraction dropped or changed it
  for a in new  ∩ live : nothing                                              -- unchanged, keeps its original asserted_at and id
  rederive(scope = this casefile's threads)                                   -- layer C, incremental
  ledger(src, n_added, n_retracted, n_unchanged, n_queued_terms)
```

A changed outcome on an existing action is a retract of the old `hop_action_result` row and an insert of
the new one — the old result remains in history with its own citation. Resolution goes through layer B's
alias table, so a re-ingested record lands on the **same** canonical nodes a human already curated;
unknown wordings go to `pending_term` and the hop points at an `auto` standard until reviewed.

## 4. Citation model

```
hop ──source_id──▶ source ──extract_sha──▶ extract_snapshot      (the JSON, inside the graph)
     └──evidence_id──▶ evidence (mail_id, attachment_sha, quote) ──▶ casefile browser / mail cache (outside)
hop.local_ref = "69a69…:a2"                                       (the extractor's id, for humans reading the extract)
```

A query answer carries, per step: the quote, the mail id, the casefile id, and the extract sha. The site
(docs/12) dereferences those to the mail page; the graph itself never follows them.

## 5. Traversal on its own memory

Everything docs/18 §7 and docs/19 §6 do stays SQL over layer A + C tables, with `retracted_at IS NULL`
added to every hop join. Two things change from the cache design:

- **`next` is stored, not joined.** `edge_next(hop_from, hop_to, thread_id)` is materialised at `rederive`
  so a chain is walked by following rows, not by the extractor's `seq` arithmetic; `seq` is kept as a
  citation of the extractor's ordering.
- **Aggregates are tables, updated incrementally.** `action_efficacy` and `followed_by` are recomputed for
  the `(problem_id, action_id)` pairs an ingest touched, not for the fleet.

The in-memory NetworkX graph (`GraphStore`) is loaded from layers A + C at start-up for rendering and
bounded path queries; it is a reader of the store, never a second copy of the truth.

## 6. Curation operations (layer B) and why they must not be rebuilt

`merge(standard a → b)` rewrites `alias.standard_id` and logs; `split`, `approve`, `assign_family`,
`confirm_thread_merge`, `reject_thread_merge` likewise. Each is a logged, reversible row — the graph's
memory of *decisions*, which is exactly what a rebuild-from-JSON would lose. Re-deriving layer C after a
merge re-threads and re-classifies the affected hops; layer A rows are untouched because they point at
standards by graph id.

## 7. What changes in the earlier docs

| Doc | Was | Now |
|---|---|---|
| 18 §1 | "SQL tables are the source; NetworkX a view; rebuild `--fresh`" | SQL store is the system of record; `rederive` replaces `--fresh`; NetworkX is a reader |
| 18 §3 | ids `cf:<id>/a1` | graph-minted ids; `cf:<id>/a1` becomes `local_ref` for citation |
| 18 §4 | "the loader" | `ingest`, idempotent by extract sha, assert/retract by `assertion_key` |
| 19 §1 | "all three pointers are rebuilt by `--fresh`" | `standard_id` is set by layer B at ingest and changed only by curation; `thread_id` / `duplicate_of` are layer C, re-derived incrementally |
| 02 §3 / kg_build_decisions | "NetworkX only, serialised to json/graphml" | graph store on disk is the truth; json/graphml are exports |

## 8. Acceptance tests

| Test | Pass when |
|---|---|
| delete `data/extractions/`, run every §5 query | identical answers |
| ingest the same record twice | ledger: 0 added, 0 retracted; ids unchanged |
| re-ingest a record whose one outcome changed | 1 retract + 1 insert on `hop_action_result`; old row visible with `retracted_at`; chain still walks |
| human merges two action standards, then re-ingest a record using the old wording | lands on the merged standard; no new `auto` term |
| ask for the citation of any step in an answer | quote, mail id, casefile id, extract sha resolve inside the store |
