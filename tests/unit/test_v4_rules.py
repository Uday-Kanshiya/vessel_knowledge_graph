"""
v4 rules enforced in code: recurrence as a new fault row, fault_id correction
in updates, the date guard on duplicate merges, and citation repair.
"""

from src.contract.validator import (
    apply_validation,
    repair_citations,
    schema_errors,
    validate_record,
)
from src.extraction.consolidate import apply_consolidation, merge_blocked_by_dates
from src.extraction.record import apply_delta, new_record, strip_sidecars, summary_for_prompt
from tests.unit.test_record_and_consolidate import M1, M2, M3, ctx, delta_first_window, prov

MAILS = {
    M1: "OMD fail alarm unit 2. We checked for terminal tightness & sensor cleaned , alarm still persists. Vessel renewed the sensor with spare one.",
    M2: "Noted. Please monitor the OMD for 48 hours.",
    M3: "OMD alarm again on unit 2 after three weeks of normal running. Sensor cleaned again.",
}


def _base(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    return r


# --- recurrence ---------------------------------------------------------------

def test_recurrence_is_a_new_fault_row_linked_to_the_earlier_one(mini_casefile):
    r = _base(mini_casefile)
    d = {
        "add": {
            "faults": [{"fault_id": "f2", "symptom": "OMD alarm again on unit 2", "equipment_id": "e1", "first_observed_at": "2026-07-12",
                        "recurrence_of": "f1", "provenance": prov(M3, quote="OMD alarm again on unit 2")}],
            "action_logs": [{"action_id": "a3", "fault_id": "f2", "action": "sensor cleaned again", "kind": "corrective", "performed_by_role": "vessel_crew",
                             "asserted_in_mail": M3, "outcome": {"value": "unknown"}, "provenance": prov(M3, quote="Sensor cleaned again")}],
        },
        "update": {},
    }
    applied = apply_delta(r, d, ctx(M3))
    assert applied.added["faults"] == ["f2"] and applied.flags == []
    f1, f2 = r["faults"]
    assert f1["recurrence_of"] is None and f2["recurrence_of"] == "f1"
    assert f1["first_observed_at"] == "2026-06-20" and f2["first_observed_at"] == "2026-07-12"  # earlier row untouched
    assert r["action_logs"][-1]["fault_id"] == "f2"
    stored = strip_sidecars(r)
    assert schema_errors(stored) == []  # recurrence_of is in the schema
    assert validate_record(stored, MAILS, {}) == []
    s = summary_for_prompt(r)
    assert s["faults"][1]["recurrence_of"] == "f1" and s["next_free_ids"]["faults"] == "f3"


def test_recurrence_of_unknown_or_self_is_flagged_and_nulled(mini_casefile):
    r = _base(mini_casefile)
    d = {"add": {"faults": [
        {"fault_id": "f2", "symptom": "x again", "equipment_id": "e1", "recurrence_of": "f9", "provenance": prov(M3, quote="OMD alarm again")},
        {"fault_id": "f3", "symptom": "y again", "equipment_id": "e1", "recurrence_of": "f3", "provenance": prov(M3, quote="OMD alarm again")},
    ]}, "update": {}}
    applied = apply_delta(r, d, ctx(M3))
    assert r["faults"][1]["recurrence_of"] is None and r["faults"][2]["recurrence_of"] is None
    assert "unresolved_recurrence_of:f2:f9" in applied.flags and "unresolved_recurrence_of:f3:f3" in applied.flags


def test_validator_flags_dangling_recurrence(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    r["faults"][0]["recurrence_of"] = "f7"
    assert "unresolved_recurrence_of:f1" in validate_record(r, MAILS, {})


# --- fault_id correction --------------------------------------------------------

def test_update_can_correct_an_actions_fault_with_a_flag(mini_casefile):
    r = _base(mini_casefile)
    apply_delta(r, {"add": {"faults": [{"fault_id": "f2", "symptom": "leak", "equipment_id": "e1", "provenance": prov(M2, quote="monitor the OMD")}]}, "update": {}}, ctx(M2))
    applied = apply_delta(r, {"add": {}, "update": {"action_logs": [
        {"action_id": "a2", "fault_id": "f2", "provenance_add": {"mail_ids": [M3], "evidence_quote": "Sensor cleaned again"}},
        {"action_id": "a1", "fault_id": "f9"},
    ]}}, ctx(M3))
    assert r["action_logs"][1]["fault_id"] == "f2" and r["action_logs"][0]["fault_id"] == "f1"
    assert "fault_id_corrected:a2:f1>f2" in applied.flags and "unresolved_fault_id:a1:f9" in applied.flags
    assert applied.updated["action_logs"] == ["a2"]


# --- merge guard ----------------------------------------------------------------

def test_merge_blocked_by_dates():
    a = {"date_performed": "2026-06-20", "date_reported": "2026-06-21T00:00:00Z"}
    assert merge_blocked_by_dates(a, {"date_performed": "2026-06-20", "date_reported": "2026-08-01T00:00:00Z"}, 3) is None  # same event, referred back later
    assert "different date_performed" in merge_blocked_by_dates(a, {"date_performed": "2026-07-12", "date_reported": "2026-07-12T00:00:00Z"}, 3)
    assert "reported 21 days apart" in merge_blocked_by_dates(a, {"date_performed": None, "date_reported": "2026-07-12T00:00:00Z"}, 3)
    assert merge_blocked_by_dates(a, {"date_performed": None, "date_reported": "2026-06-23T00:00:00Z"}, 3) is None
    assert merge_blocked_by_dates(a, {"date_performed": None, "date_reported": "2026-07-12T00:00:00Z"}, -1) is None  # gap test off


def test_refused_merge_keeps_both_rows_as_a_repeat_group(mini_casefile):
    r = _base(mini_casefile)
    apply_delta(r, {"add": {"action_logs": [
        {"action_id": "a3", "fault_id": "f1", "action": "sensor renewed with spare", "kind": "corrective", "performed_by_role": "vessel_crew",
         "asserted_in_mail": M3, "outcome": {"value": "unknown"}, "provenance": prov(M3, quote="Sensor cleaned again")},
    ]}, "update": {}}, ctx(M3))
    r["action_logs"][1]["date_performed"] = "2026-06-20"  # a2: the first renewal
    r["action_logs"][2]["date_performed"] = "2026-07-12"  # a3: the second, three weeks later
    log = apply_consolidation(r, {"merge": [{"keep": "a2", "drop": "a3", "reason": "same renewal"}], "repeat_groups": []})
    ids = [a["action_id"] for a in r["action_logs"]]
    assert "a2" in ids and "a3" in ids and log["merged"] == []
    assert log["rejected_merges"][0]["kept_as"] == "repeat_group" and "different date_performed" in log["rejected_merges"][0]["why"]
    by = {a["action_id"]: a for a in r["action_logs"]}
    assert by["a2"]["occurrence_no"] == 1 and by["a3"]["occurrence_no"] == 2


# --- citation repair ------------------------------------------------------------

def test_quote_in_uncited_attachment_is_repaired_with_a_soft_flag(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    cid = r["casefile_id"]
    cited, scan = "1" * 64, "2" * 64
    a2 = r["action_logs"][1]
    a2["provenance"]["attachment_shas"] = [cited]
    a2["outcome"] = {"value": "resolved", "verification": "confirmed_by_oem", "outcome_in_mail": M1, "outcome_date": "2026-06-21",
                     "evidence_quote": "All observed in satisfactory condition."}
    digests = {cited: {"sha256": cited, "content_bearing": True, "links": [{"mailbodyId": M1, "casefileId": cid}], "extracted_text": "All observed in sattisfactory condition."}}
    casefile_digests = dict(digests)
    casefile_digests[scan] = {"sha256": scan, "content_bearing": True, "links": [{"mailbodyId": M3, "casefileId": cid}], "extracted_text": "All observed in satisfactory condition."}

    before = validate_record(r, MAILS, digests)
    assert "quote_not_found:a2.outcome" in before

    soft = repair_citations(r, MAILS, digests, casefile_digests)
    assert soft == [f"quote_in_uncited_attachment:a2.outcome:{scan[:8]}"]
    sup = a2["provenance"]["supporting"][-1]  # the outcome's own evidence entry: sha + a mail that carries it
    assert sup["attachment_shas"] == [scan] and sup["mail_ids"] == [M3] and sup["fields"] == ["outcome"]
    assert a2["provenance"]["attachment_shas"] == [cited] and M3 in a2["provenance"]["mail_ids"]
    flags = validate_record(r, MAILS, digests)
    assert "quote_not_found:a2.outcome" not in flags and not any(f.startswith("sha_") for f in flags)
    apply_validation(r, soft + flags)
    assert r["validation_flags"] == soft and r["needs_review"] is False  # soft flags do not send a record to review


def test_repair_leaves_a_quote_found_nowhere_alone(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    r["faults"][0]["provenance"]["evidence_quote"] = "text nobody wrote"
    assert repair_citations(r, MAILS, {}, {}) == []
    flags = validate_record(r, MAILS, {})
    assert "quote_not_found:f1" in flags
    apply_validation(r, flags)
    assert r["needs_review"] is True
