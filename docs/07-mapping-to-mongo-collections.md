# How the v2 extract maps onto `vessel_issues_schema.json`

> Status: **proposal, for discussion**
> Date: 2026-09-17
> Answers two questions: does dropping attachment records from the casefile record lose anything, and is
> the v2 record an "incident report" in the sense of the team's Mongo sketch. Short answer to both: the v2
> record IS `vessel_issues_incident_reports`, and the attachment collection stays — as a sha-keyed digest
> store the record cites, not as rows inside the record.

---

## 1. Attachments — what was removed and what was not

Removed from the casefile record: the per-link attachment rows (title, summary, kind, maker/model per
link). Not removed: attachment processing. Every window still fetches and digests the content-bearing
attachments of its mails (06 §4), and every equipment / problem / fault / action row cites the documents it
was read from in `provenance.attachment_shas`.

So a separate attachment collection is needed, and it improves the process on three counts:

| Need | Why a collection, not inline rows |
|---|---|
| **Digest once, reuse everywhere** | the same file recurs: KING ONE 166 links → 53 distinct shas; across 40 casefiles 1,979 links → 920 file names, one PDF on 24 links. Keyed by sha256, a digest is computed once and reused across windows, casefiles and re-runs. Inline rows re-digest it every time (v1 stored the KING ONE service report 2× and the OSIS spares PDF would be 24×). |
| **Resolve the citation** | `attachment_shas` in the record must resolve to a file name, URL and summary. That lookup is this collection. |
| **Validate quotes** | the validator checks every `evidence_quote` against the cited document's text. The extracted text (or a pointer to it) lives here; without it, quotes from attachments cannot be checked and a re-run with a new prompt re-OCRs every scan. |

Shape — `schema/attachment_digest.v1.schema.json`, sample `schema/king_one_attachments.v1.sample.json`.
One document per distinct file, keyed by sha:

```
vessel_issues_mail_attachments
  sha256                         UNIQUE        -- identity
  links[] {mailbodyId, casefileId, attachment_url, attachment_name}   -- every place the file appeared
  content_bearing: bool                        -- junk decided once, never re-made
  kind_of_doc
  summary, tags[]                              -- the digest (team's fields, kept; tags a closed list)
  maker, model, equipment_details              -- three fields, "NA" unless THIS document states them
  extracted_text | text_ref                    -- for the quote check and re-runs
  extraction_method                            -- pdf_text | pdf_ocr | vision | docx_text | xlsx_table | eml_parse | listed_only | skipped
```

`equeipment` as one free string is the only field that has to change: a single string cannot be joined to
PMS or compared across vessels.

Mails need no digest collection. `provenance.mail_ids` resolve to `pages[].referenceId` in the source
casefile collection, which already holds date, sender, subject and the body link. What the validator needs
is the **fetched body text**, cached by `mail_id` — a text cache, not a schema entity, and never an LLM
summary.

## 2. Incident report — the v2 record is it

The team's `vessel_issues_incident_reports` and the v2 casefile extract are one document per casefile with
the same content; the v2 record is the same collection with the loose ends tied:

| team's sketch | v2 record | note |
|---|---|---|
| `casefileId` | `casefile_id` | |
| `symptoms[]` (strings) | `faults[].symptom` | a symptom is stored **with** the equipment it was on, so "knocking on the main engine" and "knocking on the purifier" never merge (02 §3.4). A flat `symptoms[]` list, if wanted for a UI, is the loader's projection of `faults[].symptom` — not a second copy |
| `equipments[]` (strings) | `equipment[]` with `equipment_id`, maker, model, provenance | |
| `problems[]` (strings) | `problems[]` with `problem_id`, `asserted_by`, provenance; linked from `faults[].problem_id` | only concluded causes; suspicions stay on the fault |
| `actions[].action_name / action_description` | `action_logs[].action` + `kind` + `target` + `parts` | |
| `"related problems and equipment and symptoms"` (structure TBD) | `action_logs[].fault_id` → `faults[]` → `equipment_id`, `problem_id` | this was the open item; one FK resolves all three |
| `result of the action`, `result_status: success\|failure\|partial` | `outcome {value, verification, outcome_in_mail, outcome_date, evidence_quote}` | adds `unknown` (the expected majority), `no_effect` vs `made_worse`, the diagnostic vocabulary, and the mail that states it |
| `reference: [mailbodyId…]` | `provenance.mail_ids` + `asserted_in_mail` + `outcome.outcome_in_mail` | same ids, split by what each one evidences |
| `casefile_log` ("log you are going to build") | **not stored** — it is `action_logs[]` sorted by `date_performed` else `date_reported`, rendered | a stored narrative would be the LLM summary the record just dropped; render it from the rows instead |
| — | `progress`, `validation_flags`, `needs_review`, `export_meta` | build cursor, validator output, audit copy |

Nothing structural is lost by not having a separate "incident report": the record already carries the
report's four lists plus the links between them, and the links are the part the sketch left undecided.

## 3. How the two schemas join

The casefile record never carries attachment content; it carries citations. The join is:

```
casefile_extract.v2  …provenance.attachment_shas[i]   ==  vessel_issues_mail_attachments.sha256
casefile_extract.v2  …provenance.mail_ids             ∩   vessel_issues_mail_attachments.links[].mailbodyId   ≠ ∅
```

Rules the validator enforces (no model): every cited sha exists in the attachment collection; at least one
of its `links[].mailbodyId` is in the same row's `mail_ids` (the file was actually on a mail the row cites);
`evidence_quote` is found in that document's `extracted_text`/`text_ref` when the row cites a sha, else in
the mail body. A cited sha with `content_bearing: false` is a validation flag — the extractor read a logo.

Order of work per window (06 §4) is therefore: fetch mails → for each attachment URL, fetch bytes → sha →
look up; on a miss, junk-filter, digest, insert; on a hit, append the link if new → pass the digests
(summary, maker/model/details, kind) to the extractor with the mail bodies → the extractor cites shas.
The extractor never sees a URL or a file name, only shas and digests; KING ONE's 166 links become 53
lookups, 45 of them content-bearing, and a second casefile carrying the same J-ENG report costs nothing.

Checked on the KING ONE samples: 0 schema errors on either file, 0 join failures across all cited shas.

## 4. So the collections are

```
vessel_issues_incident_reports    = the v2 casefile extract, one doc per casefile   (schema/casefile_extract.v2.schema.json)
vessel_issues_mail_attachments    = sha-keyed digests, cited by provenance.attachment_shas   (§1, §3; schema/attachment_digest.v1.schema.json)
mail_bodies (cache)               = fetched text by mail_id, for the validator and re-runs   (not an entity)
```

Flat `faults` / `action_log` collections (05 §3) are the **loader's** fleet-query projection of the same
record, produced after resolution; the extractor writes only the record.
