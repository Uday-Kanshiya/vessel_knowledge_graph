"""Reviewed broader_than links between symptom groups (docs/12 §3, §5.1): loader validation,
the symptom_broad facet, lookup-page nesting with untouched counts, and the build wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.search.index import build_index, project
from src.site import views
from src.site.render import build, check_links
from src.site.source import DictRecordSource
from src.site.symptom_links import SymptomLinks, load_symptom_links
from tests.unit.test_site_build import RECORD

MAIL = "bbbbbbbbbbbbbbbbbbbbbbb1"


def _record(cid: str, *symptoms: str) -> dict:
    """A minimal complete record with one fault per symptom."""
    rec = json.loads(json.dumps(RECORD))
    rec["casefile_id"] = cid
    rec["faults"] = [
        {
            "fault_id": f"f{i + 1}",
            "symptom": s,
            "alarms": [],
            "equipment_id": "e1",
            "position": None,
            "problem_id": None,
            "suspected_cause": None,
            "first_observed_at": "2026-01-10",
            "provenance": {"mail_ids": [MAIL], "attachment_shas": [], "evidence_quote": s},
        }
        for i, s in enumerate(symptoms)
    ]
    for a in rec["action_logs"]:
        a["fault_id"] = "f1"
    return rec


# Since A1 (docs/13 §8) "knocking sound from exhaust valve" dissolves into the failure mode
# "knocking sound" + component exhaust valve, so the narrower fixture is a wording with no table
# term in it; the record's equipment (e1) is a main_engine, so M/E / main engine leave the key.
BROAD = "knocking sound"
NARROW = "knocking sound at high rpm"
LINK = {"broader": BROAD, "narrower": NARROW, "reviewed_by": "t", "date": "2026-10-21", "note": "n"}

REC_BROAD = _record("c" * 23 + "1", "Knocking sound from M/E cylinder No.3")
REC_NARROW = _record("c" * 23 + "2", "Knocking sound at high rpm, main engine cylinder no. 4.")
REC_OTHER = _record("c" * 23 + "3", "Engine sometimes fails to start")
OTHER = "fail to start"  # P1b: the head noun and the verb form leave the core


def test_fixture_symptoms_normalise_to_the_link_keys():
    assert views.fault_symptom_key(REC_BROAD, REC_BROAD["faults"][0]) == BROAD
    assert views.fault_symptom_key(REC_NARROW, REC_NARROW["faults"][0]) == NARROW
    assert views.fault_symptom_key(REC_OTHER, REC_OTHER["faults"][0]) == OTHER


# --------------------------------------------------------------------------- loader


def test_loader_reads_file_and_checks_shape(tmp_path: Path):
    p = tmp_path / "links.json"
    p.write_text(json.dumps([LINK]), encoding="utf-8")
    assert load_symptom_links(p) == [LINK]
    assert load_symptom_links(tmp_path / "missing.json") == []
    p.write_text(json.dumps([{"broader": BROAD}]), encoding="utf-8")
    with pytest.raises(ValueError, match="narrower"):
        load_symptom_links(p)
    p.write_text(json.dumps([{"broader": BROAD, "narrower": BROAD}]), encoding="utf-8")
    with pytest.raises(ValueError, match="itself"):
        load_symptom_links(p)


def test_validate_drops_links_with_a_missing_side_and_warns():
    with pytest.raises(ValueError, match="two broader"):
        SymptomLinks([LINK, {"broader": "vibration", "narrower": NARROW}])
    links = SymptomLinks([LINK, {"broader": "vibration", "narrower": "vibration at bearing"}])
    links.validate({BROAD, NARROW})
    assert len(links) == 1 and links.broader_of == {NARROW: BROAD}
    assert len(links.warnings) == 1 and "vibration" in links.warnings[0]
    assert not SymptomLinks([LINK]).validate({"x"})


def test_cycles_and_multiple_broader_are_errors():
    with pytest.raises(ValueError, match="cycle"):
        SymptomLinks([{"broader": "a", "narrower": "b"}, {"broader": "b", "narrower": "a"}])
    with pytest.raises(ValueError, match="cycle"):
        SymptomLinks(
            [
                {"broader": "a", "narrower": "b"},
                {"broader": "b", "narrower": "c"},
                {"broader": "c", "narrower": "a"},
            ]
        )
    with pytest.raises(ValueError, match="two broader"):
        SymptomLinks([{"broader": "a", "narrower": "c"}, {"broader": "b", "narrower": "c"}])
    # the same link twice is fine
    assert len(SymptomLinks([LINK, dict(LINK)])) == 1


def test_ancestors_are_transitive():
    links = SymptomLinks([{"broader": "a", "narrower": "b"}, {"broader": "b", "narrower": "c"}])
    assert links.ancestors("c") == ["b", "a"]
    assert links.ancestors("a") == []
    assert links.widen(["c", "z"]) == ["a", "b", "c", "z"]
    assert links.narrower("a") == ["b"] and links.depth("c") == 2


# --------------------------------------------------------------------------- index facet


def test_symptom_broad_widens_only_upwards():
    links = SymptomLinks([LINK])
    narrow = project(REC_NARROW, links)
    broad = project(REC_BROAD, links)
    assert narrow["facets"]["symptom"] == [NARROW]  # the facet itself is unchanged
    assert narrow["facets"]["symptom_broad"] == [BROAD, NARROW]  # a click on BROAD reaches it
    assert broad["facets"]["symptom_broad"] == [BROAD]  # a click on NARROW does not reach it
    assert project(REC_OTHER, links)["facets"]["symptom_broad"] == [OTHER]
    assert project(REC_NARROW)["facets"]["symptom_broad"] == [NARROW]  # no links: same as symptom
    idx = build_index([REC_BROAD, REC_NARROW], links)
    assert idx["facet_match"] == {"symptom": "symptom_broad"}
    # facet count semantics: results you get if you click it
    hits = lambda term: [d["id"] for d in idx["docs"] if term in d["facets"]["symptom_broad"]]
    assert hits(BROAD) == [REC_BROAD["casefile_id"], REC_NARROW["casefile_id"]]
    assert hits(NARROW) == [REC_NARROW["casefile_id"]]


# --------------------------------------------------------------------------- lookup page


def test_lookup_nests_narrower_under_broader_without_rolling_counts():
    records = [REC_OTHER, REC_NARROW, REC_BROAD, REC_BROAD]
    flat = views.lookup_tables(records)["symptoms"]
    nested = views.lookup_tables(records, SymptomLinks([LINK]))["symptoms"]
    by_norm_flat = {r["norm"]: (r["n_raw"], r["total"]) for r in flat}
    by_norm_nested = {r["norm"]: (r["n_raw"], r["total"]) for r in nested}
    assert by_norm_flat == by_norm_nested  # exactly the same counts per group
    assert by_norm_nested[BROAD] == (1, 2) and by_norm_nested[NARROW] == (1, 1)
    norms = [r["norm"] for r in nested]
    assert norms.index(NARROW) == norms.index(BROAD) + 1  # listed directly under its broader row
    broad_row = nested[norms.index(BROAD)]
    narrow_row = nested[norms.index(NARROW)]
    assert (broad_row["depth"], broad_row["n_narrower"], broad_row["broader"]) == (0, 1, None)
    assert (narrow_row["depth"], narrow_row["broader"], narrow_row["note"]) == (1, BROAD, "n")
    assert all(r["depth"] == 0 for r in flat)


def test_lookup_without_the_broader_group_present_keeps_narrower_top_level():
    links = SymptomLinks([LINK]).validate({NARROW})  # BROAD not in this build -> dropped
    rows = views.lookup_tables([REC_NARROW], links)["symptoms"]
    assert rows[0]["norm"] == NARROW and rows[0]["depth"] == 0 and rows[0]["broader"] is None


# --------------------------------------------------------------------------- build


def test_build_wires_links_into_index_and_lookup_page(tmp_path: Path):
    src = DictRecordSource({r["casefile_id"]: r for r in (REC_BROAD, REC_NARROW, REC_OTHER)})
    bad = {"broader": "vibration", "narrower": "vibration at bearing"}
    summary = build(src, tmp_path / "site", symptom_links=[LINK, bad])
    assert summary["symptom_links"] == 1 and len(summary["symptom_link_warnings"]) == 1
    assert check_links(tmp_path / "site") == []
    idx = json.loads((tmp_path / "site" / "search_index.json").read_text(encoding="utf-8"))
    by_id = {d["id"]: d for d in idx["docs"]}
    assert by_id[REC_NARROW["casefile_id"]]["facets"]["symptom_broad"] == [BROAD, NARROW]
    page = (tmp_path / "site" / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "⊂ knocking sound" in page and 'class="narrower"' in page
    assert 'n <span class="muted">(t, 2026-10-21)</span>' in page  # the note column
    assert "vibration" in page  # the dropped link is reported, not silently lost
    app = (tmp_path / "site" / "assets" / "app.js").read_text(encoding="utf-8")
    assert "symptom_broad" in app


def test_repo_config_file_loads_and_links_exist_in_normaliser_terms():
    root = Path(__file__).resolve().parents[2]
    rows = load_symptom_links(root / "config" / "symptom_links.json")
    # the reviewed example (knocking sound > knocking sound from exhaust valve) was RETIRED on
    # 2026-10-25: its narrower key dissolved under A1 (13 §8); the file may be empty now
    assert isinstance(rows, list)
    SymptomLinks(rows)  # structurally valid: no cycles, one broader per narrower
    for r in rows:
        for side in ("broader", "narrower"):
            assert views.normalise_symptom(r[side]) == r[side], (
                f"{r[side]!r} is not a normalised symptom string as the lookup page prints it"
            )
