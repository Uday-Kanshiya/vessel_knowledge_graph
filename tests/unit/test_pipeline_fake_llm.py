"""
End-to-end over the 3-mail fixture with a scripted model and a fake HTTP
client: windowing (2 mails per call here), attachment fetch + sha cache +
junk filter, delta application, checkpoint/resume with --max-windows,
consolidate, validation, stored files. No network, no real model.
"""

import json

import pytest

from src.attachments.digest import AttachmentDigester
from src.attachments.fetch import Fetcher
from src.extraction.runner import CasefileRunner, RunOptions, plan
from src.extraction.windows import WindowSpec
from tests.conftest import FakeLLM

M1, M2, M3 = "bbbbbbbbbbbbbbbbbbbbbbb1", "bbbbbbbbbbbbbbbbbbbbbbb2", "bbbbbbbbbbbbbbbbbbbbbbb3"


def _window1_delta():
    return {
        "add": {
            "equipment": [{"equipment_id": "e1", "name": "Main engine oil mist detector (OMD)", "equipment_type": "oil_mist_detector", "maker": "NA", "model": "NA",
                           "provenance": {"mail_ids": [M1], "attachment_shas": [], "evidence_quote": "Main engine crankcase oil detector fail alarm for unit no:2"}}],
            "problems": [],
            "faults": [{"fault_id": "f1", "symptom": "OMD fail alarm unit no:2, photodiode fault", "alarms": ["Main engine crankcase oil detector fail alarm"], "equipment_id": "e1", "problem_id": None, "first_observed_at": "2026-06-20",
                        "provenance": {"mail_ids": [M1], "attachment_shas": [], "evidence_quote": "OMD control Panel displays photodiode fault"}}],
            "action_logs": [
                {"action_id": "a1", "fault_id": "f1", "action": "checked terminal tightness and cleaned sensor", "kind": "diagnostic", "performed_by_role": "vessel_crew", "asserted_in_mail": M1,
                 "outcome": {"value": "inconclusive", "verification": "reported_only", "outcome_in_mail": M1, "outcome_date": None, "evidence_quote": "alarm still persists"},
                 "provenance": {"mail_ids": [M1], "attachment_shas": [], "evidence_quote": "sensor cleaned , alarm still persists"}},
                {"action_id": "a2", "fault_id": "f1", "action": "renewed the sensor with spare", "kind": "corrective", "performed_by_role": "vessel_crew", "asserted_in_mail": M1,
                 "outcome": {"value": "unknown", "verification": "none", "outcome_in_mail": None, "outcome_date": None, "evidence_quote": None},
                 "provenance": {"mail_ids": [M1], "attachment_shas": [], "evidence_quote": "Vessel renewed the sensor with spare one"}},
            ],
        },
        "update": {},
        "window_notes": "fault reported and first actions",
    }


def _window2_delta():
    return {
        "add": {"equipment": [], "problems": [], "faults": [], "action_logs": []},
        "update": {"action_logs": [{"action_id": "a2", "outcome": {"value": "resolved", "verification": "confirmed_by_vessel", "outcome_in_mail": M3, "outcome_date": None, "evidence_quote": "no further alarm observed"},
                                    "provenance_add": {"mail_ids": [M3], "evidence_quote": "no further alarm observed"}}]},
        "window_notes": "48 h monitoring result",
    }


def _make(tmp_store, fake_http, llm, spec=None, attachments="digest", **opt):
    spec = spec or WindowSpec(2, 1)
    fetcher = Fetcher(client=fake_http, retries=1)
    digester = AttachmentDigester(tmp_store, fetcher, llm, mode=attachments, concurrency=2)
    runner = CasefileRunner(tmp_store, llm, fetcher, digester, RunOptions(window=spec, **opt))
    return runner


async def test_full_run_two_windows(tmp_store, fake_http, mini_casefile):
    llm = FakeLLM([_window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": [], "notes": ""}])
    runner = _make(tmp_store, fake_http, llm)
    report = await runner.run(mini_casefile)

    assert report.error is None
    assert report.windows_total == 2 and report.windows_run == 2 and report.complete
    record = tmp_store.get_record(mini_casefile.casefile_id)
    assert record["progress"] == {"last_pagenum_processed": 2, "complete": True}
    assert [a["action_id"] for a in record["action_logs"]] == ["a1", "a2"]
    a2 = record["action_logs"][1]
    assert a2["outcome"]["value"] == "resolved" and a2["outcome"]["outcome_in_mail"] == M3
    assert a2["outcome"]["outcome_date"] == "2026-06-22"
    assert record["validation_flags"] == [] and record["needs_review"] is False
    assert record["extraction_version"].startswith("casefile_extract.v2+wu.")

    # Every mail per call <= window size, first mail of window 2 marked as context.
    prompts = [p for _, p in llm.prompts[:2]]
    assert prompts[0].count("### MAIL mail_id=") == 2
    assert prompts[1].count("### MAIL mail_id=") == 2
    assert "context only" in prompts[1] and "context only" not in prompts[0]
    # The consolidate call saw the record only.
    assert llm.prompts[2][1].startswith("## RECORD")

    # Attachments: logo junk-filtered (no digest call), fake PDF -> listed_only (no text, no call).
    # 2 links on the overlapping mail, seen by both windows: 4 link events, 2 fetches, 2 cache hits.
    assert report.attachments["links"] == 4 and report.attachments["fetched"] == 2 and report.attachments["cache_hits"] == 2
    assert report.attachments["digest_calls"] == 0
    digests = list(tmp_store.att_dir.glob("*.json"))
    assert len(digests) == 2
    by_name = {d["audit"]["name"]: d for d in (json.loads(p.read_text(encoding="utf-8")) for p in digests)}
    assert by_name["image001.png"]["content_bearing"] is False and by_name["image001.png"]["extraction_method"] == "skipped"
    assert by_name["OMD service note.pdf"]["content_bearing"] is True and by_name["OMD service note.pdf"]["extraction_method"] == "listed_only"
    assert by_name["OMD service note.pdf"]["links"][0]["mailbodyId"] == M2
    # The prompt never shows a URL.
    assert "example.invalid/att" not in prompts[1]

    # Audit files and run log exist.
    assert tmp_store.window_path(mini_casefile.casefile_id, 0).exists()
    assert tmp_store.window_path(mini_casefile.casefile_id, 1).exists()
    log = tmp_store.get_run_log(mini_casefile.casefile_id)
    assert len(log["runs"]) == 1 and log["consolidate"]["called"] is True
    assert log["runs"][0]["tokens"]["call_count"] == 2


async def test_max_windows_pauses_and_resume_finishes(tmp_store, fake_http, mini_casefile):
    llm = FakeLLM([_window1_delta()])
    runner = _make(tmp_store, fake_http, llm, max_windows=1)
    r1 = await runner.run(mini_casefile)
    assert r1.windows_run == 1 and not r1.complete and r1.last_pagenum == 1
    stored = tmp_store.get_record(mini_casefile.casefile_id)
    assert stored["progress"]["complete"] is False and len(stored["action_logs"]) == 2

    pl = plan(mini_casefile, WindowSpec(2, 1), tmp_store)
    assert pl["windows_done"] == 1 and pl["windows_pending"] == 1 and pl["pending_pagenums"] == [[1, 2]]

    fetch_calls_before = len(fake_http.calls)
    llm2 = FakeLLM([_window2_delta(), {"merge": [], "repeat_groups": [], "notes": ""}])
    runner2 = _make(tmp_store, fake_http, llm2)
    r2 = await runner2.run(mini_casefile)
    assert r2.windows_done_before == 1 and r2.windows_run == 1 and r2.complete
    # Mail 1 (overlap) and its attachments came from cache: only mail 3's HTML was fetched.
    new_calls = fake_http.calls[fetch_calls_before:]
    assert new_calls == [f"https://example.invalid/mail/{M3}.html"]
    record = tmp_store.get_record(mini_casefile.casefile_id)
    assert record["action_logs"][1]["outcome"]["value"] == "resolved"
    assert record["validation_flags"] == []


async def test_restart_discards_stored_record(tmp_store, fake_http, mini_casefile):
    llm = FakeLLM([_window1_delta()])
    await _make(tmp_store, fake_http, llm, max_windows=1).run(mini_casefile)
    llm2 = FakeLLM([_window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": []}])
    r = await _make(tmp_store, fake_http, llm2, restart=True).run(mini_casefile)
    assert r.windows_done_before == 0 and r.windows_run == 2 and r.complete


async def test_rejected_delta_is_retried_once_then_flagged(tmp_store, fake_http, mini_casefile):
    bad = _window1_delta()
    bad["add"]["faults"][0]["equipment_id"] = "e9"  # unresolved -> DeltaError
    llm = FakeLLM([bad, bad, _window2_delta(), {"merge": [], "repeat_groups": []}])
    r = await _make(tmp_store, fake_http, llm).run(mini_casefile)
    assert r.windows_rejected == 1 and r.complete
    assert "## CORRECTION" in llm.prompts[1][1]
    record = tmp_store.get_record(mini_casefile.casefile_id)
    assert record["equipment"] == [] and record["action_logs"] == []
    assert any(f.startswith("window_rejected:w0") for f in record["validation_flags"])
    assert any(f.startswith("update_unknown_id:a2") for f in json.loads(tmp_store.window_path(mini_casefile.casefile_id, 1).read_text(encoding="utf-8"))["applied"]["flags"])
    assert record["needs_review"] is True


async def test_attachments_off_skips_fetch(tmp_store, fake_http, mini_casefile):
    llm = FakeLLM([_window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": []}])
    r = await _make(tmp_store, fake_http, llm, attachments="off").run(mini_casefile)
    assert r.complete and r.attachments["links"] == 0
    assert all("/att/" not in u for u in fake_http.calls)


async def test_provider_error_stops_run_but_keeps_checkpoint(tmp_store, fake_http, mini_casefile):
    from src.llm import LLMError

    llm = FakeLLM([_window1_delta(), LLMError("boom (auth)")])
    r = await _make(tmp_store, fake_http, llm).run(mini_casefile)
    assert r.error and "boom" in r.error
    assert r.windows_run == 1 and not r.complete
    stored = tmp_store.get_record(mini_casefile.casefile_id)
    assert stored["progress"]["last_pagenum_processed"] == 1
    assert tmp_store.get_run_log(mini_casefile.casefile_id)["runs"][0]["error"]


async def test_no_overlap_prompt_carries_record_summary_with_ids(tmp_store, fake_http, mini_casefile):
    """Default design: 5 new mails per window; memory is the id-keyed summary, not a repeated mail."""
    llm = FakeLLM([_window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": []}])
    r = await _make(tmp_store, fake_http, llm, spec=WindowSpec(2, 0)).run(mini_casefile)
    assert r.complete and r.windows_total == 2
    p1, p2 = llm.prompts[0][1], llm.prompts[1][1]
    assert p1.count("### MAIL mail_id=") == 2 and p2.count("### MAIL mail_id=") == 1  # 3 mails, no repeat
    assert "context only" not in p2
    assert "next_free_ids: equipment e1, problems p1, faults f1, action_logs a1" in p1
    assert "next_free_ids: equipment e2, problems p1, faults f2, action_logs a3" in p2
    summary = json.loads(p2[p2.index("{"): p2.index("\n\n## WINDOW")])
    assert [a["action_id"] for a in summary["action_logs"]] == ["a1", "a2"]
    assert summary["action_logs"][1]["outcome"] == "unknown"
    assert summary["faults"][0]["symptom"].startswith("OMD fail alarm")
    assert "evidence_quote" not in p2[: p2.index("## WINDOW")]  # no provenance in the summary
    # The outcome from window 2 still landed on a2 by id.
    assert tmp_store.get_record(mini_casefile.casefile_id)["action_logs"][1]["outcome"]["value"] == "resolved"


def test_default_config_is_five_new_mails_no_overlap():
    """Source defaults, independent of whatever a local .env sets."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2].joinpath("src", "config", "__init__.py").read_text(encoding="utf-8")
    assert '_env_int("EXTRACTION_WINDOW_SIZE", 5)' in src
    assert '_env_int("EXTRACTION_WINDOW_OVERLAP", 0)' in src
    assert '_env("WINDOW_UPDATE_PROMPT_VERSION", "v7")' in src
    assert '_env("CONSOLIDATE_PROMPT_VERSION", "v4")' in src
    assert '_env("PDF_OCR_BACKEND", "vision")' in src


@pytest.mark.parametrize("spec", [WindowSpec(5, 1), WindowSpec(5, 0)])
def test_default_window_never_exceeds_five(spec):
    from src.extraction.windows import windows

    for n in (1, 5, 6, 46, 218):
        assert max(len(w) for w in windows(list(range(n)), spec)) <= 5


async def test_length_cutoff_retries_with_double_budget():
    from src.extraction.window_update import json_call_with_budget
    from src.llm import LLMError

    class Budgeted(FakeLLM):
        def __init__(self):
            super().__init__([])
            self.budgets = []

        async def json_completion(self, prompt, system_message=None, token_tracker=None, **kwargs):
            self.budgets.append(kwargs.get("max_tokens"))
            if kwargs.get("max_tokens", 0) < 4000:
                raise LLMError("Empty response from m (finish_reason='length')")
            return {"ok": True}

    llm = Budgeted()
    assert await json_call_with_budget(llm, "p", "s", None, "w", start=1000, cap=8000) == {"ok": True}
    assert llm.budgets == [1000, 2000, 4000]

    llm = Budgeted()
    with pytest.raises(LLMError, match="length"):
        await json_call_with_budget(llm, "p", "s", None, "w", start=1000, cap=2000)  # cap reached, error surfaces


async def test_window_audit_captures_what_the_model_saw(tmp_store, fake_http, mini_casefile):
    """docs/14: wNNN.json `inputs` + the .prompt.txt sidecar hold what the model was GIVEN."""
    llm = FakeLLM([_window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": [], "notes": ""}])
    r = await _make(tmp_store, fake_http, llm).run(mini_casefile)  # window 2, overlap 1
    assert r.complete
    cid = mini_casefile.casefile_id
    w0 = json.loads(tmp_store.window_path(cid, 0).read_text(encoding="utf-8"))
    w1 = json.loads(tmp_store.window_path(cid, 1).read_text(encoding="utf-8"))

    # mails: every page of the window, in pagenum order, with body hash and fetch flag
    assert [m["mail_id"] for m in w0["inputs"]["mails"]] == [M1, M2] == w0["mail_ids"]
    assert [m["mail_id"] for m in w1["inputs"]["mails"]] == [M2, M3] == w1["mail_ids"]
    assert [m["pagenum"] for m in w1["inputs"]["mails"]] == [1, 2]
    m2_first, m2_again = w0["inputs"]["mails"][1], w1["inputs"]["mails"][0]
    assert m2_first["fetched_now"] is True and m2_again["fetched_now"] is False
    assert m2_first["body_sha1"] == m2_again["body_sha1"] and len(m2_first["body_sha1"]) == 40
    assert m2_first["context_only"] is False and m2_again["context_only"] is True
    assert all(m["skipped_as_twin_of"] is None for m in w0["inputs"]["mails"])
    assert m2_first["subject"].startswith("RE: Main engine OMD") and m2_first["body_chars"] > 0

    # attachments: the logo is junk, the fake PDF is content-bearing listed_only and was sent;
    # window 1 sees both URLs again as cache hits
    att0 = {a["name"]: a for a in m2_first["attachments"]}
    assert att0["image001.png"]["status"] == "junk_skipped" and att0["image001.png"]["sent_to_model"] is False
    pdf = att0["OMD service note.pdf"]
    assert pdf["status"] == "fetched_digested" and pdf["digested_now"] is True
    assert pdf["content_bearing"] is True and pdf["read_method"] == "listed_only" and pdf["sent_to_model"] is True
    assert pdf["digest_as_sent"]["attachment_sha"] == pdf["sha256"] and "url" not in pdf["digest_as_sent"]
    assert "example.invalid" in pdf["url"]  # the audit keeps the URL; the prompt never had it
    att1 = {a["name"]: a for a in m2_again["attachments"]}
    assert att1["OMD service note.pdf"]["status"] == "cache_hit" and att1["OMD service note.pdf"]["digested_now"] is False
    assert att1["image001.png"]["status"] == "junk_skipped"
    assert m2_again["attachments"][0]["url"] == mini_casefile.pages[1].attachments[0]  # URL order kept

    # memory the model could update vs mint
    assert w0["inputs"]["prompt_version"] == w1["inputs"]["prompt_version"]
    assert w0["inputs"]["next_free_ids"] == {"equipment": "e1", "problems": "p1", "faults": "f1", "action_logs": "a1"}
    assert w1["inputs"]["next_free_ids"]["action_logs"] == "a3"
    assert [a["action_id"] for a in w1["inputs"]["record_summary"]["action_logs"]] == ["a1", "a2"]

    # tokens: this window's model calls only (FakeLLM books 100/20 per json call)
    assert w0["inputs"]["tokens"] == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "call_count": 1}
    assert [a["attempt"] for a in w0["inputs"]["attempts"]] == [1]
    assert w0["inputs"]["attempts"][0]["feedback"] == "" and w0["inputs"]["attempts"][0]["error"] is None
    assert w0["inputs"]["attempts"][0]["raw_response"] == w0["raw_response"]

    # the prompt sidecar is byte-identical to what the model received on attempt 1
    for i in (0, 1):
        text = tmp_store.get_window_prompt(cid, i)
        assert text == llm.prompts[i][1]
        assert text.startswith("## RECORD SO FAR") and "## CORRECTION" not in text
        assert json.loads(text[text.index("{"): text.index("\n\n## WINDOW")]) == json.loads(tmp_store.window_path(cid, i).read_text(encoding="utf-8"))["inputs"]["record_summary"]
    assert tmp_store.window_prompt_path(cid, 0).name == "w000.prompt.txt"  # not *.json: the site globs w*.json

    # run_log summary line per window
    wl = tmp_store.get_run_log(cid)["runs"][0]["windows"]
    assert [(x["mails"], x["attachments_sent"], x["tokens"]["total_tokens"]) for x in wl] == [(2, 1, 120), (2, 1, 120)]

    # the record itself carries none of this
    record = tmp_store.get_record(cid)
    assert "inputs" not in record and "tokens" not in record


async def test_window_audit_keeps_both_attempts_of_a_retried_window(tmp_store, fake_http, mini_casefile):
    bad = _window1_delta()
    bad["add"]["faults"][0]["equipment_id"] = "e9"
    llm = FakeLLM([bad, _window1_delta(), _window2_delta(), {"merge": [], "repeat_groups": []}])
    r = await _make(tmp_store, fake_http, llm).run(mini_casefile)
    assert r.windows_rejected == 0 and r.complete
    w0 = json.loads(tmp_store.window_path(mini_casefile.casefile_id, 0).read_text(encoding="utf-8"))
    att = w0["inputs"]["attempts"]
    assert [a["attempt"] for a in att] == [1, 2] and w0["attempts"] == 2
    assert att[0]["error"] and "e9" in att[0]["error"] and att[1]["error"] is None
    assert att[0]["feedback"] == "" and att[1]["feedback"].startswith("\n\n## CORRECTION")
    assert att[1]["prompt_chars"] == att[0]["prompt_chars"] + len(att[1]["feedback"])
    assert att[0]["raw_response"] == bad and att[1]["raw_response"] == w0["raw_response"]
    assert w0["inputs"]["tokens"]["call_count"] == 2
    # the sidecar is attempt 1's prompt, without the feedback
    assert tmp_store.get_window_prompt(mini_casefile.casefile_id, 0) == llm.prompts[0][1]
