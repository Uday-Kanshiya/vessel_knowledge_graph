"""
Static site build (docs/12). No network, no model: an in-memory RecordSource with one small
record, one cached mail, one digest and one window audit.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.search.index import build_index, project
from src.site import views
from src.site.render import build, check_links, highlight_body, packaged_name
from src.site.source import DictRecordSource

MAIL_ID = "bbbbbbbbbbbbbbbbbbbbbbb1"
MAIL_ID_2 = "bbbbbbbbbbbbbbbbbbbbbbb2"
SHA = "a" * 64

RECORD = {
    "casefile_id": "cccccccccccccccccccccccc",
    "imo": 9999999,
    "vessel_name": "TEST VESSEL",
    "extraction_version": "casefile_extract.v2+wu.v5+co.v2+ad.v1",
    "progress": {"last_pagenum_processed": 2, "complete": True},
    "problem_statement": "Main engine OMD fault",
    "export_meta": {
        "category": "General",
        "UICategory": ["defect"],
        "tags": ["defect"],
        "status": "active",
        "casefile_link": "https://example.invalid/casefile/c",
    },
    "equipment": [
        {
            "equipment_id": "e1",
            "name": "M/E",
            "equipment_type": "main_engine",
            "position": None,
            "maker": "NA",
            "model": "6S50ME",
            "serial": "NA",
            "details": "NA",
            "provenance": {
                "mail_ids": [MAIL_ID],
                "attachment_shas": [],
                "evidence_quote": "Main engine crankcase oil detector fail alarm",
            },
        }
    ],
    "problems": [],
    "faults": [
        {
            "fault_id": "f1",
            "symptom": "Main engine crankcase oil detector fail alarm for unit no:2",
            "alarms": ["OMD fail"],
            "equipment_id": "e1",
            "position": "unit 2",
            "problem_id": None,
            "suspected_cause": "photodiode fault",
            "first_observed_at": "2026-06-20",
            "recurrence_of": None,
            "provenance": {
                "mail_ids": [MAIL_ID],
                "attachment_shas": [],
                "evidence_quote": "crankcase oil detector fail alarm for unit no:2 activated",
            },
        }
    ],
    "action_logs": [
        {
            "action_id": "a1",
            "fault_id": "f1",
            "also_fault_ids": [],
            "seq": 1,
            "occurrence_no": 1,
            "target": "unit 2",
            "action": "Renewed the OMD sensor with the spare",
            "kind": "corrective",
            "performed_by_role": "vessel_crew",
            "advised_by_org": None,
            "parts": ["OMD sensor"],
            "date_performed": "2026-06-21",
            "asserted_in_mail": MAIL_ID,
            "date_reported": "2026-06-21T05:56:05Z",
            "outcome": {
                "value": "resolved",
                "verification": "confirmed_by_vessel",
                "outcome_in_mail": MAIL_ID_2,
                "outcome_date": "2026-06-22",
                "evidence_quote": "no further alarm observed",
            },
            "finding": None,
            "provenance": {
                "mail_ids": [MAIL_ID, MAIL_ID_2],
                "attachment_shas": [SHA],
                "evidence_quote": "Vessel renewed the sensor with spare one",
            },
        },
        {
            "action_id": "a2",
            "fault_id": "f1",
            "also_fault_ids": [],
            "seq": 2,
            "occurrence_no": 1,
            "target": None,
            "action": "Raised requisition for a new OMD",
            "kind": "commercial",
            "performed_by_role": "vessel_crew",
            "advised_by_org": None,
            "parts": [],
            "date_performed": None,
            "asserted_in_mail": MAIL_ID,
            "date_reported": "2026-06-21T05:56:05Z",
            "outcome": {
                "value": "unknown",
                "verification": "none",
                "outcome_in_mail": None,
                "outcome_date": None,
                "evidence_quote": None,
            },
            "finding": None,
            "provenance": {
                "mail_ids": [MAIL_ID],
                "attachment_shas": [],
                "evidence_quote": "this quote is not in the mail",
            },
        },
    ],
    "validation_flags": ["quote_not_found:a2"],
    "needs_review": True,
}

MAIL_TEXT = (
    "From: Chief Engineer cheng@vessel.example\n\nSubject: Main engine OMD fault rectified\n\n"
    "Date: 2026-06-21 05:56:05\n\nTo: super@office.example\n\n"
    "Kindly note, Main engine crankcase oil detector fail alarm for unit no:2 activated in AMS on 20/06/2026.\n"
    "OMD control Panel displays photodiode fault.\n"
    "Vessel renewed the sensor with spare one, tried out and found to be working satisfactory.\n"
)
MAIL_TEXT_2 = "From: CE\n\nSubject: RE: OMD\n\nDate: 2026-06-22 08:00:00\n\nOMD monitored for 48 hrs, no further alarm observed.\n"

DIGEST = {
    "sha256": SHA,
    "links": [
        {
            "mailbodyId": MAIL_ID,
            "casefileId": RECORD["casefile_id"],
            "attachment_url": "u",
            "name": "omd.pdf",
        }
    ],
    "content_bearing": True,
    "kind_of_doc": "Service report",
    "summary": "OMD sensor report.",
    "tags": ["report"],
    "maker": "NA",
    "model": "NA",
    "equipment_details": "NA",
    "extracted_text": "[page 1]\nOMD sensor renewed.",
    "extraction_method": "pdf_text",
    "audit": {"name": "omd.pdf", "bytes": 1234, "page_count": 1},
}

WINDOWS = [
    {
        "index": 0,
        "pagenums": [0, 1],
        "mail_ids": [MAIL_ID, MAIL_ID_2],
        "attachment_shas": [SHA],
        "attempts": 1,
        "rejected": False,
        "error": None,
        "seconds": 3.2,
        "prompt_chars": 1200,
        "raw_response": {
            "add": {},
            "update": {
                "action_logs": [
                    {
                        "action_id": "a1",
                        "outcome": {"value": "resolved"},
                        "provenance_add": {"mail_ids": [MAIL_ID_2]},
                    }
                ]
            },
            "window_notes": "sensor renewed and confirmed",
        },
        "applied": {
            "added": {"equipment": ["e1"], "faults": ["f1"], "action_logs": ["a1", "a2"]},
            "updated": {"action_logs": ["a1"]},
            "flags": [],
            "dropped_rows": [],
        },
    }
]
RUN_LOG = {
    "runs": [
        {
            "started_at": "2026-10-09T21:14:07+00:00",
            "finished_at": "2026-10-09T21:15:07+00:00",
            "extraction_version": RECORD["extraction_version"],
            "windows": [
                {
                    "index": 0,
                    "pagenums": [0, 1],
                    "added": {},
                    "updated": {},
                    "flags": [],
                    "seconds": 3.2,
                }
            ],
            "tokens": {"total_tokens": 1000, "call_count": 1},
            "attachments": {"links": 1, "cache_hits": 1, "fetched": 0, "digest_calls": 0},
        }
    ],
    "consolidate": {
        "called": True,
        "merged": [],
        "rejected_merges": [],
        "repeat_groups": [],
        "fanout_collapsed": [],
        "fault_pairs": {"candidates": []},
        "notes": "",
        "tokens": {"total_tokens": 100},
    },
}


def _source():
    return DictRecordSource(
        {RECORD["casefile_id"]: RECORD},
        windows={RECORD["casefile_id"]: WINDOWS},
        run_logs={RECORD["casefile_id"]: RUN_LOG},
        mails={
            MAIL_ID: {"mail_id": MAIL_ID, "text": MAIL_TEXT, "url": "https://example.invalid/m1"},
            MAIL_ID_2: {"mail_id": MAIL_ID_2, "text": MAIL_TEXT_2},
        },
        attachments={SHA: DIGEST},
    )


# ---- views


def test_normalise_symptom_strips_position_and_equipment_words():
    # A1 (13 §8): the key is the failure mode; the component leaves it; only the record's OWN
    # equipment words are stripped, so the equipment type must be passed
    assert (
        views.normalise_symptom(
            "Knocking sound from main engine exhaust valve cylinder no. 4.", "main_engine"
        )
        == "knocking sound"
    )
    assert views.normalise_symptom(
        "Knocking sound from M/E cylinder No.3", "main_engine"
    ) == views.normalise_symptom("Knocking sound from cylinder No.6")
    assert (
        views.normalise_symptom("Knocking sound from main engine") == "knocking sound from main engine"
    )  # no equipment type given: nothing generic is stripped any more
    assert views.normalise_symptom(None) == ""
    assert views.fault_symptom_key(RECORD, RECORD["faults"][0]) == "oil detector fail alarm"


def test_date_range_and_tally():
    assert views.date_range(RECORD) == ("2026-06-20", "2026-06-22")
    assert views.outcome_tally(RECORD) == [("resolved", 1), ("unknown", 1)]


def test_parse_flag_shapes():
    assert views.parse_flag("quote_not_found:a4")["row_id"] == "a4"
    f = views.parse_flag("quote_in_uncited_attachment:a19.outcome:0a7cc740")
    assert (f["row_id"], f["field"], f["extra"]) == ("a19", "outcome", "0a7cc740")
    assert views.parse_flag("over_cap:49>25")["extra"] == "49>25"


def test_split_mail_text_headers():
    hdr, body = views.split_mail_text(MAIL_TEXT)
    assert hdr["Subject"] == "Main engine OMD fault rectified"
    assert body.startswith("Kindly note")


def test_lookup_tables_groups_raw_names_by_type():
    lk = views.lookup_tables([RECORD])
    assert lk["equipment"][0]["type"] == "main_engine"
    assert lk["equipment"][0]["variants"][0]["name"] == "M/E"
    assert lk["parts"][0]["name"] == "OMD sensor"


def test_journey_and_lineage():
    j = views.journey(RECORD, WINDOWS, RUN_LOG)
    assert len(j["steps"]) == 1 and j["steps"][0]["has_detail"]
    assert j["lineage"]["a1"]["minted"] == 0
    assert j["lineage"]["a1"]["touched"][0]["fields"] == ["outcome"]
    assert j["lineage"]["a2"]["flags"][0]["kind"] == "quote_not_found"
    # summary fallback from run_log only
    j2 = views.journey(RECORD, [], RUN_LOG)
    assert len(j2["steps"]) == 1 and not j2["steps"][0]["has_detail"]


# ---- index


def test_project_fields_and_facets():
    d = project(RECORD)
    assert d["facets"]["equipment_type"] == ["main_engine"]
    assert d["facets"]["outcome"] == ["resolved", "unknown"]
    assert d["fields"]["symptoms"][0]["id"] == "f1"
    assert d["fields"]["actions"][0]["o"] == "resolved"
    assert d["years"] == ["2026"]
    idx = build_index([RECORD])
    assert idx["weights"]["title"] == 5 and len(idx["docs"]) == 1


# ---- highlighting


def test_highlight_body_marks_found_quotes_and_anchors_missing_ones():
    body = "alpha beta gamma delta"
    cites = [
        {"row_id": "a1", "quote": "beta gamma", "kind": "assertion"},
        {"row_id": "a1", "quote": "gamma", "kind": "outcome"},  # overlaps -> shares anchor
        {"row_id": "a2", "quote": "not here", "kind": "assertion"},
    ]
    html, out = highlight_body(body, cites)
    assert '<mark id="q-a1-0" class="assertion">beta gamma</mark>' in html
    assert out[1]["found"] and out[1]["anchor"] == "q-a1-0"
    assert not out[2]["found"] and out[2]["anchor"] == "side-q-a2-2" and out[2]["row_anchor"]
    assert '<span id="q-a1"></span>' in html


def test_highlight_handles_translated_quotes():
    html, out = highlight_body(
        "原文です and more",
        [{"row_id": "f1", "quote": "原文です [EN: original]", "kind": "assertion"}],
    )
    assert out[0]["found"] and "<mark" in html


# ---- the build


def test_build_writes_every_page_and_links_resolve(tmp_path: Path):
    out = tmp_path / "site"
    summary = build(_source(), out)
    assert summary["casefiles"] == 1 and summary["mails"] == 2 and summary["attachments"] == 1
    cid = RECORD["casefile_id"]
    for rel in (
        "index.html",
        "search_index.json",
        "assets/site.css",
        "assets/app.js",
        f"casefile/{cid}.html",
        f"mail/{MAIL_ID}.html",
        f"mail/{MAIL_ID_2}.html",
        f"attachment/{SHA}.html",
        "admin/lookup.html",
        "admin/review.html",
        f"admin/journey/{cid}.html",
    ):
        assert (out / rel).exists(), rel
    assert check_links(out) == []

    cf = (out / "casefile" / f"{cid}.html").read_text(encoding="utf-8")
    for anchor in ('id="e1"', 'id="f1"', 'id="a1"', 'id="a2"'):
        assert anchor in cf
    assert "UICategory" in cf and "quote_not_found" in cf
    # the assertion quote of a1 links to the mail where it was actually found (mail 1), the outcome to mail 2
    assert f"mail/{MAIL_ID}.html#q-a1" in cf and f"mail/{MAIL_ID_2}.html#q-a1" in cf

    mail = (out / "mail" / f"{MAIL_ID}.html").read_text(encoding="utf-8")
    assert "<mark" in mail and "not in body" in mail  # a2's quote is not verbatim here
    idx = json.loads((out / "search_index.json").read_text(encoding="utf-8"))
    assert idx["docs"][0]["id"] == cid
    index_html = (out / "index.html").read_text(encoding="utf-8")
    assert '<script id="idx" type="application/json">' in index_html


def _window_with_inputs() -> tuple[dict, str]:
    """A wNNN.json with the `inputs` audit block (docs/14) and its .prompt.txt text."""
    w = json.loads(json.dumps(WINDOWS[0]))
    w["inputs"] = {
        "prompt_version": "v5",
        "record_summary": {
            "equipment": [],
            "problems": [],
            "faults": [],
            "action_logs": [],
            "next_free_ids": {
                "equipment": "e1",
                "problems": "p1",
                "faults": "f1",
                "action_logs": "a1",
            },
        },
        "next_free_ids": {"equipment": "e1", "problems": "p1", "faults": "f1", "action_logs": "a1"},
        "mails": [
            {
                "mail_id": MAIL_ID,
                "pagenum": 0,
                "date": "2026-06-21T05:56:05Z",
                "sender": "Chief Engineer <cheng@vessel.example>",
                "to": "super@office.example",
                "subject": "Main engine OMD fault rectified",
                "context_only": False,
                "body_chars": 412,
                "body_sha1": "a" * 40,
                "fetched_now": True,
                "skipped_as_twin_of": None,
                "attachments": [
                    {
                        "url": "https://example.invalid/att/x/omd.pdf",
                        "name": "omd.pdf",
                        "sha256": SHA,
                        "status": "fetched_digested",
                        "content_bearing": True,
                        "read_method": "pdf_text",
                        "digested_now": True,
                        "error": None,
                        "sent_to_model": True,
                        "digest_as_sent": {"attachment_sha": SHA, "summary": "OMD sensor report."},
                    },
                    {
                        "url": "https://example.invalid/att/x/image001.png",
                        "name": "image001.png",
                        "sha256": "b" * 64,
                        "status": "junk_skipped",
                        "content_bearing": False,
                        "read_method": "skipped",
                        "digested_now": False,
                        "error": None,
                        "sent_to_model": False,
                        "digest_as_sent": None,
                    },
                ],
            },
            {
                "mail_id": MAIL_ID_2,
                "pagenum": 1,
                "date": "2026-06-22T08:00:00Z",
                "sender": "CE <cheng@vessel.example>",
                "to": None,
                "subject": "RE: OMD",
                "context_only": False,
                "body_chars": 90,
                "body_sha1": "c" * 40,
                "fetched_now": False,
                "skipped_as_twin_of": None,
                "attachments": [],
            },
            {
                "mail_id": "bbbbbbbbbbbbbbbbbbbbbbb9",
                "pagenum": 2,
                "date": "2026-06-22T08:00:30Z",
                "sender": "CE <cheng@vessel.example>",
                "to": None,
                "subject": "RE: OMD",
                "context_only": False,
                "body_chars": 90,
                "body_sha1": "c" * 40,
                "fetched_now": True,
                "skipped_as_twin_of": MAIL_ID_2,
                "attachments": [
                    {
                        "url": "https://example.invalid/att/y/dup.pdf",
                        "name": "dup.pdf",
                        "sha256": None,
                        "status": "not_processed",
                        "content_bearing": False,
                        "read_method": None,
                        "digested_now": False,
                        "error": None,
                        "sent_to_model": False,
                        "digest_as_sent": None,
                    }
                ],
            },
        ],
        "tokens": {
            "prompt_tokens": 4100,
            "completion_tokens": 1200,
            "total_tokens": 5300,
            "call_count": 2,
        },
        "attempts": [
            {
                "attempt": 1,
                "prompt_chars": 1200,
                "feedback": "",
                "raw_response": {"add": {}, "update": {}},
                "error": "update_unknown_id:a9",
            },
            {
                "attempt": 2,
                "prompt_chars": 1350,
                "feedback": "\n\n## CORRECTION\nYour previous response could not be applied: update_unknown_id:a9.",
                "raw_response": WINDOWS[0]["raw_response"],
                "error": None,
            },
        ],
    }
    w["attempts"] = 2
    prompt = (
        "## RECORD SO FAR (summary with ids)\n{}\n\n## WINDOW\n### MAIL mail_id="
        + MAIL_ID
        + " pagenum=0\nbody:\n<b>not html</b>\n"
    )
    return w, prompt


def test_journey_view_exposes_inputs_and_prompt():
    w, prompt = _window_with_inputs()
    j = views.journey(
        RECORD,
        [w],
        RUN_LOG,
        prompt_for=lambda i: prompt if i == 0 else None,
        mails_available={MAIL_ID},
        attachments_available={SHA},
    )
    st = j["steps"][0]
    inp = st["inputs"]
    assert inp["memory"] == {"equipment": 0, "problems": 0, "faults": 0, "action_logs": 0}
    assert inp["next_free_ids"]["action_logs"] == "a1"
    assert [m["has_page"] for m in inp["mails"]] == [True, False, False]
    assert inp["mails"][2]["skipped_as_twin_of"] == MAIL_ID_2
    atts = inp["mails"][0]["attachments"]
    assert atts[0]["has_page"] is True and atts[0]["digest_summary"] == "OMD sensor report."
    assert atts[1]["has_page"] is False
    assert (inp["n_mails_sent"], inp["n_attachments"], inp["n_attachments_sent"]) == (2, 3, 1)
    assert st["tokens"]["total_tokens"] == 5300 and len(inp["corrections"]) == 1
    assert st["prompt_text"] == prompt
    # the existing row content and the lineage are unchanged by the extra keys
    assert st["added"]["action_logs"] == ["a1", "a2"] and j["lineage"]["a1"]["minted"] == 0
    # an audit file without `inputs` gets None, and no prompt is looked up for it
    old = views.journey(RECORD, WINDOWS, RUN_LOG, prompt_for=lambda i: prompt)
    assert old["steps"][0]["inputs"] is None and old["steps"][0]["prompt_text"] is None
    # summary-only steps too
    assert views.journey(RECORD, [], RUN_LOG)["steps"][0]["inputs"] is None


def test_journey_page_shows_what_the_model_saw(tmp_path: Path):
    w, prompt = _window_with_inputs()
    cid = RECORD["casefile_id"]
    src = DictRecordSource(
        {cid: RECORD},
        windows={cid: [w]},
        run_logs={cid: RUN_LOG},
        mails={
            MAIL_ID: {"mail_id": MAIL_ID, "text": MAIL_TEXT},
            MAIL_ID_2: {"mail_id": MAIL_ID_2, "text": MAIL_TEXT_2},
        },
        attachments={SHA: DIGEST},
        prompts={cid: {0: prompt}},
    )
    assert src.windows_prompt(cid, 0) == prompt and src.windows_prompt(cid, 1) is None
    out = tmp_path / "site"
    build(src, out)
    assert check_links(out) == []
    page = (out / "admin" / "journey" / f"{cid}.html").read_text(encoding="utf-8")
    assert '<details class="saw"><summary>what the model saw</summary>' in page
    assert "e 0 · p 0 · f 0 · a 0" in page and "e1 p1 f1 a1" in page
    # mails table: pages, twin greyed with its twin id, context/fetched tags, mail link only when the page exists
    assert 'class="twin"' in page and "twin of " + MAIL_ID_2[-6:] in page
    assert "fetched now" in page and f'href="../../mail/{MAIL_ID}.html"' in page
    assert "bbbbbbbbbbbbbbbbbbbbbbb9.html" not in page
    # attachments table: status chips, sha link, hover summary, sent mark
    assert "fetched + digested" in page and ">junk<" in page and "not processed" in page
    assert f'href="../../attachment/{SHA}.html"' in page and 'title="OMD sensor report."' in page
    assert "pdf_text" in page
    # tokens, attempts, correction, raw prompt and per-attempt raw response, escaped
    assert "4,100 in / 1,200 out" in page or "4100 in / 1200 out" in page
    assert (
        "2 attempts" in page
        and "correction sent before attempt 2" in page
        and "## CORRECTION" in page
    )
    assert "raw prompt (attempt 1" in page and "&lt;b&gt;not html&lt;/b&gt;" in page
    assert "raw response · attempt 1" in page and "raw response · attempt 2" in page
    assert "update_unknown_id:a9" in page
    # the casefile page did not change
    cf = (out / "casefile" / f"{cid}.html").read_text(encoding="utf-8")
    assert "what the model saw" not in cf and "saw-pre" not in cf


def test_journey_page_without_inputs_shows_fallback_line(tmp_path: Path):
    out = tmp_path / "site"
    build(_source(), out)  # WINDOWS[0] has no `inputs`
    page = (out / "admin" / "journey" / f"{RECORD['casefile_id']}.html").read_text(encoding="utf-8")
    assert "what the model saw" in page
    assert (
        "inputs not captured — audit written by an earlier extraction run; re-run to get them"
        in page
    )
    assert "saw-mails" not in page
    assert check_links(out) == []


# ---- canonical facets (12 §3): ids only, bounded by the family list


def _lookup_for(records: list[dict], tmp_path: Path):
    """An in-memory resolver fed with the records, as scripts/build_lookup.py would."""
    from src.resolution.lookup import LookupStore

    fam = tmp_path / "fam.csv"
    fam.write_text(
        "key,display,description,patterns,sort_order\n# proposal\n"
        "alarm_or_trip,Alarm / trip,,alarm|trip,1\nabnormal_noise,Abnormal noise,,knock|noise,2\n",
        encoding="utf-8",
    )
    s = LookupStore()
    s.import_families_csv(fam)
    for r in records:
        for e in r.get("equipment", []):
            s.resolve(e.get("name"), kind="equipment")
        for f in r.get("faults", []):
            s.resolve(f.get("symptom"), kind="symptom", context=views.fault_context(r, f))
    s.assign_families("symptom")
    return s


def test_index_facets_are_ids_not_wording(tmp_path: Path):
    from src.search.index import PARENTS, facets_values

    rec2 = json.loads(json.dumps(RECORD))
    rec2["casefile_id"] = "d" * 24
    rec2["faults"][0]["symptom"] = "Knocking sound from main engine exhaust valve"
    rec2["equipment"][0]["name"] = "Main engine"
    rec3 = json.loads(json.dumps(RECORD))
    rec3["casefile_id"] = "e" * 24
    rec3["faults"][0]["symptom"] = (
        "Coating thickness below 300 microns"  # no family, and we will not feed it
    )
    records = [RECORD, rec2]
    lookup = _lookup_for(records, tmp_path)
    idx = build_index(records + [rec3], None, lookup)
    assert idx["schema"] == "site_search_index.v2" and idx["canonical"] is True
    docs = {d["id"]: d for d in idx["docs"]}
    f1, f2, f3 = (
        docs[RECORD["casefile_id"]]["facets"],
        docs[rec2["casefile_id"]]["facets"],
        docs[rec3["casefile_id"]]["facets"],
    )
    # no wording anywhere in the id facets; every value is an id, a family key or a pseudo value
    for d in idx["docs"]:
        for k in ("equipment_std", "symptom", "symptom_broad"):
            assert all(v.isdigit() or v == "__unresolved__" for v in d["facets"][k]), (
                k,
                d["facets"][k],
            )
        assert "equipment_name" not in d["facets"]
    assert f1["symptom_family"] == ["alarm_or_trip"] and f2["symptom_family"] == ["abnormal_noise"]
    assert f3["symptom"] == ["__unresolved__"] and f3["symptom_family"] == [
        "unclassified"
    ]  # not fed: not minted
    assert f1["equipment_std"] == f2["equipment_std"]  # "M/E" and "Main engine" are one standard
    # labels cover every id, and every child value maps to its parent
    for k in ("equipment_std", "symptom", "symptom_family"):
        for v in facets_values(idx, k):
            assert v in idx["labels"][k], (k, v)
    assert idx["labels"]["equipment_std"][f1["equipment_std"][0]] == "Main engine"
    assert idx["labels"]["symptom_family"]["abnormal_noise"] == "Abnormal noise"
    assert (
        idx["parents"]
        == PARENTS
        == {
            "equipment_std": "equipment_type",
            "symptom": "symptom_family",
            "symptom_on": "symptom_family",
        }
    )
    for child, parent in PARENTS.items():
        for v in facets_values(idx, child):
            parents = idx["facet_parent_vals"][child][v]
            assert parents and set(parents) <= set(facets_values(idx, parent)), (child, v, parents)
    assert idx["facet_parent_vals"]["equipment_std"][f1["equipment_std"][0]] == ["main_engine"]
    assert idx["facet_parent_vals"]["symptom"][f1["symptom"][0]] == ["alarm_or_trip"]
    # ON / WHERE (13 §8): the component of "Knocking sound from main engine exhaust valve" is
    # the part standard "exhaust valve" (an id, nested under the fault's family), the record
    # with no component or location has no value; a Symptom click lists this, not the standards
    (pid,) = f2["symptom_on"]
    assert pid.isdigit() and idx["labels"]["symptom_on"][pid] == "exhaust valve"
    assert idx["facet_parent_vals"]["symptom_on"][pid] == ["abnormal_noise"]
    # a location (WHERE) is the location-table term, labelled "in <location>"
    assert f1["symptom_on"] == ["crankcase"]
    assert idx["labels"]["symptom_on"]["crankcase"] == "in crankcase"
    assert idx["facet_parent_vals"]["symptom_on"]["crankcase"] == ["alarm_or_trip"]
    assert f3["symptom_on"] == []
    # the principle: a facet is bounded by the family list, never by the corpus
    assert (
        len(facets_values(idx, "symptom_family")) <= len(lookup.families("symptom")) + 1
    )  # + unclassified
    assert len([v for v in facets_values(idx, "symptom_family") if v != "unclassified"]) <= len(
        lookup.families("symptom")
    )
    # the wording is still searchable
    assert (
        docs[rec2["casefile_id"]]["fields"]["symptoms"][0]["t"]
        == "Knocking sound from main engine exhaust valve"
    )
    # nothing was minted by the build
    assert lookup.find("Coating thickness below 300 microns", kind="symptom") is None


def test_symptom_broad_uses_ids_with_links(tmp_path: Path):
    from src.site.symptom_links import SymptomLinks

    rec_b = json.loads(json.dumps(RECORD))
    rec_b["casefile_id"] = "f" * 24
    rec_b["faults"][0]["symptom"] = "Knocking sound from M/E cylinder No.3"
    rec_n = json.loads(json.dumps(RECORD))
    rec_n["casefile_id"] = "g" * 24
    # a narrower wording that survives decomposition (no table term in it); "knocking sound from
    # exhaust valve" dissolves into knocking sound + component since A1 (13 §8)
    rec_n["faults"][0]["symptom"] = "Knocking sound at high rpm"
    lookup = _lookup_for([rec_b, rec_n], tmp_path)
    links = SymptomLinks([{"broader": "knocking sound", "narrower": "knocking sound at high rpm"}])
    idx = build_index([rec_b, rec_n], links, lookup)
    docs = {d["id"]: d["facets"] for d in idx["docs"]}
    broad_id = docs[rec_b["casefile_id"]]["symptom"][0]
    narrow_id = docs[rec_n["casefile_id"]]["symptom"][0]
    assert broad_id != narrow_id
    assert docs[rec_n["casefile_id"]]["symptom_broad"] == sorted(
        [broad_id, narrow_id]
    )  # a click on the broader id reaches it
    assert docs[rec_b["casefile_id"]]["symptom_broad"] == [
        broad_id
    ]  # a click on the narrower id does not
    assert idx["facet_match"] == {"symptom": "symptom_broad"}


def test_search_page_with_and_without_lookup(tmp_path: Path):
    lookup = _lookup_for([RECORD], tmp_path)
    out = tmp_path / "with"
    build(_source(), out, lookup=lookup)
    assert check_links(out) == []
    html_ = (out / "index.html").read_text(encoding="utf-8")
    assert "canonical facets unavailable" not in html_
    idx = json.loads((out / "search_index.json").read_text(encoding="utf-8"))
    assert idx["canonical"] is True and "labels" in idx
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    # one merged "needs attention" queue: the equipment and the symptom standard are unreviewed,
    # the symptom was classified by rule so nobody is without a family
    assert "Needs attention · 2 unreviewed · 0 without family" in page
    assert '<details class="queue" open>' in page  # <= 10 rows: open by default
    assert "Read from the <strong>resolver</strong>" in page and "Alarm / trip" in page
    assert (
        'data-kind="symptom" data-fix="symptom,Main engine crankcase oil detector fail alarm for unit no:2,oil detector fail alarm,&lt;family_key&gt;"'
        in page
    )  # the key is the failure mode: "crankcase" is the location, "unit no:2" the position
    assert (
        '<td class="variants on"><span>in crankcase <span class="n">(1)</span></span></td>' in page
    )  # the ON column: the location the wording named; no component
    assert page.count('class="copyfix') == 2 and "assets/app.js" in page

    out2 = tmp_path / "without"
    build(_source(), out2)  # no lookup: wording facets + banner
    assert check_links(out2) == []
    html2 = (out2 / "index.html").read_text(encoding="utf-8")
    assert "canonical facets unavailable" in html2 and "scripts/build_lookup.py" in html2
    idx2 = json.loads((out2 / "search_index.json").read_text(encoding="utf-8"))
    assert idx2["canonical"] is False and idx2["schema"] == "site_search_index.v1"
    assert idx2["docs"][0]["facets"]["symptom"] == ["oil detector fail alarm"]
    page2 = (out2 / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "resolver DB was not available" in page2


def test_needs_attention_queue_is_one_merged_ranked_list(tmp_path: Path):
    from src.resolution.lookup import LookupStore

    fam = tmp_path / "fam.csv"
    fam.write_text(
        "key,display,description,patterns,sort_order\nleakage,Leakage,,leak,1\n", encoding="utf-8"
    )
    s = LookupStore()
    s.import_families_csv(fam)
    # 12 part names (one hit each) + a symptom seen 3 times with no family + a leak symptom (rule) + an approved one
    for i in range(12):
        s.resolve(f"spare widget {chr(97 + i)}", kind="part")
    for _ in range(3):
        s.resolve("Vibration at the thrust bearing", kind="symptom")
    s.resolve("Fuel leak at flange", kind="symptom")
    approved = s.resolve("Main engine", kind="equipment")
    s.approve(approved.standard_id)
    s.assign_families("symptom")

    r = views.resolver_tables(s, [RECORD])
    q = r["queue"]
    # one entry per standard; the vibration standard is both unreviewed and family-less -> one row, two reasons
    vib = [x for x in q if x["key"] == "vibration at the thrust bearing"]
    assert len(vib) == 1 and vib[0]["reasons"] == ["unreviewed", "no_family"]
    assert (
        len(q) == len({x["id"] for x in q}) == 14
    )  # 12 parts + 2 symptoms; the approved equipment is not listed
    # hits desc, then symptom before part, then key
    assert q[0]["key"] == "vibration at the thrust bearing" and q[0]["hits"] == 3
    assert [x["hits"] for x in q] == sorted([x["hits"] for x in q], reverse=True)
    assert q[1]["kind"] == "symptom" and q[2]["kind"] == "part"
    assert [x["key"] for x in q[2:5]] == ["widget a", "widget b", "widget c"]  # "spare" is a noise word
    # counts agree with fragmentation()
    frag = s.fragmentation()
    assert r["queue_counts"] == {
        "unreviewed": frag["auto"],
        "no_family": frag["by_kind"]["symptom"]["unclassified"],
        "total": 14,
    }
    assert r["queue_counts"] == {"unreviewed": 14, "no_family": 1, "total": 14}
    assert r["queue_by_kind"] == {"equipment": 0, "part": 12, "symptom": 2}
    assert (
        vib[0]["fix_row"]
        == "symptom,Vibration at the thrust bearing,vibration at the thrust bearing,<family_key>"
    )
    assert q[2]["fix_row"] == "part,spare widget a,widget a,"

    out = tmp_path / "site"
    build(_source(), out, lookup=s)
    assert check_links(out) == []
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "Needs attention · 14 unreviewed · 1 without family" in page
    assert '<details class="queue">' in page  # > 10 rows: collapsed by default
    rows = [ln for ln in page.splitlines() if ln.strip().startswith("<tr data-kind=")]
    assert len(rows) == 14
    assert "hidden" not in rows[9] and rows[10].rstrip().endswith(
        "hidden>"
    )  # the 11th row is hidden
    assert (
        'data-fix="symptom,Vibration at the thrust bearing,vibration at the thrust bearing,&lt;family_key&gt;"'
        in rows[0]
    )
    assert rows[0].count("chip warn") == 0  # chips are on the row's cells, not the <tr>
    first_row_html = page.split(rows[0], 1)[1].split("</tr>", 1)[0]
    assert (
        '<span class="chip warn">unreviewed</span><span class="chip flag">no family</span>'
        in first_row_html
    )
    assert "showing <span data-shown>10</span> of 14" in page and 'data-action="show-all"' in page
    assert (
        'data-kind="part" aria-pressed="true" title="show / hide part rows">part 12</button>'
        in page
    )
    assert "Review queues" not in page and "auto-minted standards" not in page


def test_build_without_admin_and_with_missing_caches(tmp_path: Path):
    src = DictRecordSource({RECORD["casefile_id"]: RECORD})  # no mails, no digests, no windows
    summary = build(src, tmp_path / "site", admin=False)
    assert summary["mails"] == 0 and summary["mails_cited_not_cached"] == 2
    assert not (tmp_path / "site" / "admin").exists()
    assert check_links(tmp_path / "site") == []
    cf = (tmp_path / "site" / "casefile" / f"{RECORD['casefile_id']}.html").read_text(
        encoding="utf-8"
    )
    assert "not cached" in cf


def test_packaged_name_matches_package_records_rule():
    assert (
        packaged_name(RECORD) == "TEST VESSEL__Main engine OMD fault__cccccccccccccccccccccccc.json"
    )
