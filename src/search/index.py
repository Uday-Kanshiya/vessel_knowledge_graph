"""
Search index projection (12 §3): one entry per casefile, searchable text by weight, facets.

`project(record, links, lookup)` is the only function the search side knows about a record. The
static site embeds the list in index.html and also writes search_index.json; a served build
(12 §7 stage 2) feeds the same entries to SQLite FTS5 or a Mongo text index.

Weights (12 §3): title 5 · symptoms/alarms 4 · causes/problems 3 · actions/findings/equipment/parts 2 · quotes 1.

Facets (schema v2) list CANONICAL IDS ONLY; wording is never a facet value (12 §3). With a
`LookupStore` (opened read-only: `find()`, nothing is minted at site-build time):

    equipment_type   the enum from the record (unchanged)
    equipment_std    standard ids of equipment[].name; a name with no alias yet -> "__unresolved__"
    symptom_family   family keys of the faults' symptom standards; none -> "unclassified"
    symptom          standard ids of faults[].symptom (NOT the wording); unresolved -> "__unresolved__"
    symptom_broad    what a Symptom click matches: the standards' ids plus the ids of every broader
                     term above them (config/symptom_links.json), so a broader term reaches its
                     narrower groups and a narrower term matches only itself
    symptom_on       ON / WHERE from the symptom table (13 §8): the part-standard id of the
                     component each symptom wording named (no part standard -> "__unresolved__")
                     and the location it named (a location-table term, labelled "in <location>").
                     The UI nests it under Symptom in place of the standards list

The index also carries `labels` (id -> display name, computed by the resolver's display view),
`parents` (child facet -> parent facet) and `facet_parent_vals` (child value -> parent values) so
the UI can nest equipment_type ▸ equipment_std and symptom_family ▸ symptom_on. Without a lookup
(`--no-lookup`, or no DB) the index falls back to v1 wording facets and says so (`canonical: false`).
Text fields are unchanged: the raw symptom wording is still indexed at weight 4.
"""

from __future__ import annotations

from typing import Any

from src.resolution.lookup import UNCLASSIFIED, UNRESOLVED, LookupStore
from src.site.symptom_links import SymptomLinks
from src.site.views import date_range, normalise_symptom, outcome_tally
from src.site.views import equipment_context as _equipment_context
from src.site.views import equipment_type_of as _equipment_type_of

SCHEMA_V2 = "site_search_index.v2"
SCHEMA_V1 = "site_search_index.v1"

FIELD_WEIGHTS = {
    "title": 5,
    "symptoms": 4,
    "alarms": 4,
    "causes": 3,
    "problems": 3,
    "actions": 2,
    "findings": 2,
    "equipment": 2,
    "parts": 2,
    "quotes": 1,
}

# the ON / WHERE sub-facet: component (part standard id) · location of the symptom wording (13 §8)
ON_FACET = "symptom_on"
PARENTS = {
    "equipment_std": "equipment_type",
    "symptom": "symptom_family",
    ON_FACET: "symptom_family",
}
# the condition sub-facet (13 §8) joins `parents` only when some value has >= 2 casefiles
CONDITION_FACET = "condition"
CONDITION_MIN_CASEFILES = 2
FACET_MATCH = {"symptom": "symptom_broad"}


def _s(x: Any) -> str:
    return x.strip() if isinstance(x, str) else ""


def _canonical_facets(
    equipment: list[dict],
    faults: list[dict],
    links: SymptomLinks | None,
    lookup: LookupStore,
    labels: dict[str, dict[str, str]],
    parent_vals: dict[str, dict[str, set[str]]],
    record: dict | None = None,
) -> dict[str, list[str]]:
    """The id facets of one record; fills `labels` / `parent_vals` as a side effect."""
    eq_std: set[str] = set()
    for e in equipment:
        name, etype = _s(e.get("name")), _s(e.get("equipment_type"))
        if not name:
            continue
        r = lookup.find(name, kind="equipment", context=_equipment_context(record or {}, e))
        val = str(r.standard_id) if r else UNRESOLVED
        eq_std.add(val)
        if r:
            labels["equipment_std"].setdefault(val, r.display)
        if etype:
            parent_vals["equipment_std"].setdefault(val, set()).add(etype)

    sym_ids: set[str] = set()
    sym_keys: set[str] = set()
    families: set[str] = set()
    conditions: set[str] = set()
    on_vals: set[str] = set()
    etype_of = {e.get("equipment_id"): _s(e.get("equipment_type")) or None for e in equipment}
    for f in faults:
        wording = _s(f.get("symptom"))
        if not wording:
            continue
        ctx = {"equipment_type": etype_of.get(f.get("equipment_id"))}
        r = lookup.find(wording, kind="symptom", context=ctx)
        parts = lookup.clean(wording, "symptom", ctx).parts or {}
        cond = parts.get("condition")
        if not r:
            sym_ids.add(UNRESOLVED)
            families.add(UNCLASSIFIED)
            parent_vals["symptom"].setdefault(UNRESOLVED, set()).add(UNCLASSIFIED)
            fkey = UNCLASSIFIED
        else:
            sid = str(r.standard_id)
            sym_ids.add(sid)
            sym_keys.add(r.key)
            labels["symptom"].setdefault(sid, r.display)
            fam = lookup.family_of(r.standard_id)
            fkey = fam.key if fam else UNCLASSIFIED
            families.add(fkey)
            if fam:
                labels["symptom_family"].setdefault(fkey, fam.display)
            parent_vals["symptom"].setdefault(sid, set()).add(fkey)
        # ON / WHERE (13 §8): the component as its part standard's id (shared with
        # action_logs[].parts[]), the location as the location-table term
        comp = parts.get("component")
        if comp:
            pid = parts.get("component_standard_id") or lookup.find_key(comp, "part")
            val = str(pid) if pid is not None else UNRESOLVED
            on_vals.add(val)
            if pid is not None:
                labels[ON_FACET].setdefault(val, lookup.display_name(pid))
            parent_vals[ON_FACET].setdefault(val, set()).add(fkey)
        loc = parts.get("location")
        if loc:
            on_vals.add(loc)
            labels[ON_FACET].setdefault(loc, f"in {loc}")
            parent_vals[ON_FACET].setdefault(loc, set()).add(fkey)
        if not r:
            continue
        if cond:
            conditions.add(cond)
            labels.setdefault(CONDITION_FACET, {}).setdefault(cond, cond)
            parent_vals.setdefault(CONDITION_FACET, {}).setdefault(cond, set()).add(fkey)

    # a click on a broader term reaches the narrower groups: widen the KEYS through the links,
    # then map every key back to its standard id (an ancestor with no standard is skipped)
    broad: set[str] = set(sym_ids)
    if links:
        for key in links.widen(sym_keys):
            sid = lookup.find_key(key, "symptom")
            if sid is not None:
                broad.add(str(sid))
                labels["symptom"].setdefault(str(sid), lookup.display_name(sid))
                fam = lookup.family_of(sid)
                parent_vals["symptom"].setdefault(str(sid), set()).add(
                    fam.key if fam else UNCLASSIFIED
                )
    return {
        "equipment_std": sorted(eq_std),
        "symptom_family": sorted(families),
        "symptom": sorted(sym_ids),
        "symptom_broad": sorted(broad),
        ON_FACET: sorted(on_vals),
        CONDITION_FACET: sorted(conditions),
    }


def project(
    record: dict,
    links: SymptomLinks | None = None,
    lookup: LookupStore | None = None,
    _labels: dict[str, dict[str, str]] | None = None,
    _parent_vals: dict[str, dict[str, set[str]]] | None = None,
) -> dict:
    """One index entry. With `lookup`, the Equipment and Symptom facets are canonical ids
    (module docstring); without it they are the v1 wording facets. `links` widens the Symptom
    facet upwards (broader_than). `_labels` / `_parent_vals` are build_index's accumulators."""
    faults = record.get("faults", []) or []
    actions = record.get("action_logs", []) or []
    equipment = record.get("equipment", []) or []
    problems = record.get("problems", []) or []

    start, end = date_range(record)
    quotes: list[str] = []
    for fam in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(fam, []) or []:
            q = _s((row.get("provenance") or {}).get("evidence_quote"))
            if q:
                quotes.append(q)
            oq = _s((row.get("outcome") or {}).get("evidence_quote"))
            if oq:
                quotes.append(oq)

    parts: list[str] = []
    for a in actions:
        for p in a.get("parts") or []:
            parts.append(p if isinstance(p, str) else _s(p.get("name")))

    has_attachments = any(
        (row.get("provenance") or {}).get("attachment_shas")
        for fam in ("equipment", "problems", "faults", "action_logs")
        for row in record.get(fam, []) or []
    )
    years = sorted({d[:4] for d in (start, end) if d})
    if len(years) == 2 and int(years[1]) - int(years[0]) > 1:
        years = [str(y) for y in range(int(years[0]), int(years[1]) + 1)]

    facets: dict[str, list[str]] = {
        "equipment_type": sorted(
            {_s(e.get("equipment_type")) for e in equipment if _s(e.get("equipment_type"))}
        ),
    }
    if lookup is not None:
        labels = (
            _labels
            if _labels is not None
            else {k: {} for k in ("equipment_std", "symptom", "symptom_family", ON_FACET)}
        )
        pv = _parent_vals if _parent_vals is not None else {k: {} for k in PARENTS}
        facets.update(_canonical_facets(equipment, faults, links, lookup, labels, pv, record))
    else:
        # v1 fallback: wording facets (bounded by nothing but the corpus; 12 §3 says why this is
        # only a fallback)
        symptoms = sorted(
            {
                normalise_symptom(f.get("symptom"), _equipment_type_of(record, f))
                for f in faults
                if normalise_symptom(f.get("symptom"), _equipment_type_of(record, f))
            }
        )
        facets["symptom"] = symptoms
        facets["symptom_broad"] = links.widen(symptoms) if links else list(symptoms)
    facets.update(
        {
            "outcome": sorted(
                {_s((a.get("outcome") or {}).get("value")) or "unknown" for a in actions}
            ),
            "kind": sorted({_s(a.get("kind")) for a in actions if _s(a.get("kind"))}),
            "role": sorted(
                {_s(a.get("performed_by_role")) for a in actions if _s(a.get("performed_by_role"))}
            ),
            "vessel": [_s(record.get("vessel_name"))] if _s(record.get("vessel_name")) else [],
            "year": years,
        }
    )

    return {
        "id": record["casefile_id"],
        "vessel": _s(record.get("vessel_name")),
        "imo": record.get("imo"),
        "title": _s(record.get("problem_statement")),
        "start": start,
        "end": end,
        "years": years,
        "n_faults": len(faults),
        "n_actions": len(actions),
        "outcomes": dict(outcome_tally(record)),
        "needs_review": bool(record.get("needs_review")),
        "n_flags": len(record.get("validation_flags") or []),
        "has_attachments": bool(has_attachments),
        "facets": facets,
        # weighted text; each entry keeps its row id so the hit can deep-link (#f3, #a12)
        "fields": {
            "title": [{"id": "", "t": _s(record.get("problem_statement"))}],
            "symptoms": [
                {"id": f["fault_id"], "t": _s(f.get("symptom"))}
                for f in faults
                if _s(f.get("symptom"))
            ],
            "alarms": [
                {"id": f["fault_id"], "t": al}
                for f in faults
                for al in (f.get("alarms") or [])
                if _s(al)
            ],
            "causes": [
                {"id": f["fault_id"], "t": _s(f.get("suspected_cause"))}
                for f in faults
                if _s(f.get("suspected_cause"))
            ],
            "problems": [
                {
                    "id": p["problem_id"],
                    "t": " ".join(
                        _s(p.get(k))
                        for k in ("statement", "root_cause", "description")
                        if _s(p.get(k))
                    ),
                }
                for p in problems
            ],
            "actions": [
                {
                    "id": a["action_id"],
                    "t": _s(a.get("action")),
                    "o": _s((a.get("outcome") or {}).get("value")) or "unknown",
                    "k": _s(a.get("kind")),
                }
                for a in actions
                if _s(a.get("action"))
            ],
            "findings": [
                {"id": a["action_id"], "t": _s(a.get("finding"))}
                for a in actions
                if _s(a.get("finding"))
            ],
            "equipment": [
                {
                    "id": e["equipment_id"],
                    "t": " ".join(
                        _s(e.get(k))
                        for k in ("name", "maker", "model", "serial")
                        if _s(e.get(k)) and _s(e.get(k)) != "NA"
                    ),
                }
                for e in equipment
            ],
            "parts": [{"id": "", "t": p} for p in parts if p],
            "quotes": [{"id": "", "t": q} for q in quotes],
        },
    }


def build_index(
    records: list[dict],
    links: SymptomLinks | None = None,
    lookup: LookupStore | None = None,
) -> dict:
    """The whole index. `canonical` says whether the id facets are present; the UI falls back to
    wording facets and shows a banner when it is false."""
    if lookup is None:
        return {
            "schema": SCHEMA_V1,
            "canonical": False,
            "weights": FIELD_WEIGHTS,
            "facet_match": dict(FACET_MATCH),
            "docs": [project(r, links) for r in records],
        }
    labels: dict[str, dict[str, str]] = {
        k: {} for k in ("equipment_std", "symptom", "symptom_family", ON_FACET)
    }
    parent_vals: dict[str, dict[str, set[str]]] = {k: {} for k in PARENTS}
    docs = [project(r, links, lookup, labels, parent_vals) for r in records]
    # every family the CSV defines gets a label, so the UI can show the full bounded list
    for fam in lookup.families("symptom"):
        labels["symptom_family"].setdefault(fam.key, fam.display)
    labels["symptom_family"].setdefault(UNCLASSIFIED, "unclassified")
    labels["symptom"].setdefault(UNRESOLVED, "unresolved wording")
    labels["equipment_std"].setdefault(UNRESOLVED, "unresolved wording")
    labels[ON_FACET].setdefault(UNRESOLVED, "unresolved component")
    # the condition sub-facet exists only when some value has enough casefiles behind it
    parents = dict(PARENTS)
    per_value: dict[str, int] = {}
    for d in docs:
        for v in d["facets"].get(CONDITION_FACET, []):
            per_value[v] = per_value.get(v, 0) + 1
    if per_value and max(per_value.values()) >= CONDITION_MIN_CASEFILES:
        parents[CONDITION_FACET] = "symptom_family"
    else:
        for d in docs:
            d["facets"].pop(CONDITION_FACET, None)
        labels.pop(CONDITION_FACET, None)
        parent_vals.pop(CONDITION_FACET, None)
    return {
        "schema": SCHEMA_V2,
        "canonical": True,
        "weights": FIELD_WEIGHTS,
        # the facet whose values match on another field: Symptom clicks test `symptom_broad`
        "facet_match": dict(FACET_MATCH),
        "parents": parents,
        "facet_parent_vals": {
            k: {v: sorted(ps) for v, ps in d.items()} for k, d in parent_vals.items()
        },
        "labels": labels,
        "pseudo_values": [UNCLASSIFIED, UNRESOLVED],
        "docs": docs,
    }


def facets_values(index: dict, key: str) -> list[str]:
    """Every distinct value of one facet across the index (the bound a facet must respect)."""
    return sorted({v for d in index["docs"] for v in d["facets"].get(key, [])})
