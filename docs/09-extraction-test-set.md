# Extraction test set — 10 casefiles for the first real v2 run

> Status: **proposal, for discussion** (revised 2026-09-30: KING ONE added, OCEAN LOYALTY RFQ moved to §4)
> Date: 2026-09-30
> Source: `casefiles_export_20260824_110543.json` (40 casefiles, 1325 mails, 1979 attachment links).
> Purpose: run the v2 extract (06, 08) through the API pipeline on a set wide enough to show where it
> breaks. The ten are 533 mails (40 %), 646 attachment links, 109 windows — KING ONE alone is 218 mails
> and 44 windows.

---

## 1. Basis of selection

Chosen from export metadata only: mail count, date span, subject lines, sender domains, per-mail `tags`,
attachment names and types. The export's summaries were used as hints to describe a casefile, never as a
selection rule. Mail bodies were not fetched, so "what the extract should find" below is an expectation to
check, not a gold answer.

## 2. The set

| # | casefile_id | Vessel (header) | Export title | Mails | Picked for |
|---|---|---|---|---|---|
| 1 | `69c31dde585b6c770fc42db4` | OASIS | HFO storage tank leak incident details | 63 | many unrelated problems in one casefile |
| 2 | `69b38ef8481126094a0ee2f0` | OASIS | Knocking sound from main engine exhaust valve | 46 | recurring fault, attempts that did not fix it, maker thread |
| 3 | `69be26a6235747eb320771f0` | CAP SAN ARTEMISSIO | Crane Incident Damage Assessment and Reporting | 87 | wrong vessel in the header; third-party damage, class, claim |
| 4 | `69a6965bf935cb9be885b8ee` | KING ONE | Delivery Assistance for Vessel in Suez | 218 | largest casefile; logistics noise; Chinese mails; only run in chat so far |
| 5 | `6a311cbc20960ff945871f60` | PACIFIC PEARL | AE Alarm Activation Issues on Vessels | 41 | fleet circular, sister vessels, unreadable attachments |
| 6 | `6a364ba21fb51f367efb4fd0` | CORAL PEARL | Hyper mist Valve actuator malfunction issue | 36 | long diagnostic chain, flag state, late confirmation |
| 7 | `6a24407eb29c907dcf222c97` | CAP SAN ARTEMISSIO | Technical Forms Submission for May 2026 | 21 | negative control: routine forms, almost nothing to extract |
| 8 | `6a2dd03f84c786d6955ec42a` | WHITE ALBA | SCR Blow Off Valve Relay Replacement Issue | 12 | compact guarantee claim with shipyard; four outcomes in 12 mails |
| 9 | `69ecb2a27863ec27f053cecb` | MAERSK EL PALOMAR | Fuel Pump Overhaul and Routine Maintenance | 7 | fault found during planned maintenance; maker-stated cause |
| 10 | `6a372fc706f7689c0eefde1d` | KMARIN RESOLUTION | Main engine OMD sensor replacement process | 2 | minimal baseline, no attachments |

### What each one tests

1. **OASIS — HFO tank leak.** 24 Mar–14 Aug, 17 distinct subject lines, vessel + office only. Subjects cover
   a hole in HFO tank 1S (GC043), a flooded void space, a SW pipe leak in the cofferdam, M/E knocking on
   cyl 3 and cyl 6, cyl 5 exhaust-valve renewal, M/E failed to start, M/E load limitation, the guillotine
   bar, EGCS failure and a propulsion power failure. Tests fault separation (one `f` per symptom +
   equipment), several unrelated `equipment[]` rows, the ~25-action cap, and `problem_statement` = a title
   that names one problem of about ten.
2. **OASIS — Knocking sound.** 13 Mar–7 Apr, 15 mails from the maker (Everllence ticket thread), GC046.
   139 attachment links: 105 images, 17 inline images with no extension, 2 `.manbw-spaf` engine dumps,
   2 zips. Tests outcomes other than `resolved` (05 §4 already states what is expected for this saga),
   `problem_id = null`, and the `listed_only` path for unreadable files. Shares the subject "Renew exhaust
   valve for M/E cylinder No.5" with #1, so the pair shows one event split across two casefiles.
3. **Crane incident.** 21 Mar–18 Jun, 7 sender domains (charterer 33 mails, ClassNK, agents, owner).
   85 of 87 mails name CAP SAN TAINARO and 14 cite IMO 9633965; none names ARTEMISSIO (header IMO
   9633939). The header `imo` is copied into the record, so this shows whether the mismatch is caught.
   Contact damage to the funnel, not a machinery failure: temporary repair, permanent repair, class
   survey, cost claim — corrective, administrative and commercial actions in one chain. 185 links but 65
   distinct file names (the same photos forwarded up to 11 times) — sha cache.
4. **KING ONE — Delivery assistance in Suez.** 3 Mar–11 Aug, 13 sender domains, 3 of 218 mails tagged
   `defect`. So far extracted only in the chat interface; the v2 sample (06 §5, 08 §2) was written from
   the v1 digests, not from an API run — this run is the first to compare against it. The only casefile
   at this scale: 44 windows exercise the checkpoint/resume loop and the carried record (an outcome on
   p. 192 must land on an action opened on p. 178; equipment maker/model arrive on p. 196). About 46
   mails are port-agent traffic and 32 have Chinese subjects (customs notices, cost discussion) — tests
   restraint over long stretches where nothing should be added, and verbatim quotes in Chinese. The
   title names logistics; the complete chain is the idle-gear repair, carried by three SOFs and one
   service report. 166 attachment links, 53 distinct files, 84 links to 8 signature/logo files.
5. **PACIFIC PEARL — AE alarm.** 16 Jun–1 Jul, maker HD + vessel + office. The same LO-sump alarm fault is
   discussed for PACIFIC GARNET and PACIFIC EMERALD in the same thread; Garnet's setting files are
   attached. Tests that equipment and actions of other vessels are not written under this IMO, `target`
   across AE#1/#2/#3, a fix that is a configuration upload not a part, and 8 binary `.addr` files.
6. **CORAL PEARL — Hyper mist.** 20 Jun–17 Aug, 33 of 36 mails tagged `defect`, maker Kashiwa, flag
   administration. Day-by-day remote troubleshooting → many diagnostic actions; restoration reported
   26 Jun, function test 17 Aug → an outcome that must bind to an action opened several windows earlier.
   Two attached `.eml` files are a BLUEFIN PEARL thread (another vessel's precedent). Only fire-safety
   system and only flag-state correspondence in the set.
7. **CAP SAN ARTEMISSIO — Technical forms.** 6 Jun–12 Aug, 0 mails tagged `defect`, one sender domain,
   49 attachments of which 46 are routine form PDFs (ME/AE performance, bearing clearances, LO analysis…).
   The correct record is close to empty; shows whether the extractor invents faults from performance
   sheets. The last mails do carry technical content (scavenge-inspection findings 8 Jul, M/E unit 7
   decarb report 11–12 Aug) and should still be caught. Heaviest digest load per mail.
8. **WHITE ALBA — SCR relay (GC048).** 13–16 Jun, shipyard Chengxi in the thread, 10 of 12 subjects carry
   the Chinese reply prefix `回复:`. Relay 50K5 localised, interim fix, supplied relay did not match,
   correct relay fitted and tested — a diagnostic, a temporary fix, a failed attempt and a confirmed fix,
   all checkable by hand. Attachments pdf / xlsx / docx / jpg.
9. **MAERSK EL PALOMAR — Fuel pump overhaul.** 25 Apr–11 Aug, 0 mails tagged `defect`, one sender domain,
   4 xlsx + 1 pdf + 1 png. Broken spring found during the AE#4 8000 h routine — a fault with no alarm or
   reported symptom; cause attributed in forwarded maker comments (19 May) → a `problems[]` row. Evidence
   in spreadsheets (`xlsx_table`). The August mails are consumables stock queries after a 3-month gap.
10. **KMARIN RESOLUTION — OMD sensor.** 2 mails, same day, one window, no attachments (the only such
    casefile). Isolates mail-body extraction from attachment digests; the result is verifiable by reading
    two mails.

## 3. Coverage

| Dimension | Values in the 40 (casefiles) | Covered by |
|---|---|---|
| Mails per casefile | 2–5 (8) · 6–19 (15) · 20–51 (9) · 63–103 (7) · 218 (1) | #10 · #8 #9 · #2 #5 #6 #7 · #1 #3 · #4 |
| Distinct subject lines | 1–3 (18) · 4–9 (12) · 10+ (10) | #8 #9 #10 · #2 #5 #7 · #1 #3 #4 #6 |
| Share of mails tagged `defect` | 0 (4) · <25 % (10) · 25–75 % (13) · >75 % (13) | #7 #9 · #4 #5 · #1 #8 · #2 #3 #6 #10 |
| Span | ≤7 d (12) · 8–45 d (11) · >45 d (17) | #8 #10 · #2 #5 · #1 #3 #4 #6 #7 #9 |
| Attachments | none (1) · image-dominant (15) · PDF-dominant (8) · spreadsheet-heavy (5) · Word (18) · nested .eml (14) · zip (8) · unreadable binary (2) | #10 · #1 #2 · #6 #7 · #9 · #1 #2 #3 #4 #5 #8 · #2 #3 #4 #5 #6 · #2 · #2 #5 |
| Who writes | vessel + office only (11) · maker (13) · shipyard (2) · class (5) · flag (4) · charterer/owner (11) · agent (8) · vendor (7) | #1 #7 #9 #10 · #2 #5 #6 · #8 · #3 · #6 · #3 · #3 #4 · #4 |
| Guarantee-claim ref (GC/GCR) | 10 | #1 #2 #8 |
| Other vessel / IMO inside | 5 | #3 #5 #6 |
| Chinese-language mails | 1 (KING ONE); reply prefixes only in 2 more | #4 · #8 |
| `UICategory` without `defect` | 6 | #4 |
| Vessels (header) | 11 | 8 — OASIS ×2, CAP SAN ARTEMISSIO ×2, KING ONE, PACIFIC PEARL, CORAL PEARL, WHITE ALBA, MAERSK EL PALOMAR, KMARIN RESOLUTION |

## 4. Not covered, and the cheapest way to add it

| Gap | Casefile to add | Mails |
|---|---|---|
| Purchasing-portal notices and vendor ticket auto-replies around a fault (VDR); OCEAN LOYALTY. Dropped for KING ONE, which covers the same role (noise around a fault whose chain sits in a service report) at larger scale | `6a0ebae5b3ef1fac92fa275a` Variance in Quotation for RFQ OCLY-26-8 | 45 |
| A hand-built answer to diff against; JEFFREYS BAY | `69e8b40474879085df147b17` Fuel Valve Leak GCR 103 | 10 |
| Reefer-container reports (3 casefiles; equipment is cargo, not ship machinery) | `6a2e1f3384c786d6955ef8ba` RMR HLXU8802730 | 3 |
| EMPEROR PAMPERO (vessel mail via amosconnect) | `6a166c3c0e4644320ffeefbe` Expansion valve | 2 |
| One fault over months with guarantee claim + flag dispensation + class | `6a23353fb29c907dcf21139f` EGCS SO2 sensor GCR 114 | 75 |
| Charter-party / cargo-document casefile with an M/E stoppage buried in it | `69d622ec64b4310f9b27a4a7` OASIS Bunker tank configuration | 67 |

Also unrepresented: class-observation casefiles (PR 17, boiler coaming) and cargo-system equipment (inert
gas, stripping pump, tank radar).

## 5. Ids

```json
["69c31dde585b6c770fc42db4", "69b38ef8481126094a0ee2f0", "69be26a6235747eb320771f0",
 "69a6965bf935cb9be885b8ee", "6a311cbc20960ff945871f60", "6a364ba21fb51f367efb4fd0",
 "6a24407eb29c907dcf222c97", "6a2dd03f84c786d6955ec42a", "69ecb2a27863ec27f053cecb",
 "6a372fc706f7689c0eefde1d"]
```
