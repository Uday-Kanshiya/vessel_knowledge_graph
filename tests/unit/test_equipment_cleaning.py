"""
Equipment-label hygiene (docs/13 §3–§4, §6; docs/16 P2): vessel strip (whole name, sister ship
logged), glossary + adjudicator, record strip, qualifiers, the four guard reasons, --check, the
position view, and the lookup page's chain / stripped column (golden). Per-label cases live in
resolution_cases.csv (test_resolution_cases.py).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.resolution import LookupStore
from src.resolution.lookup import DEFAULT_GUARD, equipment_guard
from src.resolution.normalize import (
    NOISE_WORDS,
    clean_equipment_label,
    has_non_latin,
    load_config,
    load_equipment_glossary,
    normalize,
)
from src.site import views
from src.site.render import build, check_links
from src.site.source import DictRecordSource
from tests.unit.test_site_build import RECORD

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _config_dir(tmp_path: Path, vessels: str = "ENGINE STAR\nOASIS\n") -> Path:
    """A config dir with the repo tables and a hand-written fleet list."""
    for name in (
        "modifier_terms.csv",
        "equipment_glossary.csv",
        "equipment_qualifiers.csv",
        "component_terms.csv",
        "location_terms.csv",
    ):
        (tmp_path / name).write_text(
            (CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / "vessel_names.txt").write_text("# test\n" + vessels, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------- the cleaner


def test_vessel_strip_is_whole_name_only_and_logs_a_sister_ship(tmp_path):
    cfg = load_config(_config_dir(tmp_path))
    assert "engine star" in cfg.vessel_names
    e = clean_equipment_label("ENGINE STAR M/E", {"vessel_name": "ENGINE STAR"}, cfg)
    assert (e.core, e.vessel, e.sister_vessel, e.stripped) == (
        "main engine",
        "engine star",
        None,
        {"vessel": "ENGINE STAR"},
    )
    # "engine" alone is not the vessel: nothing is removed from a plain name
    e = clean_equipment_label("Main engine", {"vessel_name": "ENGINE STAR"}, cfg)
    assert (e.core, e.vessel, e.stripped) == ("main engine", None, {})
    assert clean_equipment_label("Engine Star board", {}, cfg).vessel == "engine star"
    assert (
        clean_equipment_label("Enginestar pump", {}, cfg).core == "pump"  # run together: whole name
    )  # run together
    # with and without spaces / hyphens on a two-word name (the repo's extra list has the sister ship)
    for spelling in ("PACIFIC GARNET AE #1", "Pacific-Garnet AE #1", "PACIFICGARNET AE #1"):
        e = clean_equipment_label(spelling, {"vessel_name": "PACIFIC PEARL"})
        assert (e.core, e.position, e.vessel, e.sister_vessel) == (
            "auxiliary engine",
            "No.1",
            "pacific garnet",
            "pacific garnet",
        ), spelling
    # the record's own vessel is not a sister
    e = clean_equipment_label("PACIFIC GARNET AE #1", {"vessel_name": "PACIFIC GARNET"})
    assert e.vessel == "pacific garnet" and e.sister_vessel is None
    # a part has no vessel step
    assert clean_equipment_label("OASIS gasket", {}, cfg, kind="part").core == "oasis gasket"
    # the store writes the sister ship into the log
    s = LookupStore(guard=DEFAULT_GUARD)
    r = s.resolve(
        "PACIFIC GARNET AE #2", source="casefile:p", context={"vessel_name": "PACIFIC PEARL"}
    )
    assert (r.key, r.position, r.method) == ("auxiliary engine", "No.2", "new_term")
    row = s.log(kind="equipment")[-1]
    assert row["method_chain"] == "vessel_strip+position+abbreviations+normalised"
    assert row["rationale"].endswith("; label names sister vessel PACIFIC GARNET")
    assert s.alias_rows()[0]["parts"]["sister_vessel"] == "pacific garnet"


def test_glossary_full_hit_partial_hit_and_adjudicator():
    gl = load_equipment_glossary(CONFIG_DIR / "equipment_glossary.csv")
    assert gl["主机"] == ("zh", "main engine", "equipment") and gl["右"][1] == "starboard"
    assert {k for k in gl if gl[k][0] == "ja"} >= {"主機", "補機"} and gl["주기관"][0] == "ko"
    assert has_non_latin("右主机") and not has_non_latin("M/E No.1 (S)")
    e = clean_equipment_label("右主机", {"vessel_name": "KING ONE"})
    assert (e.core, e.position, e.side, e.script, e.issues) == (
        "main engine",
        "starboard",
        "starboard",
        "zh",
        (),
    )
    assert clean_equipment_label("左主机", {}).side == "port"
    assert clean_equipment_label("主機 2号", {}).core == "main engine"  # ja + N号 -> No.2
    assert clean_equipment_label("주기관", {}).core == "main engine"
    # a partial hit: the leftover stays, the guard reason is set, nothing is minted
    e = clean_equipment_label("右主机增压器", {})
    assert (e.untranslated, e.issues) == ("增压器", ("needs_translation",))
    s = LookupStore(guard=DEFAULT_GUARD)
    r = s.resolve("右主机增压器", source="casefile:k")
    assert r.method == "queued" and r.reason == "needs_translation" and s.standards() == []
    assert s.pending()[0].parts["untranslated"] == "增压器"
    # with an adjudicator the leftover is translated and the label is minted; the step is logged
    calls = []

    def adjudicate(text: str) -> str | None:
        calls.append(text)
        return "turbocharger" if "增压器" in text else None

    s2 = LookupStore(guard=DEFAULT_GUARD, adjudicator=adjudicate)
    r = s2.resolve("右主机增压器", source="casefile:k")
    assert (r.method, r.key, r.position) == ("new_term", "main engine turbocharger", "starboard")
    assert calls == ["增压器"]
    parts = s2.alias_rows()[0]["parts"]
    assert parts["stripped"] == {"glossary": "右主机增压器", "adjudicator": "增压器"}
    assert s2.log()[-1]["method_chain"] == "glossary+adjudicator+position+normalised"
    # an adjudicator that gives up leaves the label queued
    s3 = LookupStore(guard=DEFAULT_GUARD, adjudicator=lambda t: None)
    assert s3.resolve("右主机增压器").method == "queued"
    # a standard key never carries non-Latin characters
    assert all(not has_non_latin(x.key) for st in (s, s2, s3) for x in st.standards())


def test_record_strip_uses_the_entry_values_only():
    ctx = {"vessel_name": "OASIS", "maker": "MAN B&W", "model": "S60ME-C8", "serial": "HE0003"}
    e = clean_equipment_label("MAN B&W main engine S60ME-C8 HE0003", ctx)
    assert e.core == "main engine" and e.stripped["record"] == "MAN B&W | S60ME-C8 | HE0003"
    assert list(e.steps) == ["record_strip"]
    # the casefolded / punctuation-stripped form matches too
    assert clean_equipment_label("man b w main engine", ctx).core == "main engine"
    # "NA" and blanks never match anything
    e = clean_equipment_label("NA main engine", {"maker": "NA", "model": "", "serial": None})
    assert e.core == "na main engine" and "record" not in e.stripped
    # another record's maker is NOT stripped: no corpus-wide maker list
    assert (
        clean_equipment_label("MAN B&W main engine", {"maker": "HIMSEN"}).core
        == "man b and w main engine"
    )
    # a part has no record entry
    assert clean_equipment_label("MAN B&W piston", ctx, kind="part").core == "man b and w piston"


def test_qualifiers_moved_out_of_noise_words():
    assert "spare" not in NOISE_WORDS and "new" not in NOISE_WORDS and "pcs" in NOISE_WORDS
    e = clean_equipment_label("spare exhaust valve", {}, kind="part")
    assert (e.core, e.stripped, list(e.steps)) == (
        "exhaust valve",
        {"qualifier": "spare"},
        ["qualifier"],
    )
    e = clean_equipment_label("6 sets of exhaust valve overhaul spares", {}, kind="part")
    assert e.core == "exhaust valve overhaul" and e.stripped["qualifier"] == "spare"
    assert e.residue == ("6 sets of",)  # dropped by a regex, not a table
    assert normalize("Damaged bearing (old)").key == "bearing"
    assert normalize("new idle gear").key == "idle gear"  # today's behaviour kept


def test_guard_reasons_each():
    s = LookupStore(guard=DEFAULT_GUARD)
    # needs_translation
    assert s.resolve("锅炉给水泵X", source="t").reason == "needs_translation"
    # the other three cannot normally survive cleaning; drive the guard with hand-built parts
    from src.resolution.normalize import Cleaned

    for reason in ("contains_vessel", "contains_position", "contains_identifier"):
        c = Cleaned("k", None, "k", False, {"core": "k", "issues": [reason]})
        assert equipment_guard("equipment", c, "k") == reason
    assert equipment_guard("part", Cleaned("k", None, "k", False, {"core": "k"}), "k") is None
    assert (
        equipment_guard("symptom", Cleaned("k", None, "k", False, {"issues": ["x"]}), "k") is None
    )
    # a clean label is minted as before
    assert s.resolve("HFO storage tank 1S", source="t").method == "new_term"
    assert s.resolve("No. 3 AE", source="t").method == "new_term"


def _build_lookup_module():
    spec = importlib.util.spec_from_file_location(
        "build_lookup", ROOT / "scripts" / "build_lookup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_check_flags_equipment_standards_without_a_typed_record_alias():
    mod = _build_lookup_module()
    rec = json.loads(json.dumps(RECORD))
    rec["equipment"].append(
        dict(rec["equipment"][0], equipment_id="e2", name="Hypermist", equipment_type=None)
    )
    s = LookupStore(guard=DEFAULT_GUARD)
    s.import_csv(ROOT / "src" / "resolution" / "seed_aliases.csv")
    for e in rec["equipment"]:
        s.resolve(
            e["name"],
            source="casefile:" + rec["casefile_id"],
            context=views.equipment_context(rec, e),
        )
    offenders, seed_only = mod.check_equipment_types(s, [rec])
    assert [o.key for o in offenders] == ["hypermist"]  # from a record, but that entry has no type
    assert "main engine" not in {o.key for o in offenders}  # M/E is typed main_engine in the record
    assert {o.key for o in seed_only} >= {
        "diesel generator",
        "turbocharger",
    }  # seed rows only: reported
    rec["equipment"][1]["equipment_type"] = "fire_fighting_system"
    assert mod.check_equipment_types(s, [rec])[0] == []


def test_canonical_position_is_a_view_over_the_record_field():
    assert (
        views.canonical_position("right") == "starboard"
        and views.canonical_position("left") == "port"
    )
    assert views.canonical_position("port side forward") == "port side forward"
    assert views.canonical_position("No. 3") == "No. 3" and views.canonical_position(None) == ""
    assert views.canonical_position("stbd fwd") == "starboard forward"


def test_lookup_page_shows_chain_and_stripped_text_golden(tmp_path):
    rec = json.loads(json.dumps(RECORD))
    rec["vessel_name"] = "PACIFIC PEARL"
    rec["equipment"] = [
        dict(
            rec["equipment"][0],
            equipment_id="e1",
            name="PACIFIC GARNET AE #1",
            equipment_type="auxiliary_engine",
            position="No. 1",
            maker="NA",
            model="NA",
        ),
        dict(
            rec["equipment"][0],
            equipment_id="e2",
            name="右主机",
            equipment_type="main_engine",
            position="right",
            maker="NA",
            model="NA",
        ),
    ]
    s = LookupStore(guard=DEFAULT_GUARD)
    for e in rec["equipment"]:
        s.resolve(
            e["name"],
            source="casefile:" + rec["casefile_id"],
            context=views.equipment_context(rec, e),
        )
    r = views.resolver_tables(s, [rec])
    aliases = {a["raw"]: a for g in r["equipment"] for st in g["standards"] for a in st["aliases"]}
    assert (
        aliases["PACIFIC GARNET AE #1"]["chain"] == "vessel_strip+position+abbreviations+new_term"
    )
    assert aliases["PACIFIC GARNET AE #1"]["stripped"] == "vessel: PACIFIC GARNET"
    assert aliases["右主机"]["chain"] == "glossary+position+new_term"
    assert aliases["右主机"]["stripped"] == "glossary: 右主机"
    out = tmp_path / "site"
    build(DictRecordSource({rec["casefile_id"]: rec}), out, lookup=s)
    assert check_links(out) == []
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert (
        '<span title="vessel: PACIFIC GARNET">PACIFIC GARNET AE #1 <span class="n">(1 · new_term)</span> '
        '<span class="chain small muted mono">vessel_strip+position+abbreviations+new_term</span> '
        '<span class="stripped small muted">− vessel: PACIFIC GARNET</span></span>'
    ) in page
    assert (
        '<span title="glossary: 右主机">右主机 <span class="n">(1 · new_term)</span> '
        '<span class="chain small muted mono">glossary+position+new_term</span> '
        '<span class="stripped small muted">− glossary: 右主机</span></span>'
    ) in page
    assert "Each spelling shows its cleaning" in page
    # the casefile page shows the canonical side and keeps the record's wording
    cf = (out / "casefile" / f"{rec['casefile_id']}.html").read_text(encoding="utf-8")
    assert (
        '<dt>Position</dt><dd>starboard <span class="muted small">(as written: right)</span></dd>'
        in cf
    )
    assert "<dt>Position</dt><dd>No. 1</dd>" in cf
    assert rec["equipment"][1]["position"] == "right"  # the record is never rewritten


@pytest.mark.parametrize(
    "raw, key, position",
    [
        ("No.1 AE", "auxiliary engine", "No.1"),
        ("AE No. 2 SCR", "auxiliary engine selective catalytic reduction", "No.2"),
        ("Auxiliary Engine No.4", "auxiliary engine", "No.4"),
        ("HFO storage tank 1S", "heavy fuel oil storage tank", "1S"),
    ],
)
def test_ordinals_both_sides_and_a_clean_label(raw, key, position):
    e = clean_equipment_label(raw, {"vessel_name": "X"})
    assert (e.core, e.position, e.issues) == (key, position, ())
