"""
P0 shared store pieces (docs/13 §2, §4-§6; docs/16 P0): the table-driven resolver cases, the
pending queue + mint guard, the resolution log, alias.parts, _migrate() on a pre-P0 file, the
shared config tables, diff_lookup on two in-memory stores, and the queue section of the lookup page.
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.resolution import LookupStore
from src.resolution.diff import diff, render_md, snapshot
from src.resolution.lookup import DEFAULT_GUARD, METHODS, PENDING_REASONS, Resolution
from src.resolution.normalize import (
    Cleaned,
    check_header,
    load_config,
    load_modifier_terms,
    normalize,
    strip_comments,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES = HERE / "resolution_cases.csv"
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG_DIR = ROOT / "config"


def _cases() -> list[dict]:
    with open(CASES, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(strip_comments(fh))
        check_header(
            reader.fieldnames,
            (
                "kind",
                "raw",
                "vessel_name",
                "equipment_type",
                "maker",
                "model",
                "serial",
                "expected_key",
                "expected_parts_json",
                "expected_method",
            ),
            CASES,
        )
        return [r for r in reader if (r.get("kind") or "").strip()]


# --------------------------------------------------------------------------- 1. the table


@pytest.mark.parametrize("case", _cases(), ids=lambda c: f"{c['kind']}:{c['raw'][:40]}")
def test_resolution_case(case):
    """One fresh store per row, repo config, no seeds, the symptom guard on: key, method, and
    the parts listed in expected_parts_json (a subset check; null = absent)."""
    s = LookupStore(config_dir=CONFIG_DIR, guard=DEFAULT_GUARD)  # symptom + equipment guards
    context = {
        k: (case[k] or "").strip() or None
        for k in ("equipment_type", "vessel_name", "maker", "model", "serial")
    }
    r = s.resolve(case["raw"], kind=case["kind"], source="test", context=context)
    assert r is not None
    assert r.key == case["expected_key"]
    assert r.method == case["expected_method"]
    parts = {"position": r.position}
    if r.method != "queued":
        alias = next(a for a in s.alias_rows(case["kind"]) if a["raw_term"] == case["raw"].strip())
        parts.update(alias["parts"] or {})
    else:
        parts.update(s.pending(case["kind"])[0].parts or {})
    for k, v in json.loads(case["expected_parts_json"]).items():
        assert parts.get(k) == v, (k, parts)


def test_case_table_covers_the_prompt_examples():
    rows = _cases()
    assert len(rows) >= 10
    assert {r["kind"] for r in rows} == {"equipment", "part", "symptom"}
    raws = {r["raw"] for r in rows}
    assert sum(r["kind"] == "symptom" for r in rows) >= 10  # the P1 cases
    assert {
        "No. 3 AE",
        "M/E C.W. P/P",
        "safety valves",
        "right main engine",
        "left main engine",
    } <= raws


# --------------------------------------------------------------------------- 2. config tables


def test_modifier_terms_csv_is_the_position_table_and_canonicalises_sides():
    cfg = load_config(CONFIG_DIR)
    assert cfg.modifier_terms["left"] == "port" and cfg.modifier_terms["right"] == "starboard"
    assert cfg.modifier_terms["stbd"] == "starboard" and cfg.modifier_terms["center"] == "centre"
    assert normalize("right main engine").position == "starboard"
    assert normalize("Left Side Forward Chain Stopper").position == "port forward"
    # the generated fleet list is optional, casefolded, comment lines ignored
    assert all(n == n.casefold() for n in cfg.vessel_names)


def test_store_reads_its_own_config_dir(tmp_path):
    (tmp_path / "modifier_terms.csv").write_text(
        "term,canonical\n# test table\nleft,left\nupper,top\n", encoding="utf-8"
    )
    (tmp_path / "vessel_names.txt").write_text("# generated\nTEST VESSEL\n", encoding="utf-8")
    s = LookupStore(config_dir=tmp_path)
    assert s.config.vessel_names == frozenset({"test vessel"})
    r = s.resolve("left upper main engine")
    assert (r.key, r.position) == ("main engine", "left top")  # this store's table, not the repo's
    assert normalize("left main engine").position == "port"  # the default table is untouched
    with pytest.raises(FileNotFoundError):
        LookupStore(config_dir=tmp_path / "nowhere")


def test_config_and_seed_csvs_are_header_checked(tmp_path):
    bad = tmp_path / "modifier_terms.csv"
    bad.write_text("word,meaning\nleft,port\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad CSV header"):
        load_modifier_terms(bad)
    s = LookupStore()
    seed = tmp_path / "seed.csv"
    seed.write_text("kind,raw,standard_key\nequipment,M/E,main engine\n", encoding="utf-8")
    with pytest.raises(ValueError, match="raw_term"):
        s.import_csv(seed)
    fam = tmp_path / "fam.csv"
    fam.write_text("key,label\nleakage,Leakage\n", encoding="utf-8")
    with pytest.raises(ValueError, match="display"):
        s.import_families_csv(fam)
    assert s.standards() == [] and s.families() == []
    # the repo's own CSVs pass
    assert s.import_csv(ROOT / "src" / "resolution" / "seed_aliases.csv") >= 10
    assert s.import_families_csv(CONFIG_DIR / "symptom_families.csv") >= 18
    assert s.import_csv(CONFIG_DIR / "seed_symptom_aliases.csv") >= 1


def test_vessel_names_file_is_generated_with_a_header():
    p = CONFIG_DIR / "vessel_names.txt"
    if not p.exists():
        pytest.skip("config/vessel_names.txt not generated yet (run scripts/build_lookup.py)")
    first = p.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#") and "GENERATED" in first


# --------------------------------------------------------------------------- 3. queue / guard


def _guard_positions(kind: str, c: Cleaned, raw: str) -> str | None:
    """A stand-in for the P1 / P2 guards: refuse to mint anything that carried a position."""
    return "contains_position" if c.position else None


def test_guard_queues_instead_of_minting_and_find_stays_unresolved():
    s = LookupStore()
    r = s.resolve("No. 3 AE", guard=_guard_positions, source="casefile:c1")
    assert r.method == "queued" and r.standard_id is None and r.reason == "contains_position"
    assert (r.key, r.position, r.display) == ("auxiliary engine", "No.3", "AE")
    assert s.standards() == [] and s.alias_rows() == []  # nothing minted, no alias
    assert (
        s.find("No. 3 AE") is None and s.find("AE") is None
    )  # pending rows are invisible to find()
    p = s.pending()
    assert len(p) == 1 and (p[0].kind, p[0].raw_key, p[0].raw_term, p[0].reason) == (
        "equipment",
        "no 3 ae",
        "No. 3 AE",
        "contains_position",
    )
    assert p[0].hit_count == 1 and p[0].source == "casefile:c1"
    assert p[0].parts["core"] == "auxiliary engine" and p[0].parts["position"] == "No.3"
    # a repeat bumps the count; the guard is asked again
    s.resolve("no. 3 ae", guard=_guard_positions, source="casefile:c2")
    assert s.pending()[0].hit_count == 2 and s.pending()[0].source == "casefile:c1"
    # a clean spelling is minted as before; the guard applies only to would-be new_term
    ok = s.resolve("Auxiliary engine", guard=_guard_positions)
    assert ok.method == "new_term" and ok.standard_id
    # ... and once the key exists, the positioned spelling resolves as normalised and leaves the queue
    again = s.resolve("No. 3 AE", guard=_guard_positions)
    assert again.method == "normalised" and again.standard_id == ok.standard_id
    assert s.pending() == []
    assert s.fragmentation()["n_pending"] == 0


def test_store_default_guard_and_fragmentation_counts_pending():
    s = LookupStore(guard=_guard_positions)
    assert s.resolve("No.1 M/E C.W. P/P").method == "queued"
    assert s.resolve("Port Side Forward Chain Stopper", kind="part").method == "queued"
    assert s.resolve("M/E").method == "new_term"
    f = s.fragmentation()
    assert f["n_pending"] == 2 and f["standards"] == 1
    assert f["by_kind"]["equipment"]["n_pending"] == 1 and f["by_kind"]["part"]["n_pending"] == 1
    assert [p.kind for p in s.pending("part")] == ["part"]
    assert s.resolve("No.2 D/G", mint=False) is None  # mint=False never queues
    assert s.fragmentation()["n_pending"] == 2


def test_queue_promote_dismiss():
    s = LookupStore()
    with pytest.raises(ValueError):
        s.queue("equipment", "右主机", "because")
    p = s.queue(
        "equipment", "右主机", "needs_translation", parts={"script": "zh"}, source="casefile:k"
    )
    assert p.reason == "needs_translation" and p.parts == {"script": "zh"} and p.hit_count == 1
    p2 = s.queue("equipment", "右主机", "needs_translation", source="casefile:k")
    assert p2.hit_count == 2 and p2.parts == {"script": "zh"}  # parts kept when the repeat has none
    s.queue("symptom", "Vibration", "undecomposed")
    assert [x.raw_term for x in s.pending()] == ["右主机", "Vibration"]  # hits desc
    # promote: alias of the given standard (created on demand), row gone
    sid = s.promote("equipment", "右主机", "main engine")
    assert s.find("右主机").standard_id == sid and s.find("右主机").method == "human"
    assert s.standards()[0].key == "main engine" and s.standards()[0].status == "reviewed"
    assert [x.kind for x in s.pending()] == ["symptom"]
    with pytest.raises(KeyError):
        s.promote("equipment", "右主机", "main engine")
    # dismiss: row gone, nothing created
    assert s.dismiss("symptom", "vibration", note="noise") is True
    assert s.dismiss("symptom", "vibration") is False
    assert s.pending() == [] and len(s.standards()) == 1
    # a seed CSV row pinning a pending spelling also clears it (promote by CSV)
    s.queue("part", "O rings", "candidate_term")
    sid2 = s.set_alias("O rings", "o ring", kind="part")
    assert s.pending() == [] and s.find("O rings", kind="part").standard_id == sid2
    for r in PENDING_REASONS:
        s.queue("part", f"term {r}", r)
    assert len(s.pending("part")) == len(PENDING_REASONS)


def test_resolution_method_vocabulary():
    assert "queued" in METHODS
    with pytest.raises(ValueError):
        Resolution(standard_id=1, key="k", position=None, method="fuzzy", display="K")
    with pytest.raises(ValueError):
        Resolution(standard_id=None, key="k", position=None, method="new_term", display="K")
    with pytest.raises(ValueError):
        Resolution(standard_id=1, key="k", position=None, method="queued", display="K")


# --------------------------------------------------------------------------- 4. the log


def test_resolution_log_rows():
    s = LookupStore()
    a = s.resolve("Main Engine Cooling Water Pump", source="casefile:1")  # new_term
    s.resolve("ME CW PP", source="casefile:2")  # normalised
    s.resolve("ME CW PP", source="casefile:3")  # alias: NOT logged
    s.resolve("No.1 D/G", guard=lambda k, c, r: "contains_position" if c.position else None)
    s.resolve("No.1 D/G", guard=lambda k, c, r: "contains_position")  # repeat: not logged again
    b = s.resolve("Scrubber")
    c = s.resolve("Exhaust Gas Cleaning System")
    s.approve(a.standard_id, note="checked")
    s.merge(b.standard_id, c.standard_id, note="same unit")
    s.set_alias("CW pump", "jacket cooling water pump")
    s.promote("equipment", "no 1 d g", "diesel generator")
    s.queue("symptom", "Vibration", "undecomposed")
    s.dismiss("symptom", "vibration")
    rows = s.log()
    chains = [(r["method_chain"], r["raw_key"]) for r in rows]
    assert chains == [
        ("normalised", "main engine cooling water pump"),
        ("abbreviations+normalised", "me cw pp"),  # the cleaner's steps lead the chain (13 §3)
        ("position+abbreviations+queued", "no 1 d g"),
        ("normalised", "scrubber"),
        ("normalised", "exhaust gas cleaning system"),
        ("approve", "main engine cooling water pump"),
        ("merge", "scrubber"),
        ("human", "cw pump"),
        ("human", "no 1 d g"),  # promote = set_alias (logged) ...
        ("promoted", "no 1 d g"),  # ... + its own row
        ("queued", "vibration"),
        ("dismissed", "vibration"),
    ]
    by_chain = {r["method_chain"]: r for r in rows}
    first = rows[0]
    assert (first["before"], first["after"], first["standard_id"]) == (
        "Main Engine Cooling Water Pump",
        "main engine cooling water pump",
        a.standard_id,
    )
    assert rows[0]["rationale"].startswith("new_term") and rows[1]["rationale"].startswith(
        "normalised"
    )
    assert rows[2]["standard_id"] is None and rows[2]["rationale"] == "contains_position"
    assert (by_chain["approve"]["before"], by_chain["approve"]["after"]) == ("auto", "reviewed")
    assert (
        by_chain["merge"]["after"] == "exhaust gas cleaning system"
        and by_chain["merge"]["standard_id"] == c.standard_id
    )
    assert by_chain["merge"]["rationale"] == "same unit"
    assert by_chain["dismissed"]["standard_id"] is None
    assert all(r["ts"] and r["kind"] for r in rows)
    # filters
    assert [r["kind"] for r in s.log(kind="symptom")] == ["symptom", "symptom"]
    assert s.log(since=rows[-1]["ts"])[-1]["method_chain"] == "dismissed"
    assert s.log(since="2999-01-01") == []
    # alias.method stays one word
    assert {r["method"] for r in s.alias_rows()} <= {"seed", "normalised", "new_term", "human"}


# --------------------------------------------------------------------------- 5. alias.parts + migrate


def test_alias_parts_round_trip_through_the_store(monkeypatch):
    """resolve() stores whatever Cleaned.parts carries; alias_rows() returns it as a dict."""
    import src.resolution.lookup as lookup_mod

    real = lookup_mod.normalize_for_kind

    def with_parts(raw, kind, config=None, context=None, adjudicator=None):
        c = real(raw, kind, config, context)
        return Cleaned(
            c.key,
            c.position,
            c.stem,
            c.expanded,
            parts={"steps": ["vessel_strip"], "residue": "x1"},
        )

    monkeypatch.setattr(lookup_mod, "normalize_for_kind", with_parts)
    s = LookupStore()
    s.resolve("Hypermist system")
    row = s.alias_rows()[0]
    assert row["parts"] == {"residue": "x1", "steps": ["vessel_strip"]}
    assert s.log()[0]["method_chain"] == "vessel_strip+normalised"  # the chain follows the steps
    assert s.display_name(row["standard_id"]) == "Hypermist system"  # display_name unchanged


def test_migrate_opens_a_pre_p0_db_from_fixtures(tmp_path):
    src = FIXTURES / "lookup_pre_p0.sqlite"
    db = tmp_path / "lookup.sqlite"
    shutil.copy(src, db)
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r[1] for r in con.execute("PRAGMA table_info(alias)")}
    con.close()
    assert "pending_term" not in names and "resolution_log" not in names and "parts" not in cols
    with LookupStore(db) as s:
        assert s.find("M/E").standard_id == 1 and s.find("No. 3 AE").position == "No.3"
        assert s.pending() == [] and s.log() == []
        assert s.alias_rows()[0]["parts"] is None
        r = s.resolve("Hypermist")  # the migrated file takes new writes
        assert r.method == "new_term" and s.log()[-1]["raw_key"] == "hypermist"
        s.queue("equipment", "右主机", "needs_translation")
        assert s.fragmentation()["n_pending"] == 1
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {r[1] for r in con.execute("PRAGMA table_info(alias)")}
    con.close()
    assert {"pending_term", "resolution_log"} <= names and "parts" in cols
    assert (
        sqlite3.connect(src).execute("SELECT count(*) FROM alias").fetchone()[0] == 5
    )  # fixture untouched


# --------------------------------------------------------------------------- 6. diff


def test_diff_two_in_memory_stores_reports_moves_pending_new_and_vanished():
    old = LookupStore()
    for raw in ("M/E", "Main engine", "Scrubber", "EGCS"):
        old.resolve(raw, source="casefile:a")
    old.resolve("exhaust valve", kind="part", source="casefile:a")
    old.resolve("Knocking sound from exhaust valve", kind="symptom", source="casefile:b")

    new = LookupStore()
    for raw in ("M/E", "Main engine"):
        new.resolve(raw, source="casefile:a")
    egcs = new.resolve("EGCS", source="casefile:a")
    new.set_alias("Scrubber", "exhaust gas cleaning system")  # was its own standard: moved
    new.queue(
        "part", "exhaust valve", "candidate_term", parts={"residue": "valve x"}, source="casefile:a"
    )
    new.resolve("Hypermist", source="casefile:c")  # new standard
    new.resolve("Vibration", kind="symptom", source="casefile:c")
    # the knocking symptom is gone (not re-resolved): vanished

    rep = diff(snapshot(old.db, "old"), snapshot(new.db, "new"))
    assert rep["changed"]
    eq = rep["kinds"]["equipment"]
    assert (eq["standards_before"], eq["standards_after"]) == (3, 3)  # scrubber out, hypermist in
    assert eq["moved"] == [
        {"from": "scrubber", "to": "exhaust gas cleaning system", "raw_keys": ["scrubber"]}
    ]
    assert [s["key"] for s in eq["new_standards"]] == ["hypermist"]
    assert [s["key"] for s in eq["vanished_standards"]] == ["scrubber"]
    assert egcs.key == "exhaust gas cleaning system"
    part = rep["kinds"]["part"]
    assert part["now_pending"] == [
        {
            "raw_key": "exhaust valve",
            "raw_term": "exhaust valve",
            "reason": "candidate_term",
            "hits": 1,
            "was": "exhaust valve",
            "parts": {"residue": "valve x"},
        }
    ]
    assert part["residue"] == [
        {"token": "valve", "n": 1, "casefiles": 1},
        {"token": "x", "n": 1, "casefiles": 1},
    ]
    assert (part["standards_before"], part["standards_after"], part["pending_after"]) == (1, 0, 1)
    sym = rep["kinds"]["symptom"]
    assert [s["key"] for s in sym["vanished_standards"]] == ["knocking sound"]
    assert sym["vanished_standards"][0]["aliases"] == ["Knocking sound from exhaust valve"]
    md = render_md(rep)
    assert "`scrubber` → `exhaust gas cleaning system`: scrubber" in md
    assert "| exhaust valve | candidate_term | 1 | `exhaust valve` |" in md
    assert "No differences" not in md
    # one kind only
    only = diff(snapshot(old.db), snapshot(new.db), kind="symptom")
    assert list(only["kinds"]) == ["symptom"]


def test_diff_of_a_store_against_itself_is_empty():
    s = LookupStore()
    s.resolve("M/E")
    s.resolve("No. 3 AE")
    s.resolve("Knocking sound", kind="symptom")
    rep = diff(snapshot(s.db, "a"), snapshot(s.db, "b"))
    assert not rep["changed"]
    for e in rep["kinds"].values():
        assert not (e["moved"] or e["now_pending"] or e["new_standards"] or e["vanished_standards"])
    assert "**No differences**" in render_md(rep)


def test_diff_cli_reads_both_files_read_only(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "diff_lookup", ROOT / "scripts" / "diff_lookup.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    old_db, new_db = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    with LookupStore(old_db) as s:
        s.resolve("M/E")
    shutil.copy(FIXTURES / "lookup_pre_p0.sqlite", new_db)  # an old-schema file diffs too
    before = (old_db.stat().st_size, new_db.read_bytes())
    out = tmp_path / "r" / "diff.md"
    assert mod.main([str(old_db), str(new_db), "--md", str(out), "--kind", "equipment"]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| equipment | 1 | 2 |" in text and "`auxiliary engine`" in text
    assert "| part |" not in text
    assert new_db.read_bytes() == before[1]  # not migrated by the diff


# --------------------------------------------------------------------------- 7. the queue page


def test_lookup_page_queue_section_golden(tmp_path):
    """The 'queued (not minted)' list on admin/lookup.html and admin/review.html."""
    from src.site import views
    from src.site.render import build, check_links
    from src.site.source import DictRecordSource
    from tests.unit.test_site_build import RECORD

    s = LookupStore()
    r = views.resolver_tables(s, [RECORD])
    assert r["pending"] == [] and r["pending_counts"] == {
        "equipment": 0,
        "part": 0,
        "symptom": 0,
        "total": 0,
    }
    out = tmp_path / "site0"
    build(DictRecordSource({RECORD["casefile_id"]: RECORD}), out, lookup=s)
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "Needs attention · 0 unreviewed · 0 without family · 0 queued" in page
    assert "Queued (not minted) · 0" in page and 'colspan="8">nothing queued' in page
    assert "Queued (not minted) · 0" in (out / "admin" / "review.html").read_text(encoding="utf-8")

    rec = json.loads(json.dumps(RECORD))
    rec["equipment"].append(dict(rec["equipment"][0], equipment_id="e2", name="右主机"))
    s.queue(
        "equipment",
        "右主机",
        "needs_translation",
        parts={"script": "zh"},
        source="casefile:" + rec["casefile_id"],
    )
    s.queue("symptom", "Vibration at the thrust bearing", "undecomposed")
    s.queue("symptom", "Vibration at the thrust bearing", "undecomposed")
    r = views.resolver_tables(s, [rec])
    assert [(p["raw"], p["reason"], p["hits"]) for p in r["pending"]] == [
        ("Vibration at the thrust bearing", "undecomposed", 2),
        ("右主机", "needs_translation", 1),
    ]
    assert (
        r["pending"][1]["casefiles"] == [rec["casefile_id"]]
        and r["pending"][1]["parts_text"] == '{"script": "zh"}'
    )
    assert (
        r["pending"][0]["fix_row"]
        == "symptom,Vibration at the thrust bearing,<standard_key>,reviewed,<family_key>"
    )
    assert r["pending"][1]["fix_row"] == "equipment,右主机,<standard_key>,reviewed"
    assert r["pending_counts"] == {"equipment": 1, "part": 0, "symptom": 1, "total": 2}
    assert r["fragmentation"]["n_pending"] == 2
    assert r["queue"] == []  # pending rows are not standards: not in queue 1 / 2

    out = tmp_path / "site"
    build(DictRecordSource({rec["casefile_id"]: rec}), out, lookup=s)
    assert check_links(out) == []
    page = (out / "admin" / "lookup.html").read_text(encoding="utf-8")
    assert "Needs attention · 0 unreviewed · 0 without family · 2 queued" in page
    golden = (
        '<h3 class="pending-title">Queued (not minted) · 2\n'
        '  <span class="muted small">equipment 1 · part 0 · symptom 1</span></h3>\n'
    )
    assert golden in page
    rows = [ln for ln in page.splitlines() if ln.strip().startswith('<tr class="pending"')]
    assert rows == [
        '    <tr class="pending" data-pending-kind="symptom" data-fix="symptom,Vibration at the thrust bearing,&lt;standard_key&gt;,reviewed,&lt;family_key&gt;">',
        '    <tr class="pending" data-pending-kind="equipment" data-fix="equipment,右主机,&lt;standard_key&gt;,reviewed">',
    ]
    second = page.split(rows[1], 1)[1].split("</tr>", 1)[0]
    assert (
        '<td class="canon">右主机</td>\n'
        '      <td><span class="chip kind">equipment</span></td>\n'
        '      <td><span class="chip flag">needs_translation</span></td>\n'
        '      <td class="n">1</td>\n'
        f'      <td class="small mono muted">casefile:{rec["casefile_id"]}</td>\n'
        '      <td class="small mono">{&#34;script&#34;: &#34;zh&#34;}</td>\n'
        f'      <td class="small"><a href="../casefile/{rec["casefile_id"]}.html">{rec["casefile_id"][:6]}</a></td>\n'
    ) in second
    assert [
        ln for ln in page.splitlines() if ln.strip().startswith("<tr data-kind=")
    ] == []  # not queue 1 rows
    review = (out / "admin" / "review.html").read_text(encoding="utf-8")
    assert "Resolver queue — queued, not minted" in review and golden in review
