"""The deterministic validator: schema, links, quotes, sha join, outcome rule, cap."""

import copy
import json
from pathlib import Path

from src.contract.validator import apply_validation, schema_errors, validate_record
from src.extraction.record import WindowContext, apply_delta, new_record, strip_sidecars
from tests.unit.test_record_and_consolidate import M1, M2, M3, delta_first_window

SCHEMA_SAMPLE = Path(__file__).resolve().parents[2] / "schema" / "king_one_casefile_69a6965bf935cb9be885b8ee.v2.json"

MAILS = {
    M1: "From: CE\nOMD fail alarm unit 2. We checked for terminal tightness & sensor cleaned , alarm still persists. Vessel renewed the sensor with spare one.",
    M2: "Noted. Please monitor the OMD for 48 hours.",
    M3: "OMD monitored for 48 hrs, no further alarm observed.",
}


def _ctx(*ids, shas=()):
    return WindowContext(mail_ids=set(ids), mail_dates={M1: "2026-06-21T00:26:05Z", M2: "2026-06-21T05:13:47Z", M3: "2026-06-22T08:00:00Z"}, shas=set(shas))


def _built(mini_casefile):
    r = new_record(mini_casefile)
    apply_delta(r, delta_first_window(), _ctx(M1))
    return r


def test_king_one_sample_is_schema_valid_once_versioned():
    sample = json.loads(SCHEMA_SAMPLE.read_text(encoding="utf-8"))
    sample.setdefault("vessel_name", "KING ONE")
    sample.setdefault("extraction_version", "sample")
    assert schema_errors(sample) == []


def test_built_record_is_schema_valid_and_clean(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    assert schema_errors(r) == []
    flags = validate_record(r, MAILS, {})
    assert flags == []
    apply_validation(r, flags)
    assert r["needs_review"] is False and r["validation_flags"] == []


def test_quote_not_found_is_flagged(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    r["faults"][0]["provenance"]["evidence_quote"] = "text that is not in the mail"
    flags = validate_record(r, MAILS, {})
    assert "quote_not_found:f1" in flags


def test_outcome_quote_checked_against_outcome_mail(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    a1 = r["action_logs"][0]
    a1["outcome"]["outcome_in_mail"] = M2  # quote "alarm still persists" is in M1, not M2
    flags = validate_record(r, MAILS, {})
    assert "quote_not_found:a1.outcome" in flags


def test_unresolved_links_and_cap(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    r["faults"][0]["equipment_id"] = "e7"
    r["faults"][0]["problem_id"] = "p3"
    r["action_logs"][1]["fault_id"] = "f9"
    flags = validate_record(r, MAILS, {}, max_actions=1)
    assert "unresolved_equipment_id:f1" in flags
    assert "unresolved_problem_id:f1" in flags
    assert "unresolved_fault_id:a2" in flags
    assert any(f.startswith("over_cap:2>1") for f in flags)


def test_sha_join_rules(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    sha_ok, sha_logo, sha_elsewhere = "1" * 64, "2" * 64, "3" * 64
    r["faults"][0]["provenance"]["attachment_shas"] = [sha_ok, sha_logo, sha_elsewhere, "4" * 64]
    r["faults"][0]["provenance"]["evidence_quote"] = "photodiode fault confirmed in service note"
    digests = {
        sha_ok: {"sha256": sha_ok, "content_bearing": True, "links": [{"mailbodyId": M1}], "extracted_text": "Service note: photodiode fault confirmed in service note."},
        sha_logo: {"sha256": sha_logo, "content_bearing": False, "links": [{"mailbodyId": M1}], "extracted_text": None},
        sha_elsewhere: {"sha256": sha_elsewhere, "content_bearing": True, "links": [{"mailbodyId": M3}], "extracted_text": "x"},
    }
    flags = validate_record(r, MAILS, digests)
    assert f"non_content_bearing_sha:f1:{sha_logo[:8]}" in flags
    assert f"sha_not_on_cited_mail:f1:{sha_elsewhere[:8]}" in flags
    assert f"sha_unknown:f1:{'4' * 8}" in flags
    assert "quote_not_found:f1" not in flags  # found in the attachment text


def test_schema_rejects_unknown_outcome_with_quote(mini_casefile):
    r = strip_sidecars(_built(mini_casefile))
    bad = copy.deepcopy(r)
    bad["action_logs"][1]["outcome"]["evidence_quote"] = "should be null"
    flags = validate_record(bad, MAILS, {})
    assert any(f.startswith("schema_error") for f in flags)
    assert "outcome_inconsistent:a2" in flags
