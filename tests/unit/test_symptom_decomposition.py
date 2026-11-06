"""
Symptom decomposition (docs/13 §8, A1; docs/16 P1): the three knocking wordings are one standard
with the component as a part, the guard queues a label that still carries a known term, only the
record's own equipment words are stripped, a dissolved link is retired and logged, residue tokens
seen in two casefiles become candidate terms, and the ON column / fault-row display (golden).
The per-wording cases live in resolution_cases.csv (test_resolution_cases.py).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from src.resolution import LookupStore, load_config
from src.resolution.lookup import symptom_guard
from src.resolution.normalize import decompose_symptom, equipment_forms
from src.site import views
from src.site.render import build, check_links
from src.site.source import DictRecordSource
from src.site.symptom_links import SymptomLinks, load_symptom_links, retire_dissolved
from tests.unit.test_site_build import RECORD

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
CTX = {"equipment_type": "main_engine"}


def _store() -> LookupStore:
    return LookupStore(config_dir=CONFIG_DIR, guard=symptom_guard)


def _rec_with(symptoms: list[str], etype: str = "main_engine") -> dict:
    rec = json.loads(json.dumps(RECORD))
    rec["equipment"][0]["equipment_type"] = etype
    base = rec["faults"][0]
    rec["faults"] = [dict(base, fault_id=f"f{i + 1}", symptom=s) for i, s in enumerate(symptoms)]
    for a in rec["action_logs"]:
        a["fault_id"] = "f1"
    return rec


def test_three_knocking_wordings_are_one_standard_with_the_component_as_a_part():
    s = _store()
    ids = {
        s.resolve(w, kind="symptom", source="casefile:a", context=CTX).standard_id
        for w in (
            "Knocking sound from main engine exhaust valve",
            "Knocking sound from exhaust valve",
            "Knocking sound",
        )
    }
    assert len(ids) == 1
    sid = ids.pop()
    assert s.display_name(sid) == "Knocking sound"
    part = s.find("exhaust valve", kind="part")
    assert part is not None and part.method == "alias"
    # the same part id an action_logs line gets
    assert s.resolve("exhaust valves", kind="part").standard_id == part.standard_id
    rows = {a["raw_term"]: a["parts"] for a in s.alias_rows("symptom")}
    assert rows["Knocking sound"] == {"core": "knocking sound"}
    assert rows["Knocking sound from exhaust valve"]["component_standard_id"] == part.standard_id
    assert [r["method_chain"] for r in s.log(kind="part")] == [
        "symptom_component+new_term",
        "normalised",  # the explicit "exhaust valves" resolve above
    ]
    assert [r["method_chain"] for r in s.log(kind="symptom")] == [
        "equipment_strip+tail+component+normalised",
        "tail+component+normalised",
        "normalised",
    ]


def test_guard_queues_a_label_that_still_carries_a_known_term():
    s = _store()
    label = "Bearing damage found at the cylinder liner, piston ring scuffed"
    # two components: the tail one wins the slot, the other stays in the core -> queued
    r = s.resolve(label, kind="symptom", context=CTX)
    assert r.method == "queued" and r.reason == "undecomposed" and r.standard_id is None
    p = s.pending("symptom")[0]
    assert p.parts["component"] == "cylinder liner"
    assert set(p.parts["leftover"]) == {"bearing", "piston ring"}
    assert s.standards(kind="symptom") == []  # nothing minted
    assert s.find(label, kind="symptom", context=CTX) is None
    # without the guard the same label is minted: the guard is the only thing that refuses
    assert (
        LookupStore(config_dir=CONFIG_DIR).resolve(label, kind="symptom", context=CTX).method
        == "new_term"
    )
    # a clean decomposition is never refused
    assert (
        s.resolve("Knocking sound from exhaust valve", kind="symptom", context=CTX).method
        == "new_term"
    )


def test_equipment_forms_and_only_the_record_type_is_stripped():
    forms = equipment_forms("main_engine")
    assert (
        ("main", "engine") in forms
        and ("m/e",) in forms
        and ("me",) in forms
        and ("m", "e") in forms
    )
    assert equipment_forms(None) == [] and equipment_forms("scr_system")[0] == ("scr", "system")
    d = decompose_symptom("Purifier knocking sound from main engine", "purifier")
    assert d.core == "knocking sound from main engine"  # not the record's type: stays
    d = decompose_symptom("Purifier knocking sound from main engine", "main_engine")
    assert d.core == "purifier knocking sound"
    # the fleet vessel name goes, whole name only
    assert "oasis" in load_config(CONFIG_DIR).vessel_names
    d = decompose_symptom("OASIS M/E knocking sound", "main_engine")
    assert (d.core, d.vessel, list(d.steps)) == (
        "knocking sound",
        "oasis",
        ["vessel_strip", "equipment_strip"],
    )
    # the lemma map is explicit, not a stemmer: bearing / casing keep their nouns
    d = decompose_symptom("Bearing cracked and leaking", None)
    assert (d.core, d.component) == ("crack and leak", "bearing")
    assert decompose_symptom("Casing fractured", None).component == "casing"


def test_retired_link_is_logged_and_absent(tmp_path):
    links = tmp_path / "links.json"
    links.write_text(
        json.dumps(
            [
                {
                    "broader": "knocking sound",
                    "narrower": "knocking sound from exhaust valve",
                    "reviewed_by": "r",
                    "date": "2026-10-21",
                    "note": "valve is the localised component",
                },
                {"broader": "knocking sound", "narrower": "knocking sound at high rpm"},
            ]
        ),
        encoding="utf-8",
    )
    s = _store()
    for w in ("Knocking sound", "Knocking sound from exhaust valve", "Knocking sound at high rpm"):
        s.resolve(w, kind="symptom", source="casefile:a", context=CTX)
    retired = retire_dissolved(links, s)
    assert [r["narrower"] for r in retired] == ["knocking sound from exhaust valve"]
    assert retired[0]["rationale"] == (
        "dissolved into 'knocking sound' + component 'exhaust valve'; "
        "note: valve is the localised component; reviewed_by: r 2026-10-21"
    )
    kept = load_symptom_links(links)
    assert [r["narrower"] for r in kept] == ["knocking sound at high rpm"]
    row = [r for r in s.log(kind="symptom") if r["method_chain"] == "link_retired"]
    assert len(row) == 1 and row[0]["before"] == "knocking sound from exhaust valve"
    assert row[0]["after"] == "knocking sound"
    assert row[0]["standard_id"] == s.find_key("knocking sound", "symptom")
    assert row[0]["rationale"] == retired[0]["rationale"]
    sl = SymptomLinks(kept).validate({x.key for x in s.standards(kind="symptom")})
    assert sl.warnings == [] and len(sl) == 1
    assert retire_dissolved(links, s) == []  # idempotent
    assert retire_dissolved(tmp_path / "missing.json", s) == []


def _build_lookup_module():
    spec = importlib.util.spec_from_file_location(
        "build_lookup", ROOT / "scripts" / "build_lookup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_candidate_terms_are_queued_from_residue_seen_in_two_casefiles():
    mod = _build_lookup_module()
    s = _store()
    s.resolve("Vibration at the thrust block", kind="symptom", source="casefile:a", context=CTX)
    s.resolve("Noise at the thrust block", kind="symptom", source="casefile:b", context=CTX)
    s.resolve("Leak at the gasket", kind="symptom", source="casefile:b", context=CTX)
    st = mod.decomposition_stats(s)
    assert (st["labels"], st["split"], st["whole"], st["with_residue"], st["queued"]) == (
        3,
        0,
        3,
        3,
        0,
    )
    assert st["residue"][:2] == [("block", 2), ("thrust", 2)] and ("gasket", 1) in st["residue"]
    assert mod.queue_candidate_terms(s, 2) == ["block", "thrust"]
    pend = {p.raw_term: p for p in s.pending("symptom")}
    assert set(pend) == {"block", "thrust"} and pend["block"].reason == "candidate_term"
    assert pend["block"].parts == {
        "token": "block",
        "casefiles": ["casefile:a", "casefile:b"],
        "propose": "component_terms|location_terms",
    }
    assert mod.queue_candidate_terms(s, 3) == []
    assert s.find("block", kind="symptom") is None  # a candidate is not a standard
    assert s.fragmentation()["by_kind"]["symptom"]["n_pending"] == 2


def test_lookup_page_on_column_and_fault_row_golden(tmp_path):
    rec = _rec_with(
        [
            "Knocking sound from main engine exhaust valve cylinder no. 4.",
            "Knocking sound from exhaust valve",
            "Knocking sound",
            "Bolts of the support shaft cover plate for the starboard main engine idle gear were found fractured",
        ]
    )
    s = _store()
    for f in rec["faults"]:
        s.resolve(
            f["symptom"],
            kind="symptom",
            source="casefile:" + rec["casefile_id"],
            context=views.fault_context(rec, f),
        )
    r = views.resolver_tables(s, [rec])
    std = [x for g in r["symptoms"] for x in g["standards"] if x["key"] == "knocking sound"]
    assert len(std) == 1 and std[0]["on"] == [
        {"component": "exhaust valve", "location": None, "modifiers": "", "condition": None, "frequency": None, "n": 2}
    ]
    assert r["pending_counts"]["symptom"] == 1  # the bolts label is queued, not a standard
    out = tmp_path / "site"
    build(DictRecordSource({rec["casefile_id"]: rec}), out, lookup=s)
    assert check_links(out) == []
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "<th>on / when</th>" in page
    assert (
        '<td class="variants on"><span>exhaust valve <span class="n">(2)</span></span></td>' in page
    )
    # the casefile page's fault rows show core · on component
    cf = (out / "casefile" / f"{rec['casefile_id']}.html").read_text(encoding="utf-8")
    assert cf.count('<div class="small muted decomposed">') == 3  # not the bare "Knocking sound"
    assert (
        '<div class="small muted decomposed"><strong>Knocking sound</strong> · on exhaust valve</div>'
        in cf
    )
    parts = views.fault_decomposition(rec, s)
    assert set(parts) == {"f1", "f2", "f4"}
    assert parts["f4"] == {
        "core": "support shaft cover plate for the starboard idle gear were found fractured",
        "component": "bolt",
        "location": None,
        "modifiers": "",
        "condition": None,
        "frequency": None,
    }
    # without a resolver the canonical key stands in for the display name
    assert views.fault_decomposition(rec)["f1"]["component"] == "exhaust valve"
    # a location shows as "in <location>"
    rec2 = _rec_with(
        ["Unit No. 7 fuel pump spring found broken, with a groove in the pump casing."],
        "auxiliary_engine",
    )
    d = views.fault_decomposition(rec2)["f1"]
    assert (d["core"], d["component"], d["location"]) == (
        "found broken with a groove",
        "fuel pump spring",
        "pump casing",
    )
