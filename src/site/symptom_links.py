"""
Reviewed `broader_than` links between normalised symptom groups (12 §3, §5.1).

A broad symptom ("knocking sound") can be searched to find its narrower groups ("knocking sound
from exhaust valve") without merging them. The link is a hand-maintained, reviewed decision in
`config/symptom_links.json`:

    [{"broader": "knocking sound", "narrower": "knocking sound from exhaust valve",
      "reviewed_by": "...", "date": "2026-10-21", "note": "..."}]

Keys are normalised symptom strings exactly as the lookup page prints them (views.normalise_symptom:
since A1, docs/13 §8, the failure-mode core of the wording). A link whose narrower key no longer
exists after decomposition is RETIRED by `retire_dissolved()` during the lookup build: removed from
the file and written to resolution_log (method_chain "link_retired") with the reviewer's note kept
in the rationale.

This touches neither node merging nor thread merging (kg_dedup_layer): every group keeps its own
rows and counts. The link only widens what a facet click on the broader term returns, and nests
the narrower group under the broader one on the lookup page.

Validation (`SymptomLinks.validate`):
  - both sides must exist in the current records: WARN and drop the link, never fail (the set of
    groups changes per run);
  - no cycles, and a narrower term has at most one broader term: these are config errors -> ValueError.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

REQUIRED = ("broader", "narrower")
OPTIONAL = ("reviewed_by", "date", "note")


def load_symptom_links(path: str | Path) -> list[dict]:
    """Read the JSON file; shape-check each row. Missing file -> []."""
    p = Path(path)
    if not p.exists():
        return []
    rows = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise TypeError(f"{p}: expected a JSON list of links")
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise TypeError(f"{p}[{i}]: expected an object")
        for k in REQUIRED:
            if not isinstance(r.get(k), str) or not r[k].strip():
                raise ValueError(f"{p}[{i}]: '{k}' must be a non-empty string")
        link = {k: r[k].strip() for k in REQUIRED}
        if link["broader"] == link["narrower"]:
            raise ValueError(f"{p}[{i}]: a term cannot be broader than itself: {link['broader']!r}")
        for k in OPTIONAL:
            if r.get(k) is not None:
                link[k] = str(r[k])
        out.append(link)
    return out


def retire_dissolved(path: str | Path, store) -> list[dict]:
    """Drop every link whose `narrower` key is no longer a symptom standard (its wordings now
    resolve to the failure mode alone: docs/13 §8). The file is rewritten without them; each one
    is logged on the store (kind symptom, method_chain "link_retired", rationale "dissolved into
    <core> + component <x>; note: ...") so the reviewer's decision is not lost. Returns the
    retired rows. Links with a missing BROADER side are left for validate() to report."""
    from src.resolution.normalize import decompose_symptom

    p = Path(path)
    rows = load_symptom_links(p)
    if not rows:
        return []
    live = {s.key for s in store.standards(kind="symptom") if s.status != "merged"}
    kept, retired = [], []
    for r in rows:
        if r["narrower"] in live:
            kept.append(r)
            continue
        parts = decompose_symptom(r["narrower"], None, store.config)
        tail = []
        if r.get("note"):
            tail.append(f"note: {r['note']}")
        if r.get("reviewed_by") or r.get("date"):
            tail.append(f"reviewed_by: {r.get('reviewed_by', '')} {r.get('date', '')}".strip())
        rationale = f"dissolved into {parts.core!r} + component {parts.component!r}" + (
            "; " + "; ".join(tail) if tail else ""
        )
        store.log_event(
            "symptom",
            r["narrower"],
            "link_retired",
            before=r["narrower"],
            after=r["broader"],
            standard_id=store.find_key(parts.core, "symptom"),
            rationale=rationale,
        )
        retired.append(dict(r, rationale=rationale))
    if retired:
        p.write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return retired


class SymptomLinks:
    """The validated link set. `broader_of[narrower] -> broader`; `ancestors(term)` walks up."""

    def __init__(self, links: Iterable[dict] = ()):
        self.links: list[dict] = list(links)
        self.broader_of: dict[str, str] = {}
        self.narrower_of: dict[str, list[str]] = {}
        self.note_of: dict[str, dict] = {}
        self.warnings: list[str] = []
        self._build(self.links)

    # ------------------------------------------------------------------ validation

    def validate(self, known: Iterable[str]) -> SymptomLinks:
        """Drop (with a warning) every link whose sides are not both in `known`; then check the
        structure. Returns self for chaining."""
        known_set = set(known)
        kept = []
        for link in self.links:
            missing = [s for s in ("broader", "narrower") if link[s] not in known_set]
            if missing:
                self.warnings.append(
                    "symptom_links: dropped "
                    f"{link['broader']!r} > {link['narrower']!r}: "
                    + ", ".join(f"{m} term not in current records" for m in missing)
                )
                continue
            kept.append(link)
        self.links = kept
        self._build(kept)
        return self

    def _build(self, links: list[dict]) -> None:
        self.broader_of = {}
        self.narrower_of = {}
        self.note_of = {}
        for link in links:
            b, n = link["broader"], link["narrower"]
            if n in self.broader_of and self.broader_of[n] != b:
                raise ValueError(
                    f"symptom_links: {n!r} has two broader terms: "
                    f"{self.broader_of[n]!r} and {b!r} (at most one allowed)"
                )
            self.broader_of[n] = b
            self.narrower_of.setdefault(b, [])
            if n not in self.narrower_of[b]:
                self.narrower_of[b].append(n)
            self.note_of[n] = link
        for start in self.broader_of:
            seen = [start]
            cur = start
            while cur in self.broader_of:
                cur = self.broader_of[cur]
                if cur in seen:
                    seen.append(cur)
                    raise ValueError("symptom_links: cycle: " + " > ".join(seen))
                seen.append(cur)

    # ------------------------------------------------------------------ queries

    def __bool__(self) -> bool:
        return bool(self.broader_of)

    def __len__(self) -> int:
        return len(self.broader_of)

    def ancestors(self, term: str) -> list[str]:
        """Broader terms of `term`, nearest first. [] when it has none."""
        out: list[str] = []
        cur = term
        while cur in self.broader_of:
            cur = self.broader_of[cur]
            out.append(cur)
        return out

    def widen(self, terms: Iterable[str]) -> list[str]:
        """terms ∪ all their ancestors, sorted. This is the `symptom_broad` facet value."""
        out: set[str] = set()
        for t in terms:
            if t:
                out.add(t)
                out.update(self.ancestors(t))
        return sorted(out)

    def narrower(self, term: str) -> list[str]:
        return list(self.narrower_of.get(term, []))

    def depth(self, term: str) -> int:
        return len(self.ancestors(term))
