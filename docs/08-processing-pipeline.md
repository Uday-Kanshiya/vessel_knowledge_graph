# The processing picture so far, and KING ONE through it

> Status: **consolidated view of 04–07**, 2026-09-17
> Nothing new is decided here; this is the end-to-end order of operations as the docs stand, so it can be
> read in one place. Field semantics: 05 (faults, actions, outcomes), 06 (record and window loop),
> 07 (collections and the join). Schemas: `schema/casefile_extract.v2.schema.json`,
> `schema/attachment_digest.v1.schema.json`.

---

## 1. Pipeline

```
 casefile export (source Mongo, read-only)
 ──────────────────────────────────────────
   _id, imo, casefile, category, UICategory, tag, pages[] {referenceId, pagenum, dateTime, link[0], attachments[]}
          │
          ▼
 ① HEADER  (no model)
   record := { casefile_id, imo, problem_statement = export.casefile (verbatim),
               export_meta {category, UICategory, tags}, progress {-1, false},
               equipment [], problems [], faults [], action_logs [] }
          │
          ▼
 ② WINDOW LOOP  — pages sorted by pagenum, window 6, stride 5 (one mail overlap)
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ a. fetch the 6 mail bodies      pages[].link[0] → HTML → text (quotes     │
   │                                  stripped) → mail_bodies cache by mail_id │
   │ b. attachments of those mails   for each URL: fetch bytes → sha256        │
   │      └ lookup vessel_issues_mail_attachments by sha                       │
   │          hit  → append {mailbodyId, casefileId, url, name} to links[]     │
   │          miss → junk filter (name/size/dims) → content_bearing            │
   │                 → if content-bearing: read (pdf_text | pdf_ocr | vision   │
   │                   | docx_text | xlsx_table | eml_parse | listed_only)     │
   │                   → one digest call → {kind_of_doc, summary, tags,        │
   │                   maker, model, equipment_details} → insert with text     │
   │ c. LLM_update(record, 6 bodies, digests of their content-bearing files)   │
   │      may ADD    equipment e*, problems p*, faults f*, actions a*          │
   │      may UPDATE an action's outcome / dates / parts / target,             │
   │                 a fault's problem_id                                      │
   │      may NOT    remove or renumber  → code asserts (dropped_object)       │
   │      every row cites mail_ids [+ attachment_shas] + evidence_quote        │
   │ d. progress.last_pagenum_processed := window's last pagenum (resumable)  │
   │    write windows/wNNN.json (+ .prompt.txt) with inputs, response, delta  │
   └────────────────────────────────────────────────────────────────────────────┘
          │  repeat until the last page
          ▼
 ③ CONSOLIDATE  (one model call over the record only)
   merge duplicate actions · seq by date_performed else date_reported · occurrence_no
          │
          ▼
 ④ VALIDATE  (no model)
   schema · e/p/f ids resolve · every cited sha exists with a link on a cited mail
   · every evidence_quote found verbatim in the cited mail body or document text
   · outcome != unknown ⇒ outcome_in_mail + quote · cap ~25 actions
   → validation_flags[], needs_review; record loads either way
          │
          ▼
 ⑤ STORE
   vessel_issues_incident_reports   ← the record (one doc per casefile)
   vessel_issues_mail_attachments   ← digests (one doc per sha, links[] across casefiles)
   mail_bodies                      ← text cache by mail_id
          │
          ▼
 ⑥ LOAD / RESOLVE  (Part 3, not built yet)
   flat faults + action_log projections · action_canonical, symptom/equipment canonical via the resolver
   · casefile_id-prefixed global ids · cross-casefile thread merge · graph
```

What the model is asked, and nothing else: per window — new equipment/problems/faults/actions with ids
and quotes, outcome updates a mail states; per new file — one digest. No summaries of the casefile, no
titles for mails, no counts, no durations, no canonical names, no inferred outcomes (05 §5).

Re-run rule: if the source casefile's `updatedAt` moves, the record is rebuilt from the header; the
attachment collection and mail cache make the re-run cheap.

Open before this runs for real: `assets-prod.siya.com` reachability from the processing environment
(02 §8.5, 04 §5) — blocked from the sandbox and local shell, untested from k8s.

---

## 2. KING ONE (casefile 69a6965bf935cb9be885b8ee) through the pipeline

Sizes: 218 pages → **44 windows**; 166 attachment links → **53 distinct files → 45 content-bearing**
(84 links are 8 signature/logo files; one Leth logo alone is 68 links, digested once as `skipped`).

| Window (pages) | What arrives | What changes in the record |
|---|---|---|
| 0–5 … 15–20 (p. 18, 8 Mar) | shipyard mail with the C/E defect report `.docx` (sha `5b937c51…`, `docx_text`) | + `e1` stbd main engine, `e2` port main engine (maker/model still "NA" — the report doesn't state them) · + fault bellows cracked (`e1`), fault jacket-water seals (`e2`) · + actions "reduced speed to 136 rpm", "renewed bellows at Sri Lanka", both `outcome: unknown` |
| 20–25 (p. 22, 10 Mar) → 80–85 (p. 82, 27 Apr) | ~60 mails of guarantee-claim logistics: AWB, packing list, Cairo storage, Suez agent costs | + commercial action "guarantee claim E013 for 2 expansion joints" on the bellows fault; outcome later updated to `partial` from the 27 Apr "still stored at Cairo airport" mail |
| 95–100 (p. 98, 13 May) | shipyard to send two engineers at Singapore | + diagnostic action "investigate bellows cracking", `unknown` — nothing later states what they found |
| 100–190 | customs, logbooks, DNs, invoices, crew cabins, agent notices | nothing — the extractor adds no rows; junk logos hit the sha cache |
| 175–180 (p. 178, 29 Jul) | "5 service engineers boarded 0740 LT" | + corrective action "idle gear replaced by J-ENG engineers" on a fault that does not exist yet → the extractor must mint the fault from the mail's own wording (idle gear failure) and the SOF window later enriches it |
| 185–190 (p. 187–192, 30 Jul) | "ME repair completed and tested normal" | outcome of that action → `resolved`, `confirmed_by_vessel`, `outcome_in_mail` p. 192 |
| 195–200 (p. 196, 30 Jul) | signed J-ENG **service report** PDF (sha `73da2fa2…`, `pdf_text`) — maker J-ENG, model 6UEC35LSE-C1-LPSCR, T.R.H. 3076 h | `e1.maker/model` filled · + `p1` "free-end bearing worn, thrust collars broken", `asserted_by: oem` · fault.problem_id = p1 · action outcome verification raised to `confirmed_by_oem` |
| 195–205 (p. 198–203, 31 Jul) | running-in procedure and RPM instructions | + preventive action "running-in 136→152 rpm", `advised_by_org` J-ENG |
| 205–210 (p. 208–210, 1 Aug) | C/E running-in report; superintendent approves | outcome → `partial`, `reported_only` |
| 215–217 (p. 216, 11 Aug) | Master's SOF package: three SOFs + service report again (sha hit → link appended) | fault symptom/dates completed ("alloy debris found 20 Jul", `first_observed_at`) · + diagnostic "opened crankcase, inspected bearings" → `conclusive`, finding "No.1 crosshead OK; idler gear bush spalling" · + administrative "transit cancelled" · + commercial "spare idle gear on board 26 Jul" → `resolved` · + fault cover-plate bolts fractured 4 Aug, + corrective "bolts renewed", `unknown` |
| consolidate | | `seq` 1–10 by date; no duplicates to merge in this casefile |
| validate | | 4 faults → 2 equipment, 1 → p1; 10 actions → faults; all shas exist with a link on a cited mail; 0 flags |

Result (`schema/king_one_casefile_….v2.json`): `problem_statement` "Delivery Assistance for Vessel in
Suez" (export title), 2 equipment, 1 problem, 4 faults, 10 actions — outcomes 3 `resolved`/`conclusive`,
2 `partial`, 5 `unknown`; one fault (jacket-water seals) with no action at all. The idler-gear fault is
`f1` in the sample by importance; in a real run ids follow first appearance, so the bellows fault would be
`f1` and the idler gear `f3` — the ids are labels, not ranks.

What this casefile demonstrates: the fault the export title is *not* about is the one with the complete
chain (symptom → diagnostic → problem → parts → repair → running-in), and the whole chain is dated and
cited to two SOFs and one service report — the attachments, not the 218 mails, carry it.
