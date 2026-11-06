"""
P1b (docs/13 §8, docs/17 §6): HOW the failure is said and WHEN / HOW OFTEN it shows leave the
symptom core — verb-phrase canonicalisation (config/symptom_phrases.csv), condition and frequency
parts, "when" prepositions, the bare head noun of the record's equipment, the guard extended to
all of it — and the read-only near-duplicates scan (Phase 1) with its three verdicts.
The per-wording cases live in resolution_cases.csv (test_resolution_cases.py).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.resolution import LookupStore
from src.resolution.lookup import DEFAULT_GUARD
from src.resolution.near_duplicates import (
    THREAD_BAR,
    lexical_score,
    render_json,
    render_text,
    scan,
    tag_tokens,
    verdict_of,
)
from src.resolution.normalize import (
    LEMMA_MAP,
    SYMPTOM_PREPOSITIONS,
    decompose_symptom,
    head_noun_of,
    load_config,
    unmapped_inflection,
)
from src.site import views
from src.site.render import build, check_links
from src.site.source import DictRecordSource
from tests.unit.test_site_build import RECORD

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
ME = {"equipment_type": "main_engine"}
AE = {"equipment_type": "auxiliary_engine"}


def _store() -> LookupStore:
    return LookupStore(config_dir=CONFIG_DIR, guard=DEFAULT_GUARD)


def _families(store: LookupStore) -> None:
    store.import_families_csv(CONFIG_DIR / "symptom_families.csv")


# --------------------------------------------------------------------------- decomposition


def test_three_fail_to_start_wordings_are_one_standard_with_when_on_the_alias_rows():
    s = _store()
    r1 = s.resolve("No.1 AE could not start", kind="symptom", source="casefile:a", context=AE)
    r2 = s.resolve(
        "Engine sometimes fails to start in dead slow",
        kind="symptom",
        source="casefile:b",
        context=ME,
    )
    r3 = s.resolve(
        "M/E failed to start during approaching berth",
        kind="symptom",
        source="casefile:c",
        context=ME,
    )
    assert r1.key == r2.key == r3.key == "fail to start"
    assert r1.standard_id == r2.standard_id == r3.standard_id
    assert s.display_name(r1.standard_id) == "fail to start"
    parts = {a["raw_term"]: a["parts"] for a in s.alias_rows("symptom")}
    assert parts["No.1 AE could not start"]["position"] == "no.1"
    assert parts["No.1 AE could not start"]["phrases"] == ["could not start"]
    p2 = parts["Engine sometimes fails to start in dead slow"]
    assert (p2["condition"], p2["frequency"], p2["head_noun"]) == (
        "dead slow",
        "intermittent",
        "engine",
    )
    assert parts["M/E failed to start during approaching berth"]["condition"] == "approaching berth"
    chains = [r["method_chain"] for r in s.log(kind="symptom")]
    assert chains[0] == "position+equipment_strip+phrase+normalised"
    assert "condition" in chains[1] and "frequency" in chains[1] and "head_noun" in chains[1]


def test_lo_sump_pair_is_one_standard_and_knocking_vs_vibrating_are_two():
    s = _store()
    a = s.resolve("LO sump high/low alarm not activating", kind="symptom", context=AE)
    b = s.resolve("LO sump level HIGH/LOW alarm not activating", kind="symptom", context=AE)
    assert a.standard_id == b.standard_id and a.key == "lo high/low alarm fail to activate"
    assert {x["parts"]["location"] for x in s.alias_rows("symptom")} == {"sump"}
    k = s.resolve("Knocking sound", kind="symptom", context=ME)
    v = s.resolve("Vibrating sound", kind="symptom", context=ME)
    assert k.standard_id != v.standard_id and (k.key, v.key) == (
        "knocking sound",
        "vibrating sound",
    )


def test_battery_dead_keeps_dead_and_engine_room_stays_a_location():
    d = decompose_symptom("Battery dead", None)
    assert (d.core, d.condition) == ("battery dead", None)
    d = decompose_symptom("Oil leak in engine room", "main_engine")
    assert (d.core, d.location, d.head_noun) == ("oil leak", "engine room", None)
    d = decompose_symptom("Engine stalls in port", "main_engine")
    assert (d.core, d.condition, d.head_noun) == ("stall", "in port", "engine")
    assert head_noun_of("main_engine") == "engine" and head_noun_of("scr_system") is None
    # the head noun is stripped only for the record's own type
    assert decompose_symptom("Engine stalls", "purifier").core == "engine stall"
    # a one-word condition row that is a plain symptom word is refused at load
    with pytest.raises(ValueError, match="plain symptom words"):
        from src.resolution.normalize import load_condition_terms

        p = ROOT / "tests" / "fixtures" / "bad_condition_terms.csv"
        p.write_text("term,canonical\ndead,dead\n", encoding="utf-8")
        try:
            load_condition_terms(p)
        finally:
            p.unlink()


def test_when_prepositions_and_number_patterns():
    assert {"during", "while", "when", "after", "before", "since"} <= SYMPTOM_PREPOSITIONS
    d = decompose_symptom("Fuel plunger too high alarm only appears above 80 rpm.", "main_engine")
    assert (d.component, d.condition, d.core) == (
        "fuel plunger",
        "above 80 rpm",
        "too high alarm only appear",
    )
    assert (
        decompose_symptom("Engine runs rough at 80% load", "main_engine").condition == "at 80 load"
    )
    # an unknown "when" tail stays whole and reaches residue (invisible before P1b)
    d = decompose_symptom("Knocking sound during harbour generator test", "main_engine")
    assert d.core == "knocking sound during harbour generator test" and d.component is None
    assert set(d.residue) == {"harbour", "generator", "test"} and d.condition is None


def test_unmapped_verb_form_passes_through_and_is_residue(monkeypatch):
    assert LEMMA_MAP["failed"] == "fail" and LEMMA_MAP["activating"] == "activate"
    assert unmapped_inflection("failed") is None and unmapped_inflection("fails") is None
    assert unmapped_inflection("surging") is None  # "surge" is not a mapped verb: not an inflection
    monkeypatch.setitem(LEMMA_MAP, "surged", "surge")  # a base with ONE listed form
    import importlib

    nz = importlib.import_module(
        "src.resolution.normalize"
    )  # the package exports a function of that name
    monkeypatch.setattr(nz, "_LEMMA_BASES", frozenset(LEMMA_MAP.values()))
    assert unmapped_inflection("surging") == "surge"
    d = decompose_symptom("Pressure surging at high load", "main_engine")
    assert d.core == "pressure surging" and d.condition == "high load"
    assert list(d.residue) == ["surging"] and list(d.leftover) == ["surging"]
    s = _store()
    assert s.resolve("Pressure surging at high load", kind="symptom", context=ME).method == "queued"


def test_guard_covers_phrase_condition_and_frequency_leftovers():
    d = decompose_symptom("Fails to start in dead slow and in port", "main_engine")
    # two conditions, one slot: the tail ("in port") fills it, "dead slow" stays -> leftover
    assert d.condition == "in port" and "dead slow" in d.leftover
    s = _store()
    assert (
        s.resolve("Fails to start in dead slow and in port", kind="symptom", context=ME).method
        == "queued"
    )
    d = decompose_symptom("Sometimes and always knocking sound", "main_engine")
    assert d.frequency == "intermittent" and "persistent" in d.leftover


# --------------------------------------------------------------------------- near duplicates


def test_lexical_score_is_the_thread_stand_in():
    assert lexical_score("fail to start", "fail to start") == 1.0
    assert 0 <= lexical_score("knocking sound", "water leak") < THREAD_BAR
    assert (
        lexical_score(
            "lo high/low alarm fail to activate", "lo level high/low alarm fail to activate"
        )
        >= THREAD_BAR
    )


def test_tagging_and_verdicts_on_a_fixture():
    cfg = load_config(CONFIG_DIR)
    pats = ["knock", "noise", "vibrat", "shaking", "leak"]
    # modality + verb form -> every differing token tagged -> MERGE-BY-TABLE
    diff = tag_tokens("could not start", "failed to start", cfg, pats)
    tags = {(d["side"], d["token"]): d["tag"] for d in diff}
    assert tags[("a", "could")] == "modality" and tags[("a", "not")] == "modality"
    assert tags[("b", "failed")] == "modality" or tags[("b", "failed")] == "verb_form"
    verdict, tables = verdict_of(diff)
    assert verdict == "MERGE-BY-TABLE" and "symptom_phrases.csv" in " ".join(tables)
    # a failure-mode word differs -> KEEP
    verdict, _ = verdict_of(tag_tokens("knocking sound", "vibrating sound", cfg, pats))
    assert verdict == "KEEP"
    # an unexplained word -> REVIEW
    diff = tag_tokens("fail to start", "fail to start xyzzy", cfg, pats)
    assert [d["tag"] for d in diff] == ["unknown"] and verdict_of(diff)[0] == "REVIEW"
    # where / condition / frequency / head noun tags
    diff = tag_tokens("fail to start exhaust valve", "fail to start sometime", cfg, pats)
    assert {(d["token"], d["tag"]) for d in diff} == {
        ("exhaust", "where"),
        ("valve", "where"),
        ("sometime", "frequency"),
    }
    diff = tag_tokens("engine fail to start during manoeuvring", "fail to start", cfg, pats)
    assert {(d["token"], d["tag"]) for d in diff} == {
        ("engine", "head_noun"),
        ("during", "condition"),
        ("manoeuvring", "condition"),
    }


def test_scan_prints_exactly_the_three_verdicts_on_a_fixture_store(tmp_path):
    s = LookupStore(config_dir=CONFIG_DIR)  # no guard: the raw cores must be minted as they are
    fam = tmp_path / "fam.csv"
    fam.write_text(
        "key,display,description,patterns,sort_order\n"
        "fails_to_start,Fails to start,,fail to start|could not start,1\n"
        "abnormal_noise,Abnormal noise,,knock|vibrat,2\n",
        encoding="utf-8",
    )
    s.import_families_csv(fam)
    # pre-P1b cores inserted as they were (set_alias would canonicalise them through the cleaner,
    # which is exactly what P1b adds), so the backlog shapes exist side by side
    fam_id = {f.key: f.id for f in s.families("symptom")}
    for raw, key, fam in (
        ("fails to start", "fail to start", "fails_to_start"),
        ("M/E failed to start", "failed to start", "fails_to_start"),
        (
            "Knocking sound from exhaust valve",
            "knocking sound from exhaust valve",
            "abnormal_noise",
        ),
        (
            "Vibrating sound from exhaust valve",
            "vibrating sound from exhaust valve",
            "abnormal_noise",
        ),
        ("fails to start xyzzy", "fail to start xyzzy", "fails_to_start"),
    ):
        sid = s._mint("symptom", key, "rule", "2026-10-26T00:00:00+00:00")
        s.db.execute(
            "INSERT INTO alias (kind, raw_key, raw_term, stem, standard_id, method, source, first_seen, "
            "last_seen, hit_count) VALUES ('symptom', ?, ?, ?, ?, 'new_term', 'casefile:t', 'x', 'x', 1)",
            (raw.lower(), raw, key, sid),
        )
        s.db.execute(
            "UPDATE standard_term SET family_id=?, family_method='seed' WHERE id=?",
            (fam_id[fam], sid),
        )
    s.db.commit()
    # a pending (undecomposed) spelling one frequency row away from a standard
    s.queue(
        "symptom",
        "Engine fails to start sometimes",
        "undecomposed",
        parts={"core": "fail to start sometime"},
    )
    rep = scan(s)
    verdicts = {frozenset((p["a"]["key"], p["b"]["key"])): p["verdict"] for p in rep["pairs"]}
    assert (
        verdicts[frozenset(("fail to start", "failed to start"))] == "MERGE-BY-TABLE"
    )  # verb_form
    assert (
        verdicts[
            frozenset(("knocking sound from exhaust valve", "vibrating sound from exhaust valve"))
        ]
        == "KEEP"
    )
    assert verdicts[frozenset(("fail to start", "fail to start xyzzy"))] == "REVIEW"
    assert verdicts[frozenset(("failed to start", "fail to start xyzzy"))] == "REVIEW"
    assert rep["counts"] == {"MERGE-BY-TABLE": 1, "REVIEW": 2, "KEEP": 1}
    pend = {p["b"]["key"]: p["verdict"] for p in rep["pending_pairs"]}
    assert pend == {
        "fail to start": "MERGE-BY-TABLE",
        "failed to start": "MERGE-BY-TABLE",
        "fail to start xyzzy": "REVIEW",
    }
    assert rep["pending_pairs"][0]["a"]["pending"] is True
    text = render_text(rep)
    assert (
        text.count("[MERGE-BY-TABLE") == 3
        and text.count("[KEEP]") == 1
        and text.count("[REVIEW") == 3
    )
    assert "b:failed=verb_form" in text and "a:sometime=frequency" in text
    assert "a:knocking=failure_mode" in text and "b:xyzzy=unknown" in text
    # the lexical bar is what it is: a modality pair ("could not start" / "fail to start") scores
    # below 0.72 and is not listed — the family groups it, Phase 2 merges it by table
    assert lexical_score("could not start", "fail to start") < THREAD_BAR
    assert json.loads(render_json(rep))["bar"] == THREAD_BAR
    only = scan(s, family="abnormal_noise")
    assert [p["verdict"] for p in only["pairs"]] == ["KEEP"] and only["families_scanned"] == [
        "abnormal_noise"
    ]


def test_phase1_lists_nothing_mergeable_after_the_fix():
    """Any pair whose verdict is MERGE-BY-TABLE after P1b is a bug in P1b."""
    s = _store()
    _families(s)
    for raw, ctx in (
        ("No.1 AE could not start", AE),
        ("Engine sometimes fails to start in dead slow", ME),
        ("M/E failed to start during approaching berth", ME),
        ("LO sump high/low alarm not activating", AE),
        ("LO sump level HIGH/LOW alarm not activating", AE),
        ("Knocking sound", ME),
        ("Vibrating sound", ME),
    ):
        s.resolve(raw, kind="symptom", source="casefile:x", context=ctx)
    s.assign_families("symptom")
    rep = scan(s)
    assert rep["counts"]["MERGE-BY-TABLE"] == 0
    assert (
        len(s.standards(kind="symptom")) == 4
    )  # fail to start, lo ... fail to activate, knocking, vibrating


def test_review_queue_four_and_review_page_golden(tmp_path):
    rec = json.loads(json.dumps(RECORD))
    rec["faults"][0]["symptom"] = "Engine sometimes fails to start in dead slow"
    s = _store()
    _families(s)
    # a pre-P1b standard as it was minted, next to the P1b one the record resolves to
    sid = s._mint("symptom", "failed to start", "rule", "2026-10-26T00:00:00+00:00")
    s.db.execute(
        "INSERT INTO alias (kind, raw_key, raw_term, stem, standard_id, method, source, first_seen, "
        "last_seen, hit_count) VALUES ('symptom', 'm e failed to start', 'M/E failed to start', "
        "'failed to start', ?, 'new_term', 'casefile:t', 'x', 'x', 1)",
        (sid,),
    )
    s.db.commit()
    s.resolve(
        rec["faults"][0]["symptom"],
        kind="symptom",
        source="casefile:" + rec["casefile_id"],
        context=views.fault_context(rec, rec["faults"][0]),
    )
    s.assign_families("symptom")
    r = views.resolver_tables(s, [rec])
    dup = r["duplicates"]
    assert dup["counts"]["MERGE-BY-TABLE"] == 1 and dup["pairs"][0]["verdict"] == "MERGE-BY-TABLE"
    on = next(x for g in r["symptoms"] for x in g["standards"] if x["key"] == "fail to start")["on"]
    assert on == [
        {
            "component": None,
            "location": None,
            "modifiers": "",
            "condition": "dead slow",
            "frequency": "intermittent",
            "n": 1,
        }
    ]
    out = tmp_path / "site"
    build(DictRecordSource({rec["casefile_id"]: rec}), out, lookup=s)
    assert check_links(out) == []
    review = (out / "admin" / "review.html").read_text(encoding="utf-8")
    assert (
        "Possible duplicates" in review
        and '<span class="chip warn">MERGE-BY-TABLE</span>' in review
    )
    assert "failed=<span" in review and "verb_form</span>" in review  # the tagged diff
    lookup = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "<th>on / when</th>" in lookup
    assert (
        '<td class="variants on"><span>when dead slow · intermittent <span class="n">(1)</span></span></td>'
        in lookup
    )
    cf = (out / "casefile" / f"{rec['casefile_id']}.html").read_text(encoding="utf-8")
    assert (
        '<div class="small muted decomposed"><strong>fail to start</strong> · when dead slow · intermittent</div>'
        in cf
    )
    # build_lookup --review prints it as queue 4
    spec = importlib.util.spec_from_file_location(
        "build_lookup", ROOT / "scripts" / "build_lookup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.print_review(s)
    assert (
        "review queue 4 — possible duplicates" in buf.getvalue()
        and "[MERGE-BY-TABLE" in buf.getvalue()
    )


def test_condition_sub_facet_appears_only_with_two_casefiles(tmp_path):
    from src.search.index import PARENTS, build_index

    recs = []
    for i, wording in enumerate(
        (
            "Engine sometimes fails to start in dead slow",
            "M/E failed to start in dead slow",
            "Knocking sound",
        )
    ):
        rec = json.loads(json.dumps(RECORD))
        rec["casefile_id"] = chr(ord("h") + i) * 24
        rec["faults"][0]["symptom"] = wording
        recs.append(rec)
    s = _store()
    _families(s)
    for rec in recs:
        s.resolve(
            rec["faults"][0]["symptom"],
            kind="symptom",
            context=views.fault_context(rec, rec["faults"][0]),
        )
    s.assign_families("symptom")
    idx = build_index(recs, None, s)
    assert idx["parents"] == {**PARENTS, "condition": "symptom_family"}
    assert (
        idx["docs"][0]["facets"]["condition"] == ["dead slow"]
        and idx["docs"][2]["facets"]["condition"] == []
    )
    assert idx["facet_parent_vals"]["condition"]["dead slow"] == ["fails_to_start"]
    idx1 = build_index(recs[1:], None, s)  # one casefile with a condition: no sub-facet
    assert idx1["parents"] == PARENTS and "condition" not in idx1["docs"][0]["facets"]
