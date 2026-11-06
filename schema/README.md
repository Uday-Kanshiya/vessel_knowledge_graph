# schema/ — the final extraction schemas, and what changed to get here

> 2026-09-17. Two files are the contract; everything else in this folder is a worked sample.
>
> | File | What |
> |---|---|
> | `casefile_extract.v2.schema.json` | one record per casefile → Mongo `vessel_issues_incident_reports` |
> | `attachment_digest.v1.schema.json` | one document per distinct file (sha256) → Mongo `vessel_issues_mail_attachments` |
> | `king_one_casefile_69a6965bf935cb9be885b8ee.v2.json` | KING ONE in the v2 shape (0 schema errors) |
> | `king_one_attachments.v1.sample.json` | six of KING ONE's 53 files in the digest shape (0 schema errors, 0 join failures) |
>
> The pipeline that produces them is `docs/08-processing-pipeline.md` / `.drawio`. Field semantics: docs/05–07.

There were two starting points: the **v1 casefile extract** (`casefile_schema.json`, the first KING ONE
worked pair) and the **team's Mongo sketch** (`docs/vessel_issues_schema.json`: an attachment collection
and an "incident report" collection). The final schemas replace both. What changed, and why:

---

## 1. Casefile record — v1 extract → `casefile_extract.v2`

### Removed

| v1 field | Why |
|---|---|
| `casefile` (title), `vessel_name`, `source_export`, `extraction_version` | export labels and run metadata; `casefile_id` is the identity, `imo` the vessel |
| `casefile_summary[]` | LLM narrative — the kind of summary that deletes failed attempts (01 §5.2) |
| `other_issues[]` | replaced by `faults[]` |
| `mails[]` with `title`, `summary`, `kind_of_doc`, `confidence`, `sender`, `subject` | **no per-mail information** in the record; a mail is cited by `mail_id` only, and the export's `pages[]` already holds its date, sender and subject |
| `mails[].attachments[]` (per-link title, summary, maker/model) | moved out to the attachment collection; cited by sha |
| `equipment.{maker[], model[], equipment_details[]}` as three value lists | replaced by `equipment[]` rows with an `equipment_id` a fault can point at |
| `counts`, `export_meta.status / casefile_link / importance / weblink_key / audit_only` | derivable or audit noise |

### Kept

`casefile_id`, `imo`, `export_meta` (now only `category`, `UICategory`, `tags` — audit, never filtered on),
`notes`. `problem_statement` is kept but **changed in origin**: it is the export's `casefile` field copied
verbatim, no longer written by the extractor.

### Added

| Field | What it is |
|---|---|
| `progress {last_pagenum_processed, complete}` | the resume cursor for the 5-mail window build (window 6, stride 5); not a statistic |
| `equipment[]` | `e1…` rows: name, equipment_type, position, maker, model, serial, details, provenance |
| `problems[]` | `p1…` rows: a cause a document **concluded**, with `asserted_by` and provenance. Suspicions never become a problem |
| `faults[]` | `f1…` rows: the (symptom, equipment) pair being fixed, with `equipment_id` → e*, `problem_id` → p* or null, `suspected_cause`, `alarms[]`, `position`, `first_observed_at`, provenance |
| `action_logs[]` | `a1…` rows, one per action occurrence: `fault_id` → f*, `seq`, `occurrence_no`, `target`, `action`, `kind` (corrective/diagnostic/preventive/administrative/commercial), `performed_by_role`, `advised_by_org`, `parts[]`, `date_performed`, `asserted_in_mail` + `date_reported`, `outcome`, `finding`, provenance |
| `outcome` on each action | `{value, verification, outcome_in_mail, outcome_date, evidence_quote}`; `unknown` is a value and the expected majority; schema forces `unknown ⇒ no mail, no quote` and `≠ unknown ⇒ mail + quote` |
| `provenance` on every row | `{mail_ids[], attachment_shas[], evidence_quote}` — no quote, no assertion |
| `validation_flags[]`, `needs_review` | output of the deterministic validator; flagged records still load |

### The links (the part the sketch left as "structure to be decided")

```
faults[].equipment_id   -> equipment[].equipment_id
faults[].problem_id     -> problems[].problem_id      (null until concluded)
action_logs[].fault_id  -> faults[].fault_id          (exactly one)
```

One foreign key on the action resolves symptom, equipment and problem at once and keeps the pairing —
three id lists on the action would lose which symptom was on which equipment and count the action three
times. Ids are short, assigned in order of first appearance, never renumbered across windows; the loader
prefixes them with `casefile_id`.

### Reviewed and not adopted

Derived outcomes (a per-mail symptom-status timeline joined to the action date, recurrence retro-tagging,
`superseded_by`): the mails are too vague for a mechanical join; a wrong inferred `no_effect` is worse than
an honest `unknown`. Outcomes are what a mail states, with a quote, or `unknown` (05 §5).

---

## 2. Attachment collection — team sketch → `attachment_digest.v1`

| Team sketch (`vessel_issues_mail_attachemnts`) | Final | Why |
|---|---|---|
| one doc per attachment link (`mailbodyId`, `casefileId`, `attachment_url`, `attachment_name`) | **one doc per distinct file**, keyed by `sha256`, with `links[] {mailbodyId, casefileId, attachment_url, attachment_name}` | the same file recurs: KING ONE 166 links → 53 files; one OSIS PDF on 24 links; one logo on 68 mails. Digest once, cite by sha |
| `attachment_type` (server MIME) | dropped | the server mislabels `.eml` as PDF; the reader decides by bytes |
| `summary`, `tags` | kept; `tags` a closed list from config | free-text tags fragment like symptom wording |
| `equeipment` (one string) | `maker`, `model`, `equipment_details` — "NA" unless **this** document states them | one string cannot be joined to PMS or compared across vessels |
| — | `content_bearing` | 84 of KING ONE's 166 links are logos; decided once, never re-made; a cited non-content-bearing sha is a validation flag |
| — | `kind_of_doc` | service report / SOF / defect report / invoice / AWB / passport (classify only) / archive (not opened) |
| — | `extracted_text` \| `text_ref` | the validator matches `evidence_quote` against it; re-runs don't re-OCR |
| — | `extraction_method` | pdf_text · pdf_ocr · vision · docx_text · xlsx_table · eml_parse · listed_only · skipped |

Not carried over from the earlier draft of this collection (04 §2.1): `detected_type`, `bytes`,
`page_count`, `extraction_version`.

---

## 3. Incident report — team sketch → the v2 record

The team's `vessel_issues_incident_reports` **is** the v2 casefile record; nothing separate exists.

| Sketch | Final |
|---|---|
| `symptoms[]` strings | `faults[].symptom`, stored with its equipment (a flat list is a projection, not a second copy) |
| `equipments[]` strings | `equipment[]` rows with ids, maker, model |
| `problems[]` strings | `problems[]` rows with ids, `asserted_by`; linked from `faults[].problem_id` |
| `actions[].action_name / action_description` | `action_logs[].action` + `kind` + `target` + `parts` |
| `"related problems and equipment and symptoms" → TBD` | `action_logs[].fault_id` |
| `result of the action`, `result_status: success\|failure\|partial` | `outcome {value ∈ resolved\|partial\|no_effect\|made_worse\|unknown (diagnostic: conclusive\|inconclusive\|no_information\|unknown), verification, outcome_in_mail, outcome_date, evidence_quote}` |
| `reference: [mailbodyId…]` | `provenance.mail_ids` + `asserted_in_mail` + `outcome.outcome_in_mail` |
| `casefile_log` | not stored — it is `action_logs[]` sorted by date, rendered |

---

## 4. The join between the two schemas

```
record …provenance.attachment_shas[i]  ==  attachments.sha256
record …provenance.mail_ids            ∩   attachments.links[].mailbodyId  ≠ ∅
```

Enforced by the validator, no model: every cited sha exists; one of its links is on a mail the row cites;
the row's quote is found in that document's text. The extractor never sees a URL or file name — only shas
and digests.

---

## 5. Standing rules the schemas encode

- `casefile_id` is the identity; `imo` identifies the vessel; `vesselName` is not stored.
- `UICategory` / `tags` are audit hints, never filters.
- Everything is read from fetched `pages[].link` bodies and attachment bytes, never from the export's
  summary fields; `problem_statement` is the one field copied from the export, and it is the title.
- The model is asked exactly two things: per window, new `e/p/f/a` rows with ids and quotes plus outcome
  updates a mail states; per new file, one digest. Never summaries, titles, counts, durations, canonical
  names or inferred outcomes.
- `unknown` is a value. A vague mail stays `unknown`.
