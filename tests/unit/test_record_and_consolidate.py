"""
The carried record: header, delta application (ids, evidence, coercion,
updates, dropped-object guard) and consolidate's deterministic part.
"""

import copy

import pytest

from src.extraction.consolidate import apply_consolidation, assign_seq
from src.extraction.record import (
    DeltaError,
    WindowContext,
    apply_delta,
    assert_no_dropped,
    ids_of,
    new_record,
    next_id,
    strip_sidecars,
    summary_for_prompt,
)

M1, M2, M3 = "bbbbbbbbbbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbbbbbbbbbb2", "bbbbbbbbbbbbbbbbbbbbbbb3"
SHA = "a" * 64


def ctx(*mail_ids, shas=()):
    return WindowContext(
        mail_ids=set(mail_ids),
        mail_dates={M1: "2026-06-21T00:26:05Z", M2: "2026-06-21T05:13:47Z", M3: "2026-06-22T08:00:00Z"},
        shas=set(shas),
    )


def prov(*mail_ids, quote="alarm still persists", shas=()):
    return {"mail_ids": list(mail_ids), "attachment_shas": list(shas), "evidence_quote": quote}


def delta_first_window():
    return {
        "add": {
            "equipment": [{"equipment_id": "e1", "name": "Main engine OMD", "equipment_type": "oil_mist_detector", "maker": "NA", "model": "NA", "provenance": prov(M1)}],
            "problems": [],
            "faults": [{"fault_id": "f1", "symptom": "OMD fail alarm unit 2, photodiode fault", "equipment_id": "e1", "problem_id": None, "first_observed_at": "2026-06-20", "provenance": prov(M1)}],
            "action_logs": [
                {"action_id": "a1", "fault_id": "f1", "action": "terminal tightness checked, sensor cleaned", "kind": "diagnostic", "performed_by_role": "vessel_crew", "asserted_in_mail": M1,
                 "outcome": {"value": "inconclusive", "verification": "reported_only", "outcome_in_mail": M1, "outcome_date": None, "evidence_quote": "alarm still persists"}, "provenance": prov(M1, quote="sensor cleaned")},
                {"action_id": "a2", "fault_id": "f1", "action": "sensor renewed with spare", "kind": "corrective", "performed_by_role": "vessel_crew", "asserted_in_mail": M1,
                 "outcome": {"value": "unknown", "verification": "none", "outcome_in_mail": None, "outcome_date": None, "evidence_quote": None}, "provenance": prov(M1, quote="renewed the sensor")},
            ],
        },
        "update": {},
        "window_notes": "",
    }


def test_new_record_header(mini_casefile):
    r = new_record(mini_casefile)
    assert r["casefile_id"] == "aaaaaaaaaaaaaaaaaaaaaaa1"
    assert r["problem_statement"] == mini_casefile.title
    assert r["progress"] == {"last_pagenum_processed": -1, "complete": False}
    assert r["export_meta"]["tags"] == ["defect", "maintenance"]
    assert next_id(r, "faults") == "f1"


def test_apply_delta_adds_rows_and_fills_dates(mini_casefile):
    r = new_record(mini_casefile)
    applied = apply_delta(r, delta_first_window(), ctx(M1))
    assert applied.added["equipment"] == ["e1"] and applied.added["faults"] == ["f1"] and applied.added["action_logs"] == ["a1", "a2"]
    a1, a2 = r["action_logs"]
    assert a1["date_reported"] == "2026-06-21T00:26:05Z"  # from the page, not the model
    assert a1["outcome"]["outcome_date"] == "2026-06-21"  # defaulted to the outcome mail's date
    assert a1["seq"] == 1 and a2["seq"] == 2 and a2["occurrence_no"] == 1
    assert a2["outcome"]["value"] == "unknown" and a2["outcome"]["verification"] == "none"
    assert next_id(r, "action_logs") == "a3"
    assert applied.flags == []


def test_ids_must_continue_sequence(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    before = copy.deepcopy(r)
    bad = {"add": {"faults": [{"fault_id": "f1", "symptom": "x", "equipment_id": "e1", "provenance": prov(M2)}]}, "update": {}}
    with pytest.raises(DeltaError, match="already exists"):
        apply_delta(r, bad, ctx(M2))
    assert r == before  # nothing applied on error
    bad = {"add": {"faults": [{"fault_id": "f3", "symptom": "x", "equipment_id": "e1", "provenance": prov(M2)}]}, "update": {}}
    with pytest.raises(DeltaError, match="skips the sequence"):
        apply_delta(r, bad, ctx(M2))


def test_unresolved_reference_is_a_delta_error(mini_casefile):
    r = new_record(mini_casefile)
    bad = {"add": {"action_logs": [{"action_id": "a1", "fault_id": "f9", "action": "x", "kind": "corrective", "performed_by_role": "vessel_crew", "asserted_in_mail": M1, "outcome": {"value": "unknown"}, "provenance": prov(M1)}]}, "update": {}}
    with pytest.raises(DeltaError, match="fault_id 'f9'"):
        apply_delta(r, bad, ctx(M1))


def test_row_without_evidence_is_dropped_and_ids_renumbered(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    # e1 fine; add a second equipment with no quote -> dropped; the fault points at it -> dropped too.
    d["add"]["equipment"].append({"equipment_id": "e2", "name": "Purifier", "equipment_type": "purifier", "provenance": {"mail_ids": [M1], "evidence_quote": ""}})
    d["add"]["faults"].append({"fault_id": "f2", "symptom": "leak", "equipment_id": "e2", "provenance": prov(M1)})
    d["add"]["faults"].append({"fault_id": "f3", "symptom": "noise", "equipment_id": "e1", "provenance": prov(M1)})
    applied = apply_delta(r, d, ctx(M1))
    assert "e2" in applied.dropped_rows and "f2" in applied.dropped_rows
    assert any(f.startswith("row_without_evidence:e2") for f in applied.flags)
    # f3 became f2 so the sequence has no hole
    assert [f["fault_id"] for f in r["faults"]] == ["f1", "f2"]
    assert any(f.startswith("ids_renumbered_after_drop") for f in applied.flags)


def test_out_of_window_citations_are_dropped_with_flags(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["faults"][0]["provenance"]["mail_ids"] = [M1, M3]
    d["add"]["faults"][0]["provenance"]["attachment_shas"] = ["b" * 64]
    applied = apply_delta(r, d, ctx(M1))
    assert r["faults"][0]["provenance"]["mail_ids"] == [M1]
    assert r["faults"][0]["provenance"]["attachment_shas"] == []
    assert any(f.startswith("mail_id_out_of_window:f1") for f in applied.flags)
    assert any(f.startswith("sha_out_of_window:f1") for f in applied.flags)


def test_outcome_without_quote_is_downgraded_and_bad_enums_fall_back(mini_casefile):
    r = new_record(mini_casefile)
    d = delta_first_window()
    d["add"]["action_logs"][0]["outcome"] = {"value": "resolved", "verification": "confirmed_by_vessel", "outcome_in_mail": M1, "evidence_quote": None}
    d["add"]["action_logs"][0]["kind"] = "repair"  # not an enum value
    d["add"]["action_logs"][0]["date_performed"] = "20 June 2026"
    applied = apply_delta(r, d, ctx(M1))
    a1 = r["action_logs"][0]
    assert a1["outcome"]["value"] == "unknown"
    assert a1["kind"] == "corrective" and a1["date_performed"] is None
    assert any(f.startswith("outcome_downgraded:a1") for f in applied.flags)
    assert any(f.startswith("bad_enum:a1.kind") for f in applied.flags)
    assert any(f.startswith("bad_date:a1.date_performed") for f in applied.flags)


def test_late_outcome_binding_updates_existing_action(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    before = ids_of(r)
    d = {
        "add": {},
        "update": {
            "action_logs": [{"action_id": "a2", "outcome": {"value": "resolved", "verification": "confirmed_by_vessel", "outcome_in_mail": M3, "outcome_date": "2026-06-22", "evidence_quote": "no further alarm observed"}, "date_performed": "2026-06-21",
                             "provenance_add": {"mail_ids": [M3], "evidence_quote": "no further alarm observed"}}],
            "faults": [{"fault_id": "f1", "suspected_cause": "photodiode failure", "provenance_add": {"mail_ids": [M3]}}],
            "equipment": [{"equipment_id": "e1", "maker": "Schaller", "model": "Visatron VN215", "provenance_add": {"mail_ids": [M3], "attachment_shas": [SHA], "evidence_quote": "Visatron VN215"}}],
        },
    }
    applied = apply_delta(r, d, ctx(M2, M3, shas=[SHA]))
    assert applied.updated["action_logs"] == ["a2"] and applied.updated["faults"] == ["f1"] and applied.updated["equipment"] == ["e1"]
    a2 = r["action_logs"][1]
    assert a2["outcome"]["value"] == "resolved" and a2["outcome"]["outcome_in_mail"] == M3
    assert a2["date_performed"] == "2026-06-21"
    assert a2["provenance"]["mail_ids"] == [M1, M3]
    # The update's attachment and quote are their own supporting entry; the primary quote keeps its own sources.
    e1p = r["equipment"][0]["provenance"]
    assert r["equipment"][0]["maker"] == "Schaller" and e1p["attachment_shas"] == []
    assert e1p["supporting"] == [{"mail_ids": [M3], "attachment_shas": [SHA], "evidence_quote": "Visatron VN215", "fields": ["maker", "model"]}]
    assert len(r["action_logs"]) == 2  # no new row
    assert assert_no_dropped(before, r) == []
    stored = strip_sidecars(r)
    assert stored["action_logs"][1]["provenance"]["supporting"] == [
        {"mail_ids": [M3], "attachment_shas": [], "evidence_quote": "no further alarm observed", "fields": ["date_performed", "outcome"]}
    ]
    assert "update_quotes" not in stored["action_logs"][1]["provenance"]


def test_summary_for_prompt_has_ids_and_next_free_no_provenance(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    s = summary_for_prompt(r)
    assert s["next_free_ids"] == {"equipment": "e2", "problems": "p1", "faults": "f2", "action_logs": "a3"}
    assert s["faults"][0] == {"fault_id": "f1", "symptom": "OMD fail alarm unit 2, photodiode fault", "equipment_id": "e1",
                              "position": None, "problem_id": None, "suspected_cause": None, "first_observed_at": "2026-06-20",
                              "recurrence_of": None}
    assert s["action_logs"][0]["outcome"] == "inconclusive" and s["action_logs"][0]["date_reported"] == "2026-06-21"
    assert all("provenance" not in row for coll in ("equipment", "faults", "action_logs") for row in s[coll])


def test_update_unknown_id_is_flagged_not_fatal(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    applied = apply_delta(r, {"add": {}, "update": {"action_logs": [{"action_id": "a9", "target": "x"}]}}, ctx(M2))
    assert "update_unknown_id:a9" in applied.flags


def test_malformed_delta_raises(mini_casefile):
    r = new_record(mini_casefile)
    with pytest.raises(DeltaError):
        apply_delta(r, ["not", "an", "object"], ctx(M1))
    with pytest.raises(DeltaError):
        apply_delta(r, {"add": {"faults": "nope"}, "update": {}}, ctx(M1))


def test_assert_no_dropped_detects_removal(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    before = ids_of(r)
    r["action_logs"].pop()
    assert assert_no_dropped(before, r) == ["dropped_object:a2"]


# --- consolidate (deterministic part) ---------------------------------------

def _record_with_actions(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    d = {"add": {"action_logs": [
        {"action_id": "a3", "fault_id": "f1", "action": "renewed the OMD sensor", "kind": "corrective", "performed_by_role": "vessel_crew", "asserted_in_mail": M2, "date_performed": "2026-06-20",
         "outcome": {"value": "unknown"}, "provenance": prov(M2, quote="monitor the OMD")},
        {"action_id": "a4", "fault_id": "f1", "action": "sensor cleaned again", "kind": "diagnostic", "performed_by_role": "vessel_crew", "asserted_in_mail": M3, "date_performed": "2026-06-22",
         "outcome": {"value": "unknown"}, "provenance": prov(M3, quote="no further alarm")},
    ]}, "update": {}}
    apply_delta(r, d, ctx(M2, M3))
    return r


def test_assign_seq_orders_by_date_performed_then_reported(mini_casefile):
    r = _record_with_actions(mini_casefile)
    assign_seq(r)
    order = [(a["action_id"], a["seq"]) for a in r["action_logs"]]
    # a3 performed 2026-06-20 comes first; a1/a2 reported 2026-06-21; a4 performed 2026-06-22.
    assert order == [("a3", 1), ("a1", 2), ("a2", 3), ("a4", 4)]


def test_apply_consolidation_merges_duplicates_and_numbers_repeats(mini_casefile):
    r = _record_with_actions(mini_casefile)
    proposal = {
        "merge": [
            {"keep": "a2", "drop": "a3", "reason": "same renewal"},
            {"keep": "a1", "drop": "a2", "reason": "different kind -> rejected"},
            {"keep": "a1", "drop": "zz", "reason": "unknown id"},
        ],
        "repeat_groups": [{"action_ids": ["a1", "a4"], "reason": "cleaned twice"}],
        "notes": "ok",
    }
    log = apply_consolidation(r, proposal)
    ids = [a["action_id"] for a in r["action_logs"]]
    assert "a3" not in ids and set(ids) == {"a1", "a2", "a4"}
    a2 = next(a for a in r["action_logs"] if a["action_id"] == "a2")
    assert a2["date_performed"] == "2026-06-20" and M2 in a2["provenance"]["mail_ids"]  # folded from a3
    assert [m["drop"] for m in log["merged"]] == ["a3"]
    assert {m["why"] for m in log["rejected_merges"]} == {"different kind", "unknown id"}
    a1 = next(a for a in r["action_logs"] if a["action_id"] == "a1")
    a4 = next(a for a in r["action_logs"] if a["action_id"] == "a4")
    assert a1["occurrence_no"] == 1 and a4["occurrence_no"] == 2
    assert [a["seq"] for a in r["action_logs"]] == [1, 2, 3]


def test_apply_consolidation_tolerates_garbage(mini_casefile):
    r = _record_with_actions(mini_casefile)
    n = len(r["action_logs"])
    log = apply_consolidation(r, "not json object")
    assert len(r["action_logs"]) == n and log["merged"] == []
    assert [a["seq"] for a in r["action_logs"]] == [1, 2, 3, 4]
