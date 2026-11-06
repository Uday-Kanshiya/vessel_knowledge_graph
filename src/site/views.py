"""
View helpers over a casefile_extract.v2 record (12 §1: the UI is a view).

Everything here is derived at render time from fields the record already carries.
Nothing is written back; nothing here is a number the extractor was asked for.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from src.resolution.normalize import decompose_symptom, normalize_symptom

# --------------------------------------------------------------------------- dates


def parse_date(value: str | None) -> datetime | None:
    """Display-only parse of the record's ISO dates; the wall-clock part is kept as written."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    v = v.removesuffix("Z")
    v = re.sub(r"[+-]\d{2}:?\d{2}$", "", v) if "T" in v else v
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def fmt_date(value: str | None, with_time: bool = False) -> str:
    d = parse_date(value)
    if d is None:
        return value or ""
    return d.strftime("%d %b %Y %H:%M") if with_time else d.strftime("%d %b %Y")


def date_range(record: dict) -> tuple[str | None, str | None]:
    """min/max over fault first_observed_at and action dates — ISO dates, display only."""
    dates: list[datetime] = []
    for f in record.get("faults", []):
        d = parse_date(f.get("first_observed_at"))
        if d:
            dates.append(d)
    for a in record.get("action_logs", []):
        for key in ("date_performed", "date_reported"):
            d = parse_date(a.get(key))
            if d:
                dates.append(d)
        d = parse_date((a.get("outcome") or {}).get("outcome_date"))
        if d:
            dates.append(d)
    if not dates:
        return None, None
    return min(dates).strftime("%Y-%m-%d"), max(dates).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- tallies

OUTCOME_ORDER = [
    "resolved",
    "partial",
    "no_effect",
    "made_worse",
    "conclusive",
    "inconclusive",
    "no_information",
    "unknown",
]
OUTCOME_GROUP = {
    "resolved": "good",
    "conclusive": "good",
    "partial": "mid",
    "inconclusive": "mid",
    "no_effect": "bad",
    "made_worse": "bad",
    "no_information": "bad",
    "unknown": "none",
}


def outcome_tally(record: dict) -> list[tuple[str, int]]:
    c = Counter(
        (a.get("outcome") or {}).get("value") or "unknown" for a in record.get("action_logs", [])
    )
    return [(k, c[k]) for k in OUTCOME_ORDER if c.get(k)]


def kind_tally(record: dict) -> list[tuple[str, int]]:
    c = Counter(a.get("kind") or "unknown" for a in record.get("action_logs", []))
    return sorted(c.items(), key=lambda kv: -kv[1])


# --------------------------------------------------------------------------- symptom normalisation
# The symptom key function lives in the resolver now (src/resolution/normalize.py, docs/13 §8);
# re-exported here so the site code and its tests keep one name. Since A1 (2026-10-25) the key is
# the failure-mode core of `decompose_symptom(wording, equipment_type)`: pass the record's own
# equipment type (`fault_context`) or the equipment words stay in the key.
normalise_symptom = normalize_symptom


def equipment_type_of(record: dict, fault: dict) -> str | None:
    """The equipment_type of the equipment row a fault points at (None when unlinked)."""
    eid = fault.get("equipment_id")
    for e in record.get("equipment", []) or []:
        if e.get("equipment_id") == eid:
            return e.get("equipment_type") or None
    return None


def equipment_context(record: dict, entry: dict) -> dict[str, str | None]:
    """The record context the resolver's equipment cleaner takes (`LookupStore.find(...,
    context=)`, docs/13 §3): the record's vessel and the entry's own type / maker / model / serial,
    which the cleaner strips from the name (the record keeps them as fields)."""
    return {
        "vessel_name": record.get("vessel_name") or None,
        "equipment_type": entry.get("equipment_type") or None,
        "maker": entry.get("maker") or None,
        "model": entry.get("model") or None,
        "serial": entry.get("serial") or None,
    }


def canonical_position(text: str | None, config=None) -> str:
    """The record's position field as the resolver would say it: left -> port, right -> starboard,
    stbd -> starboard, fwd -> forward (config/modifier_terms.csv). The field itself is never
    rewritten; this is the view."""
    if not text:
        return ""
    from src.resolution.normalize import DEFAULT_CONFIG

    table = (config or DEFAULT_CONFIG).modifier_terms
    out = []
    for tok in str(text).split():
        bare = tok.strip(".,;:()[]/-&").lower()
        out.append(table.get(bare, tok))
    return " ".join(out)


def fault_context(record: dict, fault: dict) -> dict[str, str | None]:
    """The record context the resolver's symptom key function takes (`LookupStore.find(...,
    context=)`): the record's own equipment type and vessel name."""
    return {
        "equipment_type": equipment_type_of(record, fault),
        "vessel_name": record.get("vessel_name") or None,
    }


def fault_symptom_key(record: dict, fault: dict, config=None) -> str:
    """The symptom key of one fault row as the resolver computes it."""
    return normalize_symptom(fault.get("symptom"), equipment_type_of(record, fault), config)


def fault_decomposition(record: dict, lookup: Any = None) -> dict[str, dict]:
    """Per fault_id, what the symptom wording decomposed into (docs/13 §8), for the casefile
    page: core, component (the part standard's display name when the resolver knows it, else the
    canonical key), location, modifiers. Only faults with a component or a location are listed."""
    out: dict[str, dict] = {}
    cfg = lookup.config if lookup is not None else None
    for f in record.get("faults", []) or []:
        if not f.get("symptom"):
            continue
        d = decompose_symptom(f["symptom"], equipment_type_of(record, f), cfg)
        if not (d.component or d.location or d.condition or d.frequency):
            continue
        comp = d.component
        if comp and lookup is not None:
            sid = lookup.find_key(comp, "part")
            if sid is not None:
                comp = lookup.display_name(sid)
        out[f["fault_id"]] = {
            "core": d.stem or d.core,
            "component": comp,
            "location": d.location,
            "modifiers": " ".join(d.modifiers),
            "condition": d.condition,
            "frequency": d.frequency,
        }
    return out


# --------------------------------------------------------------------------- record views


def equipment_by_id(record: dict) -> dict[str, dict]:
    return {e["equipment_id"]: e for e in record.get("equipment", [])}


def problems_by_id(record: dict) -> dict[str, dict]:
    return {p["problem_id"]: p for p in record.get("problems", [])}


def faults_by_equipment(record: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for f in record.get("faults", []):
        out[f.get("equipment_id") or "_none"].append(f)
    return out


def actions_by_fault(record: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for a in sorted(
        record.get("action_logs", []), key=lambda a: (a.get("seq") or 0, a.get("action_id"))
    ):
        out[a.get("fault_id") or "_none"].append(a)
    return out


def cited_mail_ids(record: dict) -> list[str]:
    ids: set[str] = set()
    for fam in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(fam, []):
            ids.update(_mail_ids_of(row))
    return sorted(ids)


def cited_shas(record: dict) -> list[str]:
    shas: set[str] = set()
    for fam in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(fam, []):
            shas.update(_shas_of(row))
    return sorted(shas)


def _mail_ids_of(row: dict) -> set[str]:
    ids: set[str] = set()
    prov = row.get("provenance") or {}
    ids.update(prov.get("mail_ids") or [])
    for s in prov.get("supporting") or []:
        ids.update(s.get("mail_ids") or [])
    if row.get("asserted_in_mail"):
        ids.add(row["asserted_in_mail"])
    oim = (row.get("outcome") or {}).get("outcome_in_mail")
    if oim:
        ids.add(oim)
    return ids


def _shas_of(row: dict) -> set[str]:
    shas: set[str] = set()
    prov = row.get("provenance") or {}
    shas.update(prov.get("attachment_shas") or [])
    for s in prov.get("supporting") or []:
        shas.update(s.get("attachment_shas") or [])
    return shas


def row_id(row: dict) -> str:
    for k in ("action_id", "fault_id", "equipment_id", "problem_id"):
        if row.get(k):
            return row[k]
    return ""


def quote_cites(record: dict) -> list[dict]:
    """Every evidence quote in the record with the mails it may live in:
    [{row_id, family, quote, kind, mail_ids}]. kind: assertion | supporting | outcome.
    A row that cites several mails carries ONE assertion quote; the renderer locates it."""
    out: list[dict] = []
    for fam in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(fam, []):
            rid = row_id(row)
            prov = row.get("provenance") or {}
            targets = list(prov.get("mail_ids") or []) or (
                [row["asserted_in_mail"]] if row.get("asserted_in_mail") else []
            )
            if prov.get("evidence_quote") and targets:
                out.append(
                    {
                        "row_id": rid,
                        "family": fam,
                        "quote": prov["evidence_quote"],
                        "kind": "assertion",
                        "mail_ids": targets,
                    }
                )
            for s in prov.get("supporting") or []:
                if s.get("evidence_quote") and s.get("mail_ids"):
                    out.append(
                        {
                            "row_id": rid,
                            "family": fam,
                            "quote": s["evidence_quote"],
                            "kind": "supporting",
                            "mail_ids": list(s["mail_ids"]),
                        }
                    )
            oc = row.get("outcome") or {}
            if oc.get("evidence_quote") and oc.get("outcome_in_mail"):
                out.append(
                    {
                        "row_id": rid,
                        "family": fam,
                        "quote": oc["evidence_quote"],
                        "kind": "outcome",
                        "mail_ids": [oc["outcome_in_mail"]],
                    }
                )
    return out


# --------------------------------------------------------------------------- validation flags


def parse_flag(flag: str) -> dict:
    """'quote_not_found:a4.outcome' -> kind, row_id, field, extra."""
    parts = flag.split(":")
    kind = parts[0]
    row, field, extra = None, None, None
    if len(parts) > 1:
        tgt = parts[1]
        if "." in tgt:
            row, field = tgt.split(".", 1)
        elif re.fullmatch(r"[aefp]\d+", tgt):
            row = tgt
        else:
            extra = tgt
    if len(parts) > 2:
        extra = ":".join(parts[2:])
    return {"raw": flag, "kind": kind, "row_id": row, "field": field, "extra": extra}


def flags_by_row(record: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for fl in record.get("validation_flags", []) or []:
        p = parse_flag(fl)
        out[p["row_id"] or "_record"].append(p)
    return out


# --------------------------------------------------------------------------- mail text


_HEADER_KEYS = ("From", "Subject", "Date", "To", "CC", "Cc")


def split_mail_text(text: str) -> tuple[dict[str, str], str]:
    """The cached text starts with 'Key: value' header lines separated by blank lines."""
    headers: dict[str, str] = {}
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        m = re.match(r"^(From|Subject|Date|To|CC|Cc|Attachments?):\s*(.*)$", line)
        if not m:
            break
        headers[m.group(1).capitalize() if m.group(1) != "CC" else "CC"] = m.group(2).strip()
        i += 1
    body = "\n".join(lines[i:]).strip("\n")
    return headers, body


# --------------------------------------------------------------------------- lookup tables


def lookup_tables(records: Iterable[dict], links: Any = None) -> dict[str, Any]:
    """Raw -> canonical tables for admin/lookup.html. `links` (SymptomLinks, 12 §3) only
    changes the ORDER and nesting of the symptom rows: a narrower group follows its broader
    group with `depth` > 0, `broader` and `note` set. Every count stays per group."""
    equipment: dict[str, dict[str, dict]] = defaultdict(
        dict
    )  # type -> lowercased name -> {name, count, casefiles, makers}
    parts: dict[str, dict] = {}
    roles: Counter = Counter()
    orgs: dict[str, dict] = {}
    symptoms: dict[str, dict[str, dict]] = defaultdict(
        dict
    )  # normalised -> raw -> {count, casefiles}
    for rec in records:
        cid = rec["casefile_id"]
        for e in rec.get("equipment", []):
            et = e.get("equipment_type") or "(untyped)"
            key = (e.get("name") or "").strip().lower()
            slot = equipment[et].setdefault(
                key, {"name": e.get("name") or "", "count": 0, "casefiles": set(), "makers": set()}
            )
            slot["count"] += 1
            slot["casefiles"].add(cid)
            mk = " ".join(x for x in (e.get("maker"), e.get("model")) if x and x != "NA")
            if mk:
                slot["makers"].add(mk)
        for a in rec.get("action_logs", []):
            roles[a.get("performed_by_role") or "unknown"] += 1
            if a.get("advised_by_org"):
                o = orgs.setdefault(a["advised_by_org"].strip(), {"count": 0, "casefiles": set()})
                o["count"] += 1
                o["casefiles"].add(cid)
            for p in a.get("parts") or []:
                name = p if isinstance(p, str) else (p.get("name") or str(p))
                slot = parts.setdefault(
                    name.strip().lower(), {"name": name, "count": 0, "casefiles": set()}
                )
                slot["count"] += 1
                slot["casefiles"].add(cid)
        for f in rec.get("faults", []):
            norm = fault_symptom_key(rec, f) or "(empty)"
            raw = (f.get("symptom") or "").strip()
            slot = symptoms[norm].setdefault(
                raw, {"count": 0, "casefiles": set(), "positions": set()}
            )
            slot["count"] += 1
            slot["casefiles"].add(cid)
            if f.get("position"):
                slot["positions"].add(str(f["position"]))

    def _sets_to_lists(d):
        if isinstance(d, dict):
            return {k: _sets_to_lists(v) for k, v in d.items()}
        if isinstance(d, set):
            return sorted(d)
        return d

    eq_rows = []
    for et, names in sorted(equipment.items()):
        variants = sorted(names.values(), key=lambda v: -v["count"])
        eq_rows.append(
            {"type": et, "variants": _sets_to_lists(variants), "n_variants": len(variants)}
        )
    sym_by_norm: dict[str, dict] = {}
    for norm, raws in symptoms.items():
        total = sum(v["count"] for v in raws.values())
        sym_by_norm[norm] = {
            "norm": norm,
            "n_raw": len(raws),
            "total": total,
            "raws": _sets_to_lists(
                [{"raw": r, **v} for r, v in sorted(raws.items(), key=lambda kv: -kv[1]["count"])]
            ),
            "depth": 0,
            "broader": None,
            "note": None,
            "n_narrower": 0,
        }
    order = sorted(sym_by_norm.values(), key=lambda r: (-r["n_raw"], -r["total"], r["norm"]))
    sym_rows: list[dict] = []
    if links:
        # a narrower group is listed under its broader group; counts are NOT rolled up
        def _emit(row: dict, depth: int) -> None:
            row["depth"] = depth
            sym_rows.append(row)
            kids = [sym_by_norm[n] for n in links.narrower(row["norm"]) if n in sym_by_norm]
            row["n_narrower"] = len(kids)
            for kid in sorted(kids, key=lambda r: (-r["n_raw"], -r["total"], r["norm"])):
                link = links.note_of.get(kid["norm"], {})
                kid["broader"] = row["norm"]
                kid["note"] = link.get("note")
                kid["reviewed_by"] = link.get("reviewed_by")
                kid["date"] = link.get("date")
                _emit(kid, depth + 1)

        for row in order:
            if links.broader_of.get(row["norm"]) in sym_by_norm:
                continue  # emitted under its broader row
            _emit(row, 0)
    else:
        sym_rows = order
    return {
        "equipment": eq_rows,
        "parts": _sets_to_lists(sorted(parts.values(), key=lambda v: -v["count"])),
        "roles": sorted(roles.items(), key=lambda kv: -kv[1]),
        "orgs": _sets_to_lists(
            sorted([{"name": k, **v} for k, v in orgs.items()], key=lambda v: -v["count"])
        ),
        "symptoms": sym_rows,
    }


def resolver_tables(lookup: Any, records: Iterable[dict]) -> dict[str, Any]:
    """The admin lookup page read from the RESOLVER (12 §5.1, 13 §8), not from the records:

      equipment  equipment_type -> standard (display) -> aliases (raw spelling, hits, method, the
                 cleaning chain and what it stripped: vessel / glossary / record / qualifier) -> casefiles
      symptoms   family -> standard (display, how its family was assigned) -> aliases -> casefiles,
                 plus ON: the distinct component · location · modifier combinations the standard's
                 spellings carried (alias.parts, 13 §8), component shown by its part standard's name
      queues     auto-minted standards; symptom standards with no family (hits desc), each with the
                 CSV row that would fix it (editing is CSV + re-run, not the UI); and the third
                 queue, `pending`: spellings the mint guard refused (13 §4), never minted, never
                 found, each with the seed-CSV row that promotes it

    The records are used only to say WHICH casefiles carry each spelling; nothing is written.
    Display names come from `display_name()` (a view, never stored)."""
    from src.resolution.lookup import UNCLASSIFIED, raw_key_of

    # raw spelling -> casefiles (and, for equipment, the equipment_type the record gave it)
    cases: dict[tuple[str, str], set[str]] = defaultdict(set)
    eq_type_of: dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        cid = rec["casefile_id"]
        for e in rec.get("equipment", []) or []:
            name = (e.get("name") or "").strip()
            if name:
                rk = raw_key_of(name)
                cases[("equipment", rk)].add(cid)
                eq_type_of[rk][(e.get("equipment_type") or "(untyped)").strip()] += 1
        for f in rec.get("faults", []) or []:
            sym = (f.get("symptom") or "").strip()
            if sym:
                cases[("symptom", raw_key_of(sym))].add(cid)
        for a in rec.get("action_logs", []) or []:
            for part in a.get("parts") or []:
                name = (part if isinstance(part, str) else (part.get("name") or "")).strip()
                if name:
                    cases[("part", raw_key_of(name))].add(cid)

    def _aliases(kind: str) -> dict[int, list[dict]]:
        by_std: dict[int, list[dict]] = defaultdict(list)
        for a in lookup.alias_rows(kind):
            by_std[a["standard_id"]].append(
                {
                    "raw": a["raw_term"],
                    "hits": a["hit_count"],
                    "method": a["method"],
                    "casefiles": sorted(cases.get((kind, a["raw_key"]), set())),
                    "equipment_types": sorted(eq_type_of.get(a["raw_key"], {})),
                    "parts": a.get("parts") or {},
                    "chain": "+".join([*((a.get("parts") or {}).get("steps") or []), a["method"]]),
                    "stripped": " · ".join(
                        f"{k}: {v}"
                        for k, v in ((a.get("parts") or {}).get("stripped") or {}).items()
                    ),
                }
            )
        return by_std

    def _on(aliases: list[dict]) -> list[dict]:
        """The ON / WHEN column of a symptom standard: distinct (component, location, modifiers,
        condition, frequency) of its spellings with hit counts; the component as its part
        standard's display name (13 §8)."""
        combos: Counter = Counter()
        names: dict[str, str] = {}
        for a in aliases:
            p = a["parts"]
            comp = p.get("component")
            if comp and comp not in names:
                sid = p.get("component_standard_id") or lookup.find_key(comp, "part")
                names[comp] = lookup.display_name(sid) if sid is not None else comp
            key = (
                comp,
                p.get("location"),
                " ".join(p.get("modifiers") or []),
                p.get("condition"),
                p.get("frequency"),
            )
            if any(key):
                combos[key] += a["hits"]
        return [
            {
                "component": names.get(c) if c else None,
                "location": loc,
                "modifiers": mods,
                "condition": cond,
                "frequency": freq,
                "n": n,
            }
            for (c, loc, mods, cond, freq), n in sorted(
                combos.items(), key=lambda kv: (-kv[1], str(kv[0]))
            )
        ]

    # ---- equipment: type -> standards
    eq_alias = _aliases("equipment")
    eq_groups: dict[str, list[dict]] = defaultdict(list)
    for s in lookup.standards(kind="equipment"):
        if s.status == "merged":
            continue
        al = eq_alias.get(s.id, [])
        types = Counter()
        for a in al:
            for t in a["equipment_types"]:
                types[t] += 1
        etype = types.most_common(1)[0][0] if types else "(no record)"
        eq_groups[etype].append(
            {
                "id": s.id,
                "key": s.key,
                "display": s.display,
                "status": s.status,
                "hits": s.hits,
                "aliases": al,
                "casefiles": sorted({c for a in al for c in a["casefiles"]}),
            }
        )
    equipment = [
        {
            "type": t,
            "standards": sorted(rows, key=lambda r: (-r["hits"], r["key"])),
            "n_standards": len(rows),
        }
        for t, rows in sorted(eq_groups.items())
    ]

    # ---- symptoms: family -> standards
    sym_alias = _aliases("symptom")
    fams = lookup.families("symptom")
    by_fam: dict[str, list[dict]] = defaultdict(list)
    for s in lookup.standards(kind="symptom"):
        if s.status == "merged":
            continue
        al = sym_alias.get(s.id, [])
        by_fam[s.family_key or UNCLASSIFIED].append(
            {
                "id": s.id,
                "key": s.key,
                "display": s.display,
                "status": s.status,
                "hits": s.hits,
                "family_method": s.family_method,
                "aliases": al,
                "on": _on(al),
                "casefiles": sorted({c for a in al for c in a["casefiles"]}),
                "fix_row": f"symptom,{(al[0]['raw'] if al else s.key)},{s.key},<family_key>",
            }
        )
    symptoms = []
    for f in fams:
        rows = by_fam.get(f.key, [])
        symptoms.append(
            {
                "key": f.key,
                "display": f.display,
                "description": f.description,
                "patterns": f.patterns,
                "standards": sorted(rows, key=lambda r: (-r["hits"], r["key"])),
                "n_standards": len(rows),
                "hits": sum(r["hits"] for r in rows),
                "pseudo": False,
            }
        )
    uncl = by_fam.get(UNCLASSIFIED, [])
    symptoms.append(
        {
            "key": UNCLASSIFIED,
            "display": "unclassified",
            "description": "standards with no family yet (second review queue)",
            "patterns": [],
            "standards": sorted(uncl, key=lambda r: (-r["hits"], r["key"])),
            "n_standards": len(uncl),
            "hits": sum(r["hits"] for r in uncl),
            "pseudo": True,
        }
    )

    # ---- review queue: ONE list, one entry per standard that needs attention, merged by id.
    # reasons: "unreviewed" (auto-minted, 13 §4 case b) and/or "no_family" (symptom with no
    # family, 13 §8). Hits desc, then symptoms first (they shape the facets), then key.
    kind_rank = {"symptom": 0, "equipment": 1, "part": 2}
    needing: dict[int, dict] = {}

    def _entry(s) -> dict:
        return {
            "id": s.id,
            "kind": s.kind,
            "display": s.display,
            "key": s.key,
            "hits": s.hits,
            "aliases": list(s.aliases),
            "reasons": [],
            "fix_row": f"{s.kind},{(s.aliases[0] if s.aliases else s.key)},{s.key},"
            + ("<family_key>" if s.kind == "symptom" else ""),
        }

    for s in lookup.review_queue():
        needing.setdefault(s.id, _entry(s))["reasons"].append("unreviewed")
    for s in lookup.unclassified("symptom"):
        needing.setdefault(s.id, _entry(s))["reasons"].append("no_family")
    queue = sorted(
        needing.values(), key=lambda q: (-q["hits"], kind_rank.get(q["kind"], 9), q["key"])
    )
    queue_counts = {
        "unreviewed": sum("unreviewed" in q["reasons"] for q in queue),
        "no_family": sum("no_family" in q["reasons"] for q in queue),
        "total": len(queue),
    }
    queue_by_kind = {
        k: sum(q["kind"] == k for q in queue) for k in ("equipment", "part", "symptom")
    }

    # ---- queue 3: queued, not minted (13 §4 case b'). One row per pending spelling, hits desc.
    # The fix is a seed-CSV row (promote) or `LookupStore.dismiss`; the standard_key is the
    # human's call, so the row carries a placeholder.
    pending = []
    for p in lookup.pending():
        tail = ",<family_key>" if p.kind == "symptom" else ""
        pending.append(
            {
                "kind": p.kind,
                "raw": p.raw_term,
                "raw_key": p.raw_key,
                "reason": p.reason,
                "hits": p.hit_count,
                "source": p.source,
                "parts": p.parts,
                "parts_text": json.dumps(p.parts, ensure_ascii=False, sort_keys=True)
                if p.parts
                else "",
                "casefiles": sorted(cases.get((p.kind, p.raw_key), set())),
                "first_seen": p.first_seen,
                "last_seen": p.last_seen,
                "fix_row": f"{p.kind},{p.raw_term},<standard_key>,reviewed{tail}",
            }
        )
    pending_counts = {
        k: sum(x["kind"] == k for x in pending) for k in ("equipment", "part", "symptom")
    }
    pending_counts["total"] = len(pending)

    # ---- queue 4: possible duplicates (13 §8): symptom standards in one family whose cores read
    # alike, each pair with the table that would merge it (read-only; near_duplicates.scan)
    from src.resolution.near_duplicates import scan as _scan_duplicates

    duplicates = _scan_duplicates(lookup)
    frag = lookup.fragmentation()
    return {
        "equipment": equipment,
        "symptoms": symptoms,
        "families": [{"key": f.key, "display": f.display} for f in fams],
        "n_families_used": sum(1 for g in symptoms if not g["pseudo"] and g["n_standards"]),
        "queue": queue,
        "queue_counts": queue_counts,
        "queue_by_kind": queue_by_kind,
        "pending": pending,
        "pending_counts": pending_counts,
        "duplicates": duplicates,
        "fragmentation": frag,
        "parts_note": "parts have standards but no family layer yet (13 §8)",
    }


# --------------------------------------------------------------------------- journey


def _ids_of(items) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            rid = row_id(it)
            if rid:
                out.append(rid)
    return out


def _changed_fields(update_row: dict) -> list[str]:
    skip = {"equipment_id", "problem_id", "fault_id", "action_id", "provenance_add", "provenance"}
    return [k for k in update_row if k not in skip]


def window_inputs_view(
    inputs: dict | None,
    mails_available: set[str] | None = None,
    attachments_available: set[str] | None = None,
) -> dict | None:
    """The `inputs` audit block of a window (docs/14) as the journey panel shows it: a copy with
    `has_page` on every mail and attachment, a per-family memory count, and a sent tally.
    None when the window was written by a run that did not capture inputs."""
    if not inputs:
        return None
    mails_available = mails_available or set()
    attachments_available = attachments_available or set()
    summary = inputs.get("record_summary") or {}
    mails = []
    sent = 0
    for m in inputs.get("mails") or []:
        atts = []
        for a in m.get("attachments") or []:
            a2 = dict(a)
            a2["has_page"] = bool(a.get("sha256")) and a["sha256"] in attachments_available
            a2["digest_summary"] = (a.get("digest_as_sent") or {}).get("summary")
            atts.append(a2)
            sent += bool(a.get("sent_to_model"))
        m2 = dict(m)
        m2["attachments"] = atts
        m2["has_page"] = m.get("mail_id") in mails_available
        mails.append(m2)
    attempts = inputs.get("attempts") or []
    return {
        "prompt_version": inputs.get("prompt_version"),
        "memory": {
            fam: len(summary.get(fam) or [])
            for fam in ("equipment", "problems", "faults", "action_logs")
        },
        "next_free_ids": inputs.get("next_free_ids") or summary.get("next_free_ids") or {},
        "mails": mails,
        "n_mails_sent": sum(1 for m in mails if not m.get("skipped_as_twin_of")),
        "n_attachments": sum(len(m["attachments"]) for m in mails),
        "n_attachments_sent": sent,
        "tokens": inputs.get("tokens") or {},
        "attempts": attempts,
        "corrections": [a for a in attempts if a.get("feedback")],
    }


def journey(
    record: dict,
    windows: list[dict],
    run_log: dict,
    prompt_for: Callable[[int], str | None] | None = None,
    mails_available: set[str] | None = None,
    attachments_available: set[str] | None = None,
) -> dict:
    """Window-by-window build of one record (12 §5.2), from the audit files on disk.

    Uses windows/wNNN.json when present (full detail), else run_log.runs[].windows (summary).
    `prompt_for(index)` returns the window's .prompt.txt (RecordSource.windows_prompt) or None;
    with it, and `inputs` in the audit file, every step gets the "what the model saw" panel.
    """
    steps: list[dict] = []
    log_windows: dict[int, dict] = {}
    for run in run_log.get("runs") or []:
        for w in run.get("windows") or []:
            log_windows[w.get("index")] = w

    if windows:
        for w in windows:
            idx = w.get("index")
            applied = w.get("applied") or {}
            raw = w.get("raw_response") or {}
            upd_rows: list[dict] = []
            for fam, rows in (raw.get("update") or {}).items():
                for r in rows or []:
                    rid = row_id(r)
                    upd_rows.append(
                        {
                            "family": fam,
                            "row_id": rid,
                            "fields": _changed_fields(r),
                            "mail_ids": (r.get("provenance_add") or {}).get("mail_ids") or [],
                        }
                    )
            added = {fam: _ids_of(ids) for fam, ids in (applied.get("added") or {}).items()}
            steps.append(
                {
                    "index": idx,
                    "pagenums": w.get("pagenums") or [],
                    "mail_ids": w.get("mail_ids") or [],
                    "n_attachments": len(w.get("attachment_shas") or []),
                    "attempts": w.get("attempts"),
                    "rejected": bool(w.get("rejected")),
                    "error": w.get("error"),
                    "seconds": w.get("seconds"),
                    "prompt_chars": w.get("prompt_chars"),
                    "added": added,
                    "n_added": sum(len(v) for v in added.values()),
                    "updates": upd_rows,
                    "flags": applied.get("flags") or [],
                    "dropped_rows": applied.get("dropped_rows") or [],
                    "split_quotes": w.get("split_quotes"),
                    "translated_quotes": w.get("translated_quotes"),
                    "mail_aliases": w.get("mail_aliases") or {},
                    "notes": raw.get("window_notes") or "",
                    "has_detail": True,
                    # what the model saw (docs/14); None for audit files from earlier runs
                    "inputs": window_inputs_view(
                        w.get("inputs"), mails_available, attachments_available
                    ),
                    "tokens": (w.get("inputs") or {}).get("tokens"),
                    "prompt_text": prompt_for(idx) if (prompt_for and w.get("inputs")) else None,
                }
            )
    else:
        for idx in sorted(log_windows):
            w = log_windows[idx]
            added = {fam: _ids_of(ids) for fam, ids in (w.get("added") or {}).items()}
            upd = {fam: _ids_of(ids) for fam, ids in (w.get("updated") or {}).items()}
            steps.append(
                {
                    "index": idx,
                    "pagenums": w.get("pagenums") or [],
                    "mail_ids": [],
                    "n_attachments": None,
                    "attempts": None,
                    "rejected": False,
                    "error": None,
                    "seconds": w.get("seconds"),
                    "prompt_chars": None,
                    "added": added,
                    "n_added": sum(len(v) for v in added.values()),
                    "updates": [
                        {"family": fam, "row_id": rid, "fields": [], "mail_ids": []}
                        for fam, ids in upd.items()
                        for rid in ids
                    ],
                    "flags": w.get("flags") or [],
                    "dropped_rows": [],
                    "split_quotes": None,
                    "translated_quotes": None,
                    "mail_aliases": {},
                    "notes": "",
                    "has_detail": False,
                    "inputs": None,
                    "tokens": w.get("tokens"),
                    "prompt_text": None,
                }
            )

    runs = []
    for run in run_log.get("runs") or []:
        runs.append(
            {
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "extraction_version": run.get("extraction_version"),
                "n_windows": len(run.get("windows") or []),
                "tokens": run.get("tokens") or {},
                "attachments": run.get("attachments") or {},
                "error": run.get("error"),
                "mail_aliases": run.get("mail_aliases") or {},
            }
        )
    consolidate = run_log.get("consolidate") or {}
    postprocess = run_log.get("postprocess") or {}
    return {
        "steps": steps,
        "runs": runs,
        "consolidate": consolidate,
        "postprocess": postprocess,
        "validation_flags": [parse_flag(f) for f in record.get("validation_flags", []) or []],
        "needs_review": bool(record.get("needs_review")),
        "lineage": lineage(record, steps),
    }


def lineage(record: dict, steps: list[dict]) -> dict[str, dict]:
    """row id -> minted in window, touched in windows (with fields), final flags."""
    out: dict[str, dict] = {}
    for fam in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(fam, []):
            rid = row_id(row)
            out[rid] = {
                "family": fam,
                "minted": None,
                "touched": [],
                "flags": [],
                "label": row.get("action")
                or row.get("symptom")
                or row.get("name")
                or row.get("statement")
                or "",
            }
    for st in steps:
        for fam, ids in st["added"].items():
            for rid in ids:
                if rid in out and out[rid]["minted"] is None:
                    out[rid]["minted"] = st["index"]
        for u in st["updates"]:
            if u["row_id"] in out:
                out[u["row_id"]]["touched"].append(
                    {"window": st["index"], "fields": u["fields"], "mail_ids": u["mail_ids"]}
                )
    for fl in record.get("validation_flags", []) or []:
        p = parse_flag(fl)
        if p["row_id"] in out:
            out[p["row_id"]]["flags"].append(p)
    return out
