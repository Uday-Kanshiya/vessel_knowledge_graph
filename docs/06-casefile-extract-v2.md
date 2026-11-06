# Casefile extract v2 — the field list of record

> Status: **proposal, for discussion**
> Date: 2026-09-16
> Supersedes the v1 per-casefile extract (`casefile_schema.json`, KING ONE worked pair). Schema:
> `schema/casefile_extract.v2.schema.json`; sample: `schema/king_one_casefile_….v2.json`. Field semantics
> for faults and actions are in `05-action-log-and-outcomes.md`; this doc is what changed and how the record
> is built.

---

## 1. Removed from v1, and why

| v1 field | Gone because |
|---|---|
| `casefile` (title) | export label; the record is identified by `casefile_id` |
| `casefile_summary[]` | LLM narrative — the kind of summary 01 §5.2 says deletes failed attempts |
| `other_issues[]` | replaced by `faults[]`, one row per (symptom, equipment[, problem]) |
| `mails[]` and everything on a mail (`title`, `summary`, `kind_of_doc`, `confidence`, `sender`, `subject`) | no per-mail information in this record; mails are cited by `mail_id` only. Mail and attachment digests live in their own collections (04 §2) |
| `mails[].attachments[]` | same; cited by `attachment_sha` in `provenance` |
| `equipment.{maker,model,equipment_details}` as three value lists | replaced by `equipment[]` with an `equipment_id` a fault can point at, and maker/model on the same row |
| `counts` | derivable |
| `source_export`, `export_meta.audit_only/importance/weblink_key` | audit noise; `export_meta` keeps category, UICategory, tags, status, link |

## 2. Kept

`casefile_id`, `imo`, `vessel_name` (audit + subject-stem stopwords), `problem_statement` (one line, from
attachments), `export_meta` (verbatim, never filtered on), `notes`.

## 3. Added

`extraction_version`, `progress {last_pagenum_processed, complete}`, `equipment[]`, `problems[]`,
`faults[]`, `action_logs[]`, `validation_flags[]`, `needs_review`.

### 3.1 Ids and links

Four id families, all assigned by the extractor in order of first appearance and never renumbered:
`e1…` equipment, `p1…` problems, `f1…` faults, `a1…` actions. The links between them are explicit fields:

```
faults[].equipment_id   -> equipment[].equipment_id      (required)
faults[].problem_id     -> problems[].problem_id         (null until a document concludes the cause)
action_logs[].fault_id  -> faults[].fault_id             (required; exactly one)
```

A problem row exists only when a document *concludes* a cause (service report, SOF, OEM letter). A
suspicion stays in `faults[].suspected_cause` as text and never becomes a `p` id. Several faults may share
one problem (one cause, symptoms on two cylinders). The validator rejects any link that does not resolve.
Ids are local to the record; the loader prefixes them with `casefile_id` for the global key. Every equipment, fault and action row carries
`provenance {mail_ids, attachment_shas, evidence_quote}`; every action carries `date_performed`,
`asserted_in_mail`/`date_reported`, and `outcome {value, verification, outcome_in_mail, outcome_date,
evidence_quote}` with `unknown` enforced by schema to mean no mail, no quote.

### 3.2 `faults[].symptom` is the failure mode, short (2026-10-25)

The field's description (schema, `window_update/v6`, `consolidate/v3`): *the failure mode as a short phrase
(≤6 words: what is wrong, e.g. 'knocking sound', 'water leak', 'fails to start'); which unit / where goes in
`position`; the full sentence as written goes in `provenance.evidence_quote`.* Until v5 the field held the
observation as reported, usually a sentence; the resolver's symptom decomposition (13 §8) then found that 20
of 41 wordings passed through whole or had to be queued, because the failure mode, the component and the
place were all inside one sentence. The sentence is not lost: it is the row's evidence quote. The validator
records `symptom_too_long:<fault_id>` (a soft flag, no `needs_review`) when a symptom still runs past 12 words.
Not asked for: counts or derived numbers; every field competes for attention with what was tried, what
happened and what proves it.

## 4. How the record is built — 5 mails at a time

```
record := header from the export (casefile_id, imo, vessel_name, export_meta), progress.last_pagenum = -1
for window in pages sorted by pagenum, size 6, stride 5:
    fetch the mail bodies of the window (pages[].link[0])
    fetch + digest the content-bearing attachments of those mails (sha cache first)
    record := LLM_update(record, window bodies, attachment digests)
        - may ADD equipment / faults / actions with the next free e/f/a id
        - may UPDATE an existing action's outcome, date_performed, parts, target
        - may ADD a problem row and set fault.problem_id once a document concludes the cause
        - may NOT remove or renumber anything          -> code asserts, flags dropped_object
    progress.last_pagenum_processed := window's last pagenum   (checkpoint; resumable)
consolidate (one call over the record only):
    merge duplicate actions; assign seq by date_performed else date_reported; set occurrence_no
    write problem_statement from the attachments
validate (no model): schema; every fault_id/equipment_id/problem_id resolves; every quote is found verbatim in its
    cited mail or document; value != unknown has outcome_in_mail + quote; cap ~25 actions / casefile
progress.complete := true
```

The one-mail overlap between windows keeps immediate reply context; the carried record is what makes an
outcome reported in window 7 land on an action opened in window 2 (late binding, 02 §2.2). If the source
casefile's `updatedAt` moves, the record is rebuilt from scratch — no incremental state outside the record.

## 5. KING ONE in v2 (sample)

2 equipment, 1 problem, 4 faults (only f1 linked to p1), 10 actions. Of the 10 outcomes: 3 `resolved`/`conclusive`, 2 `partial`, 5 `unknown`
— including the bellows renewal (a2) that was later re-investigated (a4) but never stated to have failed.
One fault (f3, jacket-water seals) has no actions at all. Both are correct output.

The sample was written from the v1 digests to check the shape; quotes and `attachment_shas` become
verbatim/full-length only on a real run over fetched bodies.
