"""
The four defects of the 10-casefile dedup review, and the general guards added
with them:

  1. one sentence as proof of both an action and its result  -> split_shared_quotes
  2. one event written once per fault                         -> also_fault_ids, collapse_fanout
  3. one quote cited under several attachment hashes          -> provenance.supporting, prune_shas
  4. faults that read alike                                   -> fault pairs judged with a number/side guard
  +  twin mails, future date_performed, outcome_before_action, problem_restates_symptom
"""

import dataclasses

from src.attachments.digest import AttachmentDigester
from src.attachments.fetch import Fetcher
from src.contract.validator import apply_validation, prune_shas, schema_errors, validate_record
from src.corpus.export import Page
from src.extraction.consolidate import (
    apply_consolidation,
    apply_fault_verdicts,
    collapse_fanout,
    fault_pair_candidates,
)
from src.extraction.quote_split import quotes_equivalent, shared_quote_actions, split_shared_quotes
from src.extraction.record import apply_delta, new_record, strip_sidecars, summary_for_prompt
from src.extraction.runner import CasefileRunner, RunOptions
from src.extraction.textsim import fold, signatures_agree, similarity
from src.extraction.windows import WindowSpec
from tests.conftest import MAIL_HTML, FakeHttpClient, FakeLLM
from tests.unit.test_pipeline_fake_llm import _window1_delta, _window2_delta
from tests.unit.test_record_and_consolidate import M1, M2, M3, ctx, delta_first_window, prov
from tests.unit.test_v4_rules import MAILS

M4 = "bbbbbbbbbbbbbbbbbbbbbbb4"


def _base(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), ctx(M1))
    return r


def _action(aid, fid, mail, action="Ask the maker for advice regarding the knocking sound.", quote="monitor the OMD", kind="administrative", **kw):
    return {"action_id": aid, "fault_id": fid, "action": action, "kind": kind, "performed_by_role": "superintendent",
            "asserted_in_mail": mail, "outcome": {"value": "unknown"}, "provenance": prov(mail, quote=quote), **kw}


# --- text similarity and the number guard ---------------------------------------

def test_similarity_ignores_numbers_and_the_signature_catches_them():
    a, b = "Knocking sound from M/E Cylinder No.3", "Knocking sound from M/E Cylinder No.6"
    assert similarity(a, b) >= 0.95  # blind to the digit, as the review found
    assert not signatures_agree(a, b)  # ...so the guard is what keeps them apart
    assert signatures_agree("Knocking sound from main engine", a)  # no number does not conflict
    assert not signatures_agree("port main engine bellows", "stbd main engine bellows")
    assert signatures_agree("right main engine", "starboard main engine")


def test_fold_is_for_comparison_only():
    assert fold("M/E") == fold("main-engine") == fold("Main Engine")
    assert similarity("Unit #5 exhaust valve knocking", "cyl 5 exhaust valve knocking") == 1.0
    assert similarity("fuel pump spring broken", "crane wire parted") < 0.3


# --- 1. action quote vs outcome quote --------------------------------------------

SENTENCE = "Vessel renewed the sensor with spare one, tried out and found to be working satisfactory."


def _shared(mini_casefile):
    r = _base(mini_casefile)
    a2 = r["action_logs"][1]
    a2["provenance"]["evidence_quote"] = SENTENCE
    a2["outcome"] = {"value": "resolved", "verification": "confirmed_by_vessel", "outcome_in_mail": M1, "outcome_date": "2026-06-21", "evidence_quote": SENTENCE}
    return r


def test_quotes_equivalent():
    assert quotes_equivalent(SENTENCE, SENTENCE.upper())
    assert quotes_equivalent("报警仍然存在 [EN: alarm still present]", "报警仍然存在")
    assert not quotes_equivalent("We changed the sensor", "but issue still persist")
    assert not quotes_equivalent("sensor cleaned , alarm still persists", "alarm still persists")  # a clause of it, not the same sentence
    assert not quotes_equivalent(None, "x")


async def test_shared_sentence_is_cut_into_action_and_result(mini_casefile):
    r = _shared(mini_casefile)
    assert [a["action_id"] for a in shared_quote_actions(r)] == ["a2"]
    llm = FakeLLM([{"splits": [{"id": "a2", "action_part": "Vessel renewed the sensor with spare one", "result_part": "tried out and found to be working satisfactory."}]}])
    assert await split_shared_quotes(llm, "sys", r, None) == 1
    a2 = r["action_logs"][1]
    assert a2["provenance"]["evidence_quote"] == "Vessel renewed the sensor with spare one"
    assert a2["outcome"]["evidence_quote"] == "tried out and found to be working satisfactory."
    assert shared_quote_actions(r) == []
    flags = validate_record(strip_sidecars(r), {M1: SENTENCE + " We checked for terminal tightness & sensor cleaned , alarm still persists."}, {})
    assert not any(f.startswith(("quote_not_found", "same_quote")) for f in flags)  # both halves are still verbatim
    assert await split_shared_quotes(FakeLLM([]), "sys", r, None) == 0  # nothing shared -> no call


async def test_split_rejects_parts_that_are_not_verbatim_or_missing(mini_casefile):
    for bad in (
        {"id": "a2", "action_part": "Vessel replaced the sensor", "result_part": "found to be working satisfactory."},  # reworded
        {"id": "a2", "action_part": None, "result_part": SENTENCE},  # only a result: cannot be cut
        {"id": "a2", "action_part": SENTENCE, "result_part": SENTENCE},  # the same text twice
        {"id": "a9", "action_part": "x", "result_part": "y"},  # unknown row
    ):
        r = _shared(mini_casefile)
        assert await split_shared_quotes(FakeLLM([{"splits": [bad]}]), "sys", r, None) == 0
        assert r["action_logs"][1]["provenance"]["evidence_quote"] == SENTENCE


def test_leftover_shared_quote_is_a_soft_flag(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    r["action_logs"][0]["provenance"]["evidence_quote"] = "alarm still persists"  # same as its outcome quote
    flags = validate_record(r, MAILS, {})
    assert flags == ["same_quote_action_outcome:a1"]
    apply_validation(r, flags)
    assert r["needs_review"] is False


# --- 2. one event, one row --------------------------------------------------------

def _two_faults(mini_casefile):
    r = _base(mini_casefile)
    apply_delta(r, {"add": {"faults": [{"fault_id": "f2", "symptom": "Knocking sound", "position": "cyl 2", "equipment_id": "e1", "provenance": prov(M2, quote="monitor the OMD")}]}, "update": {}}, ctx(M2))
    return r


def test_also_fault_ids_on_add_and_update(mini_casefile):
    r = _two_faults(mini_casefile)
    applied = apply_delta(r, {"add": {"action_logs": [_action("a3", "f1", M2, also_fault_ids=["f2", "f1", "f9"])]}, "update": {}}, ctx(M2))
    a3 = r["action_logs"][-1]
    assert a3["fault_id"] == "f1" and a3["also_fault_ids"] == ["f2"]
    assert "unresolved_fault_id:a3:f9" in applied.flags
    assert summary_for_prompt(r)["action_logs"][-1]["also_fault_ids"] == ["f2"]
    assert schema_errors(strip_sidecars(r)) == []
    applied = apply_delta(r, {"add": {}, "update": {"action_logs": [{"action_id": "a1", "also_fault_ids": ["f2"]}]}}, ctx(M3))
    assert r["action_logs"][0]["also_fault_ids"] == ["f2"] and applied.updated["action_logs"] == ["a1"]
    assert not any(f.startswith("unresolved_fault_id") for f in validate_record(strip_sidecars(r), MAILS, {}))


def test_fanout_rows_collapse_into_one_event(mini_casefile):
    r = _two_faults(mini_casefile)
    apply_delta(r, {"add": {"action_logs": [
        _action("a3", "f1", M2, target="cyl 4"),
        _action("a4", "f2", M2, target="cyl 2"),
        _action("a5", "f2", M2, action="Renew the fuel pump plunger."),  # a different action from the same sentence
    ]}, "update": {}}, ctx(M2))
    log = collapse_fanout(r)
    ids = [a["action_id"] for a in r["action_logs"]]
    assert "a4" not in ids and "a3" in ids and "a5" in ids
    a3 = next(a for a in r["action_logs"] if a["action_id"] == "a3")
    assert a3["fault_id"] == "f1" and a3["also_fault_ids"] == ["f2"] and a3["target"] == "cyl 4; cyl 2"
    assert log == [{"keep": "a3", "fault_ids": ["f1", "f2"], "dropped_rows": log[0]["dropped_rows"]}] and log[0]["dropped_rows"][0]["action_id"] == "a4"
    assert schema_errors(strip_sidecars(r)) == []


def test_fanout_keeps_rows_that_are_not_one_event(mini_casefile):
    r = _two_faults(mini_casefile)
    apply_delta(r, {"add": {"action_logs": [
        _action("a3", "f1", M2),
        _action("a4", "f2", M3, quote="OMD alarm again on unit 2"),  # another mail: another event
        _action("a5", "f1", M2),  # same fault as a3: left for the duplicate-merge step
    ]}, "update": {}}, ctx(M2, M3))
    r["action_logs"][2]["outcome"]["value"] = "resolved"
    assert collapse_fanout(r) == [] and len(r["action_logs"]) == 5
    # Contradicting outcomes on two faults are two rows.
    apply_delta(r, {"add": {"action_logs": [_action("a6", "f2", M2)]}, "update": {}}, ctx(M2))
    r["action_logs"][-1]["outcome"]["value"] = "no_effect"
    assert collapse_fanout(r) == []


# --- 3. one quote, the documents that state it -------------------------------------

def test_supporting_quotes_are_validated(mini_casefile):
    r = _base(mini_casefile)
    apply_delta(r, {"add": {}, "update": {"action_logs": [{"action_id": "a2", "target": "unit 2", "provenance_add": {"mail_ids": [M3], "evidence_quote": "text nobody wrote"}}]}}, ctx(M3))
    stored = strip_sidecars(r)
    assert stored["action_logs"][1]["provenance"]["supporting"][0]["fields"] == ["target"]
    assert schema_errors(stored) == []
    assert "quote_not_found:a2.supporting1" in validate_record(stored, MAILS, {})


def test_update_with_sha_but_no_quote_is_flagged_not_cited(mini_casefile):
    r = _base(mini_casefile)
    sha = "9" * 64
    applied = apply_delta(r, {"add": {}, "update": {"equipment": [{"equipment_id": "e1", "maker": "Schaller", "provenance_add": {"mail_ids": [M3], "attachment_shas": [sha]}}]}}, ctx(M3, shas=[sha]))
    assert "update_without_quote:e1" in applied.flags
    assert r["equipment"][0]["provenance"]["attachment_shas"] == [] and "supporting" not in r["equipment"][0]["provenance"]


def test_prune_keeps_only_attachments_that_hold_the_quote(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    s1, s2, s3 = "1" * 64, "2" * 64, "3" * 64
    p = r["faults"][0]["provenance"]
    p["evidence_quote"] = "photodiode fault confirmed"
    p["attachment_shas"] = [s1, s2]
    digests = {s1: {"extracted_text": "Service note: photodiode fault confirmed."}, s2: {"extracted_text": "Delivery receipt"}, s3: {"extracted_text": "x"}}
    assert prune_shas(r, MAILS, digests) == [{"row": "f1", "kept": [s1[:8]], "dropped": [s2[:8]]}]
    assert p["attachment_shas"] == [s1]
    # Quote in none of them (it is in the mail body): nothing is pruned, the validator judges it.
    r["equipment"][0]["provenance"]["attachment_shas"] = [s2, s3]
    assert prune_shas(r, MAILS, digests) == []
    assert r["equipment"][0]["provenance"]["attachment_shas"] == [s2, s3]


# --- 4. faults that read alike ------------------------------------------------------

def _dup_fault(mini_casefile, symptom, first=None):
    r = _base(mini_casefile)
    fault = {"fault_id": "f2", "symptom": symptom, "equipment_id": "e1", "first_observed_at": first, "provenance": prov(M3, quote="OMD alarm again on unit 2")}
    apply_delta(r, {"add": {"faults": [fault], "action_logs": [_action("a3", "f2", M3, quote="Sensor cleaned again", kind="corrective", action="Sensor cleaned.")]}, "update": {}}, ctx(M3))
    return r


def test_same_fault_recorded_twice_is_merged(mini_casefile):
    r = _dup_fault(mini_casefile, "OMD fail alarm on unit 2 with photodiode fault")
    cands = fault_pair_candidates(r)
    assert [(c["a"], c["b"], c["numbers_or_sides_differ"]) for c in cands] == [("f1", "f2", False)]
    log = apply_fault_verdicts(r, [{"a": "f1", "b": "f2", "verdict": "same", "reason": "described again"}], cands)
    assert [f["fault_id"] for f in r["faults"]] == ["f1"]
    assert r["action_logs"][-1]["fault_id"] == "f1"  # its action followed it
    sup = r["faults"][0]["provenance"]["supporting"][-1]
    assert sup["fields"] == ["merged_from:f2"] and sup["evidence_quote"] == "OMD alarm again on unit 2" and M3 in r["faults"][0]["provenance"]["mail_ids"]
    assert log["merged"][0]["drop"] == "f2" and log["merged"][0]["dropped_row"]["fault_id"] == "f2"  # kept whole in the log
    assert schema_errors(strip_sidecars(r)) == [] and validate_record(strip_sidecars(r), MAILS, {}) == []


def test_merge_is_refused_when_numbers_differ(mini_casefile):
    r = _dup_fault(mini_casefile, "OMD fail alarm unit 4, photodiode fault")
    cands = fault_pair_candidates(r)
    assert cands[0]["numbers_or_sides_differ"] is True
    log = apply_fault_verdicts(r, [{"a": "f1", "b": "f2", "verdict": "same"}], cands)
    assert [f["fault_id"] for f in r["faults"]] == ["f1", "f2"] and r["faults"][1]["recurrence_of"] is None
    assert log["refused"][0]["why"].startswith("numbers or sides differ") and log["left_distinct"] == []


def test_same_after_a_resolved_action_becomes_recurrence(mini_casefile):
    r = _dup_fault(mini_casefile, "OMD fail alarm on unit 2 with photodiode fault", first="2026-07-12")
    r["action_logs"][1]["outcome"] = {"value": "resolved", "verification": "confirmed_by_vessel", "outcome_in_mail": M3, "outcome_date": "2026-06-22", "evidence_quote": "no further alarm"}
    cands = fault_pair_candidates(r)
    log = apply_fault_verdicts(r, [{"a": "f1", "b": "f2", "verdict": "same"}], cands)
    assert [f["fault_id"] for f in r["faults"]] == ["f1", "f2"] and r["faults"][1]["recurrence_of"] == "f1"
    assert log["merged"] == [] and log["recurrences"][0]["later"] == "f2"
    assert fault_pair_candidates(r) == []  # linked pairs are not proposed again


def test_verdicts_on_pairs_the_code_did_not_propose_are_ignored(mini_casefile):
    r = _dup_fault(mini_casefile, "Fuel pump spring broken")  # nothing like f1
    assert fault_pair_candidates(r) == []
    log = apply_fault_verdicts(r, [{"a": "f1", "b": "f2", "verdict": "same"}], [])
    assert len(r["faults"]) == 2 and log["merged"] == []


def test_distinct_lookalikes_are_reported(mini_casefile):
    r = _dup_fault(mini_casefile, "OMD fail alarm on unit 2 with photodiode fault")
    cands = fault_pair_candidates(r)
    log = apply_fault_verdicts(r, [{"a": "f1", "b": "f2", "verdict": "distinct"}], cands)
    assert log["left_distinct"] == [["f1", "f2"]] and len(r["faults"]) == 2


def test_duplicate_action_merge_needs_the_same_target_numbers(mini_casefile):
    r = _base(mini_casefile)
    apply_delta(r, {"add": {"action_logs": [
        _action("a3", "f1", M1, action="Renewed exhaust valve on cyl 3", kind="corrective", quote="renewed the sensor"),
        _action("a4", "f1", M1, action="Renewed exhaust valve on cyl 5", kind="corrective", quote="renewed the sensor"),
    ]}, "update": {}}, ctx(M1))
    log = apply_consolidation(r, {"merge": [{"keep": "a3", "drop": "a4"}]})
    assert log["merged"] == [] and log["rejected_merges"][0]["why"].startswith("numbers or sides differ")


# --- dates, problems ------------------------------------------------------------------

def test_future_date_performed_is_a_plan_not_a_performance(mini_casefile):
    r = _base(mini_casefile)  # M1 is dated 2026-06-21
    applied = apply_delta(r, {"add": {"action_logs": [
        _action("a3", "f1", M1, date_performed="2026-06-30"),
        _action("a4", "f1", M1, date_performed="2026-06-22", action="Sensor cleaned."),  # one day of slack: vessel-local vs UTC
    ]}, "update": {}}, ctx(M1))
    assert r["action_logs"][2]["date_performed"] is None and r["action_logs"][3]["date_performed"] == "2026-06-22"
    assert "future_date_performed:a3:2026-06-30" in applied.flags
    applied = apply_delta(r, {"add": {}, "update": {"action_logs": [{"action_id": "a3", "date_performed": "2026-07-30"}]}}, ctx(M3))  # M3: 2026-06-22
    assert r["action_logs"][2]["date_performed"] is None and "future_date_performed:a3:2026-07-30" in applied.flags
    applied = apply_delta(r, {"add": {}, "update": {"action_logs": [{"action_id": "a3", "date_performed": "2026-06-22"}]}}, ctx(M3))
    assert r["action_logs"][2]["date_performed"] == "2026-06-22"


def test_outcome_before_action_and_problem_restating_symptom(mini_casefile):
    r = strip_sidecars(_base(mini_casefile))
    a1 = r["action_logs"][0]
    a1["date_performed"], a1["outcome"]["outcome_date"] = "2026-06-25", "2026-06-21"
    r["problems"].append({"problem_id": "p1", "statement": "OMD fail alarm on unit 2, photodiode fault", "asserted_by": "vessel", "concluded_at": None, "provenance": prov(M1, quote="sensor cleaned")})
    r["faults"][0]["problem_id"] = "p1"
    flags = validate_record(r, MAILS, {})
    assert "outcome_before_action:a1" in flags and "problem_restates_symptom:p1:f1" in flags
    apply_validation(r, flags)
    assert r["needs_review"] is True
    r["problems"][0]["statement"] = "Photodiode of the sensor burnt out"  # a cause, not the symptom again
    assert "problem_restates_symptom:p1:f1" not in validate_record(r, MAILS, {})


# --- twin mails --------------------------------------------------------------------------

async def test_twin_mail_is_shown_once_and_recorded_as_alias(tmp_store, mini_casefile):
    twin = Page(mail_id=M4, pagenum=3, date_time="2026-06-21T00:26:07Z", sender_name="Chief Engineer", sender_email="cheng@vessel.example",
                to=None, subject="Main engine OMD fault rectified", link=f"https://example.invalid/mail/{M4}.html")
    casefile = dataclasses.replace(mini_casefile, pages=[*mini_casefile.pages, twin])
    http = FakeHttpClient({**MAIL_HTML, M4: MAIL_HTML[M1]}, {})  # the same mail stored under a second id
    llm = FakeLLM([_window1_delta(), _window2_delta(), {"fault_pairs": [], "merge": [], "repeat_groups": []}])
    fetcher = Fetcher(client=http, retries=1)
    runner = CasefileRunner(tmp_store, llm, fetcher, AttachmentDigester(tmp_store, fetcher, llm, mode="off"), RunOptions(window=WindowSpec(2, 0)))
    report = await runner.run(casefile)
    assert report.complete and report.error is None
    assert llm.prompts[1][1].count("### MAIL mail_id=") == 1 and M4 not in llm.prompts[1][1]  # window 2: mail 3 only
    log = tmp_store.get_run_log(casefile.casefile_id)
    assert log["runs"][0]["mail_aliases"] == {M4: M1}
    record = tmp_store.get_record(casefile.casefile_id)
    assert record["progress"]["last_pagenum_processed"] == 3 and record["validation_flags"] == []


def test_fanout_copies_that_differ_only_in_the_fault_they_name(mini_casefile):
    """One quotation for one service attendance covering three issues is one event."""
    r = _two_faults(mini_casefile)
    lead = "Quote Everllence service attendance to investigate the pending "
    apply_delta(r, {"add": {"action_logs": [
        _action("a3", "f1", M2, action=lead + "knocking sound on exhaust valve no. 4.", kind="commercial"),
        _action("a4", "f2", M2, action=lead + "main-engine starting failure.", kind="commercial"),
        _action("a5", "f2", M2, action="Renew the fuel pump plunger and test the engine.", kind="commercial"),
    ]}, "update": {}}, ctx(M2))
    log = collapse_fanout(r)
    assert [d["action_id"] for d in log[0]["dropped_rows"]] == ["a4"]
    assert [a["action_id"] for a in r["action_logs"]][-2:] == ["a3", "a5"]


# --- P3: the symptom is the failure mode, short (docs/16) --------------------------

def test_long_symptom_is_a_quality_flag_not_a_failure(mini_casefile):
    """window_update/v6 asks for the failure mode in <=6 words; a sentence past 12 words is
    recorded as a soft flag (the extractor's defect), never a needs_review."""
    r = strip_sidecars(_base(mini_casefile))
    r["faults"][0]["symptom"] = "crankcase oil detector fail alarm"
    assert validate_record(r, MAILS, {}) == []
    r["faults"][0]["symptom"] = (
        "Main engine crankcase oil mist detector fail alarm for unit no 2 activated in AMS during the night watch"
    )
    flags = validate_record(r, MAILS, {})
    assert flags == ["symptom_too_long:f1"]
    apply_validation(r, flags)
    assert r["needs_review"] is False
    # exactly 12 words is still fine
    r["faults"][0]["symptom"] = " ".join(["word"] * 12)
    assert validate_record(r, MAILS, {}) == []
