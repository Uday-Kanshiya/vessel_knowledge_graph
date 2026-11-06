"""Alias table (docs/13): deterministic cleaning, the two-table store, the display-name view."""

from pathlib import Path

import pytest

from src.resolution import LookupStore, normalize
from src.resolution.normalize import has_abbreviations

SEED = Path(__file__).resolve().parents[2] / "src" / "resolution" / "seed_aliases.csv"


# --------------------------------------------------------------------------- normalize


@pytest.mark.parametrize(
    "raw, key, position",
    [
        ("Main engine", "main engine", None),
        ("M/E", "main engine", None),
        ("ME CW PP", "main engine cooling water pump", None),
        ("M/E C.W. P/P", "main engine cooling water pump", None),
        ("Main Eng Cooling Water Pump", "main engine cooling water pump", None),
        ("No.1 M/E C.W. P/P", "main engine cooling water pump", "No.1"),
        ("Auxiliary Engine No.4", "auxiliary engine", "No.4"),
        ("No. 3 AE", "auxiliary engine", "No.3"),
        ("#2 D/G", "diesel generator", "No.2"),
        ("HFO storage tank 1S", "heavy fuel oil storage tank", "1S"),
        ("Cofferdam (S)", "cofferdam", "starboard"),
        ("right main engine", "main engine", "starboard"),  # left/right -> port/starboard (modifier_terms.csv)
        ("Port Side Forward Chain Stopper", "chain stopper", "port forward"),
        ("EGCS", "exhaust gas cleaning system", None),
        ("SCR", "selective catalytic reduction", None),  # the S of SCR is not a side letter
        ("Control & Metering Unit", "control and metering unit", None),
        ("8mm steel plate", "8mm steel plate", None),  # a number glued to a unit is not a position
        ("Gas detector chassis", "gas detector chassis", None),  # -ss / short words untouched
        ("2 pcs exhaust valve stroke sensors", "exhaust valve stroke sensor", None),
        ("exhaust valve safety valves", "exhaust valve safety valve", None),
        ("O rings", "o ring", None),
        ("CCS-AH36 plates, 1150x700x10mm", "ccs ah36 plate 1150x700x10mm", None),
        ("6 sets of exhaust valve overhaul spares", "exhaust valve overhaul", None),
        ("new idle gear", "idle gear", None),
    ],
)
def test_normalize_key_and_position(raw, key, position):
    c = normalize(raw)
    assert (c.key, c.position) == (key, position)


def test_normalize_is_deterministic_and_order_free():
    spellings = ["ME CW PP", "Main Engine Cooling Water Pump", "m/e c.w. p/p", "MAIN ENG CW PUMP"]
    keys = {normalize(s).key for s in spellings}
    assert keys == {"main engine cooling water pump"}


def test_normalize_empty_and_position_only():
    assert normalize(None).is_empty
    assert normalize("   ").is_empty
    c = normalize("No.2")
    assert c.is_empty and c.position == "No.2"


def test_stem_keeps_casing_and_drops_position():
    c = normalize("Auxiliary Engine No.4")
    assert c.stem == "Auxiliary Engine"
    assert normalize("HFO storage tank 1S").stem == "HFO storage tank"


def test_has_abbreviations():
    assert has_abbreviations("M/E C.W. P/P")
    assert has_abbreviations("Main Eng Cooling Water Pump")
    assert not has_abbreviations("Main Engine Cooling Water Pump")


# --------------------------------------------------------------------------- store


def test_update_rule_three_cases():
    s = LookupStore()
    r1 = s.resolve("Main Engine Cooling Water Pump")
    assert r1.method == "new_term"  # case b: minted
    r2 = s.resolve("ME CW PP")
    assert r2.method == "normalised" and r2.standard_id == r1.standard_id  # case a: new spelling
    r3 = s.resolve("ME CW PP")
    assert r3.method == "alias" and r3.standard_id == r1.standard_id  # already known
    assert s.alias_rows()[0]["hit_count"] >= 1
    hits = {r["raw_term"]: r["hit_count"] for r in s.alias_rows()}
    assert hits["ME CW PP"] == 2


def test_raw_key_is_casefolded_and_punctuation_free():
    s = LookupStore()
    a = s.resolve("M/E C.W. P/P")
    b = s.resolve("m/e c.w. p/p")
    c = s.resolve("M/E CW P/P")
    assert b.method == "alias"  # same row, not a second alias
    assert c.method == "normalised"
    assert len(s.alias_rows()) == 2
    assert a.standard_id == b.standard_id == c.standard_id


def test_first_arrival_does_not_pick_the_standard():
    a = LookupStore()
    a.resolve("ME CW PP")
    a.resolve("Main Engine Cooling Water Pump")
    b = LookupStore()
    b.resolve("Main Engine Cooling Water Pump")
    b.resolve("ME CW PP")
    assert [x.key for x in a.standards()] == [x.key for x in b.standards()]
    assert a.display_name(1) == b.display_name(1) == "Main Engine Cooling Water Pump"


def test_display_name_is_a_view_over_aliases():
    s = LookupStore()
    r = s.resolve("ME CW PP")
    assert s.display_name(r.standard_id) == "Main engine cooling water pump"  # fallback: key
    s.resolve("Main Engine CW Pump")  # still abbreviated -> not display-ready
    assert s.display_name(r.standard_id) == "Main engine cooling water pump"
    s.resolve("No.2 Main Engine Cooling Water Pump")  # position stripped from the shown name
    assert s.display_name(r.standard_id) == "Main Engine Cooling Water Pump"
    assert s.resolve("M/E C.W. P/P").display == "Main Engine Cooling Water Pump"


def test_position_is_split_not_a_new_standard():
    s = LookupStore()
    a = s.resolve("Auxiliary Engine No.4")
    b = s.resolve("No. 3 AE")
    assert a.standard_id == b.standard_id
    assert (a.position, b.position) == ("No.4", "No.3")
    assert s.fragmentation()["standards"] == 1


def test_kinds_are_separate_namespaces():
    s = LookupStore()
    e = s.resolve("fuel pump", kind="equipment")
    p = s.resolve("fuel pump", kind="part")
    assert e.standard_id != p.standard_id
    with pytest.raises(ValueError):
        s.resolve("x", kind="alarm")


def test_new_terms_land_in_review_queue_and_approve_clears():
    s = LookupStore()
    r = s.resolve("Hypermist system")
    assert [x.id for x in s.review_queue()] == [r.standard_id]
    s.approve(r.standard_id, note="checked against PMS")
    assert s.review_queue() == []
    assert s.standards()[0].status == "reviewed"


def test_merge_redirects_aliases_and_keeps_history():
    s = LookupStore()
    a = s.resolve("Scrubber")
    b = s.resolve("Exhaust Gas Cleaning System")
    s.merge(a.standard_id, b.standard_id, note="same unit")
    again = s.resolve("scrubber")
    assert again.standard_id == b.standard_id and again.key == "exhaust gas cleaning system"
    assert s.display_name(a.standard_id) == "Exhaust Gas Cleaning System"
    frag = s.fragmentation()
    assert frag["standards"] == 1 and frag["merged"] == 1
    s.merge(b.standard_id, a.standard_id)  # a already points at b: no cycle, no change
    assert s.resolve("EGCS").standard_id == b.standard_id


def test_set_alias_overrides_rules_and_is_sticky():
    s = LookupStore()
    r = s.resolve("CW pump")  # rules: cooling water pump
    sid = s.set_alias("CW pump", "jacket cooling water pump")
    assert sid != r.standard_id
    again = s.resolve("cw pump")
    assert again.standard_id == sid and again.method == "human"
    assert s.standards(status="reviewed")[0].key == "jacket cooling water pump"


def test_mint_false_leaves_unknowns_alone():
    s = LookupStore()
    assert s.resolve("Hypermist", mint=False) is None
    assert s.standards() == []


def test_seed_csv_roundtrip(tmp_path):
    s = LookupStore()
    n = s.import_csv(SEED)
    assert n >= 10
    assert s.review_queue() == []  # seed rows are reviewed
    assert s.resolve("m/e").method == "alias"
    assert s.resolve("Scrubber").key == "exhaust gas cleaning system"
    out = tmp_path / "aliases.csv"
    s.export_csv(out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("kind,raw_term,standard_key,status")
    s2 = LookupStore()
    assert s2.import_csv(out) == n
    assert {x.key for x in s2.standards()} == {x.key for x in s.standards()}


def test_sqlite_file_persists(tmp_path):
    db = tmp_path / "r" / "lookup.sqlite"
    with LookupStore(db) as s:
        s.resolve("Chain stopper")
    with LookupStore(db) as s:
        assert s.resolve("chain-stopper").method == "alias"


# --------------------------------------------------------------------------- symptom kind + families

ROOT = Path(__file__).resolve().parents[2]
FAMILIES = ROOT / "config" / "symptom_families.csv"
SEED_SYMPTOMS = ROOT / "config" / "seed_symptom_aliases.csv"


def _families_csv(tmp_path, rows: list[tuple[str, str, str, int]]) -> Path:
    p = tmp_path / "fam.csv"
    lines = ["key,display,description,patterns,sort_order", "# proposal"]
    lines += [f"{k},{d},,{pat},{o}" for k, d, pat, o in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_symptom_kind_resolves_and_mints_like_equipment():
    s = LookupStore()
    ctx = {"equipment_type": "main_engine"}
    a = s.resolve(
        "Knocking sound from main engine exhaust valve cylinder no. 4.", kind="symptom", context=ctx
    )
    # A1 (13 §8): the key is the failure mode; the component and the position are parts
    assert a.method == "new_term" and a.key == "knocking sound" and a.position == "cylinder no. 4"
    b = s.resolve("knocking sound from M/E exhaust valve cyl 6", kind="symptom", context=ctx)
    assert (
        b.method == "normalised" and b.standard_id == a.standard_id
    )  # a new spelling, same standard
    c = s.resolve(
        "Knocking sound from main engine exhaust valve cylinder no. 4.", kind="symptom", context=ctx
    )
    assert c.method == "alias"
    assert s.display_name(a.standard_id) == "Knocking sound"  # casing kept, position gone
    # the component became a part alias sharing the id action_logs parts would get
    part = s.resolve("exhaust valve", kind="part")
    assert part.method == "alias" and {x.kind for x in s.review_queue()} == {"part", "symptom"}
    sym_alias = [x for x in s.alias_rows("symptom") if x["raw_term"].startswith("Knocking")][0]
    assert sym_alias["parts"]["component"] == "exhaust valve"
    assert sym_alias["parts"]["component_standard_id"] == part.standard_id
    # symptom is its own namespace
    e = s.resolve("knocking sound from exhaust valve", kind="equipment")
    assert e.standard_id != a.standard_id
    assert (
        s.find("Knocking sound from exhaust valve cyl 1", kind="symptom").standard_id
        == a.standard_id
    )
    assert (
        s.find("never seen wording", kind="symptom") is None
        and len(s.standards(kind="symptom")) == 1
    )


def test_find_never_writes():
    s = LookupStore()
    s.resolve("Main engine")
    before = (len(s.alias_rows()), s.alias_rows()[0]["hit_count"])
    assert s.find("M/E").standard_id == 1 and s.find("Scrubber") is None
    assert (len(s.alias_rows()), s.alias_rows()[0]["hit_count"]) == before


def test_families_come_only_from_csv_and_never_grow_from_feed(tmp_path):
    s = LookupStore()
    assert s.families("symptom") == []
    n = s.import_families_csv(
        _families_csv(
            tmp_path, [("leakage", "Leakage", "leak", 1), ("noise", "Noise", "knock|noise", 2)]
        )
    )
    assert n == 2 and [f.key for f in s.families("symptom")] == ["leakage", "noise"]
    for w in ["Fuel oil leaking", "Knocking sound", "Vibration at bearing", "Leak at flange"]:
        s.resolve(w, kind="symptom")
    s.assign_families("symptom")
    assert [f.key for f in s.families("symptom")] == [
        "leakage",
        "noise",
    ]  # feeding never adds a family
    with pytest.raises(KeyError):
        s.set_family("Vibration at bearing", "vibration")  # not in the CSV -> refused, not minted
    # re-import updates in place (display / patterns), never duplicates
    s.import_families_csv(
        _families_csv(
            tmp_path, [("leakage", "Leaks", "leak|seep", 1), ("noise", "Noise", "knock|noise", 2)]
        )
    )
    fams = s.families("symptom")
    assert len(fams) == 2 and fams[0].display == "Leaks" and fams[0].patterns == ["leak", "seep"]


def test_family_precedence_human_seed_inherited_rule_none(tmp_path):
    s = LookupStore()
    s.import_families_csv(
        _families_csv(
            tmp_path,
            [
                ("leakage", "Leakage", "leak", 1),
                ("noise", "Noise", "knock|noise", 2),
                ("vibration", "Vibration", "vibrat", 3),
            ],
        )
    )
    # standards
    human = s.resolve(
        "Knocking sound", kind="symptom"
    )  # will be set by a human (to leakage, deliberately against the rule)
    seed = s.resolve(
        "Oil seeping at flange", kind="symptom"
    )  # seed CSV row -> leakage (no pattern matches "seep")
    child = s.resolve(
        "Knocking sound at high rpm", kind="symptom"
    )  # inherits from "knocking sound" (a wording that does NOT dissolve: no table term in it)
    rule = s.resolve("Fuel leak at pump", kind="symptom")  # rule: leakage
    none = s.resolve("Coating thickness below spec", kind="symptom")  # nothing
    # 1. human
    s.set_family("knocking sound", "leakage", kind="symptom", source="human")
    # 1. seed via CSV
    p = tmp_path / "seed.csv"
    p.write_text(
        "kind,raw_term,standard_key,status,family_key\n# note\nsymptom,Oil seeping at flange,,reviewed,leakage\n",
        encoding="utf-8",
    )
    assert s.import_csv(p, source="seed") == 1
    # 2 + 3 + 4 derived
    links = {"knocking sound at high rpm": ["knocking sound"]}
    counts = s.assign_families("symptom", ancestors_of=lambda k: links.get(k, []))
    fam = {x.key: (x.family_key, x.family_method) for x in s.standards(kind="symptom")}
    assert fam["knocking sound"] == ("leakage", "human")  # rule says noise; human wins
    assert fam["oil seeping at flange"] == ("leakage", "seed")
    assert fam["knocking sound at high rpm"] == (
        "leakage",
        "inherited",
    )  # follows its (human-set) broader term
    assert fam["fuel leak"] == (
        "leakage",
        "rule",
    )  # "at pump": equipment word and trailing preposition stripped
    assert fam["coating thickness below spec"] == (None, "none")
    assert counts == {"human": 1, "seed": 1, "inherited": 1, "rule": 1, "none": 1}
    assert [x.key for x in s.unclassified("symptom")] == ["coating thickness below spec"]
    assert s.family_of(rule.standard_id).key == "leakage" and s.family_of(none.standard_id) is None
    assert (
        s.family_of(human.standard_id).key == "leakage"
        and s.family_of(seed.standard_id).key == "leakage"
    )
    assert s.family_of(child.standard_id).key == "leakage"
    # a rule never overrides a human row, and re-running is idempotent
    s.resolve("Knocking sound", kind="symptom")
    again = s.assign_families("symptom", ancestors_of=lambda k: links.get(k, []))
    assert again == counts
    assert {x.key: (x.family_key, x.family_method) for x in s.standards(kind="symptom")} == fam
    # when the ancestor loses its family the child falls back to the rule
    s.db.execute(
        "UPDATE standard_term SET family_id=NULL, family_method='none' WHERE key='knocking sound'"
    )
    s.assign_families("symptom", ancestors_of=lambda k: links.get(k, []))
    fam2 = {x.key: (x.family_key, x.family_method) for x in s.standards(kind="symptom")}
    assert fam2["knocking sound"] == ("noise", "rule") and fam2[
        "knocking sound at high rpm"
    ] == ("noise", "inherited")


def test_fragmentation_reports_per_kind_and_unclassified(tmp_path):
    s = LookupStore()
    s.import_families_csv(_families_csv(tmp_path, [("leakage", "Leakage", "leak", 1)]))
    s.resolve("M/E")
    s.resolve("Fuel leak", kind="symptom")
    s.resolve("Vibration", kind="symptom")
    s.assign_families("symptom")
    f = s.fragmentation()
    assert (
        f["standards"] == 3 and f["unclassified"] == 2
    )  # the equipment standard has no family either
    assert (
        f["by_kind"]["symptom"]["standards"] == 2 and f["by_kind"]["symptom"]["unclassified"] == 1
    )
    assert f["by_kind"]["equipment"]["standards"] == 1 and f["families"] == {
        "equipment": 0,
        "part": 0,
        "symptom": 1,
    }


def test_export_csv_carries_family_key_and_reimports(tmp_path):
    s = LookupStore()
    s.import_families_csv(_families_csv(tmp_path, [("leakage", "Leakage", "leak", 1)]))
    s.resolve("Fuel leak at pump", kind="symptom")
    s.resolve("M/E")
    s.assign_families("symptom")
    out = tmp_path / "aliases.csv"
    s.export_csv(out)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("kind,raw_term,standard_key,status,family_key")
    assert "symptom,Fuel leak at pump,fuel leak,auto,leakage" in text
    assert "equipment,M/E,main engine,auto," in text  # blank family = unchanged on re-import
    assert "part,pump,pump,auto," in text  # the component of the symptom label, as a part alias
    s2 = LookupStore()
    s2.import_families_csv(_families_csv(tmp_path, [("leakage", "Leakage", "leak", 1)]))
    assert s2.import_csv(out, source="human") == 3
    row = {x.key: x for x in s2.standards(kind="symptom")}["fuel leak"]
    assert row.family_key == "leakage" and row.family_method == "human"


def test_repo_family_csv_and_seed_load():
    s = LookupStore()
    n = s.import_families_csv(FAMILIES)
    assert 18 <= n <= 25, "a bounded list: ~20 families"
    keys = [f.key for f in s.families("symptom")]
    assert "abnormal_noise" in keys and "leakage" in keys and len(keys) == len(set(keys))
    assert s.import_csv(SEED_SYMPTOMS, source="seed") >= 1
    r = s.find("Knocking sound from main engine", kind="symptom")
    assert r and s.family_of(r.standard_id).key == "abnormal_noise"
    # every pattern is non-empty and every family has a display
    for f in s.families("symptom"):
        assert f.display and all(p for p in f.patterns)


def test_old_db_without_family_columns_still_opens(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE standard_term (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'auto', merged_into INTEGER, source TEXT NOT NULL, first_seen TEXT NOT NULL, "
        "note TEXT, UNIQUE (kind, key));"
        "CREATE TABLE alias (kind TEXT NOT NULL, raw_key TEXT NOT NULL, raw_term TEXT NOT NULL, stem TEXT NOT NULL, "
        "standard_id INTEGER NOT NULL, method TEXT NOT NULL, source TEXT NOT NULL, first_seen TEXT NOT NULL, "
        "last_seen TEXT NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, raw_key));"
        "INSERT INTO standard_term (kind, key, status, source, first_seen) VALUES ('equipment', 'main engine', 'auto', 'rule', 'x');"
        "INSERT INTO alias VALUES ('equipment', 'm e', 'M/E', 'M/E', 1, 'new_term', 'x', 'x', 'x', 1);"
    )
    con.commit()
    con.close()
    with LookupStore(db) as s:
        assert s.find("M/E").standard_id == 1
        assert s.standards()[0].family_key is None and s.standards()[0].family_method == "none"
