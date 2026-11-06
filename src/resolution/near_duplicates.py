"""
Phase 1 of the symptom-core hygiene (docs/13 §8, docs/17 §6): find symptom standards that are
probably one failure mode said differently, and SAY WHY — read-only, nothing merges here.

For every symptom family with more than one standard, every pair of cores is scored with the
lexical stand-in `(token_set_ratio + token_sort_ratio) / 200` (rapidfuzz) and kept at or above
the thread bar (0.72). The two token sets are diffed and every differing token is tagged by what
would explain it:

    verb_form     same lemma, different inflection (fails / failed / failing)
    modality      could not / unable to / would not / does not vs fail to
    condition     a condition_terms.csv row, or a tail after during|while|when|after|before|since
    frequency     a frequency_terms.csv row
    head_noun     a bare equipment head noun (engine, pump, compressor)
    where         a component / location / modifier term — should not be in a core at all: a
                  decomposition bug
    failure_mode  a word a symptom family's patterns name (knock, vibrat, leak …): the two cores
                  are DIFFERENT failure modes (the near-synonym rule, docs/15 §7)
    unknown       none of the above

Verdict per pair: MERGE-BY-TABLE <tables> when every differing token is tagged and none is
unknown or failure_mode (the tables named are where the fix goes); KEEP when a failure_mode word
differs; REVIEW otherwise. The same scan runs pending_term rows against the standards, so a
queued spelling one table row away from an existing standard is listed.

Every merge still comes from an explicit table row written by a human; this module only points
at the row to write. After Phase 2 a pair whose verdict is MERGE-BY-TABLE is a bug.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

from src.resolution.lookup import UNCLASSIFIED, LookupStore
from src.resolution.normalize import (
    LEMMA_MAP,
    ResolverConfig,
    _lemma_bases,
    unmapped_inflection,
)

THREAD_BAR = 0.72
MODAL_WORDS = frozenset(
    {
        "could", "couldn", "cannot", "can", "unable", "would", "won", "t", "not", "no", "does",
        "did", "do", "to", "fail", "failure", "able", "is", "was", "be", "being",
    }
)  # fmt: skip
WHEN_PREPOSITIONS = frozenset({"during", "while", "when", "after", "before", "since", "in", "at"})
TAG_TABLE = {
    "verb_form": "LEMMA_MAP / symptom_phrases.csv",
    "modality": "symptom_phrases.csv",
    "condition": "condition_terms.csv",
    "frequency": "frequency_terms.csv",
    "head_noun": "equipment_head_nouns.csv",
    "where": "decomposition (bug: a where-term in a core)",
}
TAGS = (*TAG_TABLE, "failure_mode", "unknown")


def lexical_score(a: str, b: str) -> float:
    """(token_set_ratio + token_sort_ratio) / 200 — the thread's lexical stand-in."""
    from rapidfuzz import fuzz

    return round((fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 200, 3)


@dataclass
class Side:
    standard_id: int | None
    key: str
    display: str
    family: str
    spellings: list[dict]  # raw, hits, source
    pending: bool = False


@dataclass
class Pair:
    family: str
    a: Side
    b: Side
    score: float
    diff: list[dict]  # token, side (a|b), tag
    verdict: str
    tables: list[str] = field(default_factory=list)


def _covered(core_lems: list[str], cfg: ResolverConfig, kinds: Iterable[str]) -> dict[int, str]:
    """index -> kind for every token covered by a table term of the given kinds."""
    out: dict[int, str] = {}
    wanted = set(kinds)
    max_len = max((len(k) for k in cfg.terms), default=0)
    j = 0
    while j < len(core_lems):
        hit = None
        for n in range(min(max_len, len(core_lems) - j), 0, -1):
            t = cfg.terms.get(tuple(core_lems[j : j + n]))
            if t and t[0] in wanted:
                hit = (n, t[0])
                break
        if hit:
            for k in range(j, j + hit[0]):
                out[k] = hit[1]
            j += hit[0]
        else:
            j += 1
    return out


def _verb_base(tok: str) -> str | None:
    if tok in LEMMA_MAP:
        return LEMMA_MAP[tok]
    if tok in _lemma_bases():
        return tok
    return unmapped_inflection(tok)


def _family_words(cfg_patterns: Iterable[str]) -> list[str]:
    return [p.strip().lower() for p in cfg_patterns if p.strip()]


def tag_tokens(
    core_a: str,
    core_b: str,
    cfg: ResolverConfig,
    family_patterns: list[str],
) -> list[dict]:
    """Diff the two cores' token sets and tag every differing token."""
    la, lb = core_a.split(), core_b.split()
    sa, sb = set(la), set(lb)
    out: list[dict] = []
    for side, lems, other in (("a", la, sb), ("b", lb, sa)):
        where = _covered(lems, cfg, ("component", "location", "modifier"))
        cond = _covered(lems, cfg, ("condition",))
        freq = _covered(lems, cfg, ("frequency",))
        after_when: set[int] = set()
        for i, t in enumerate(lems):
            if t in WHEN_PREPOSITIONS:
                after_when.update(range(i, len(lems)))
        other_bases = {_verb_base(t) for t in other} - {None}
        for i, tok in enumerate(lems):
            if tok in other:
                continue
            tag = "unknown"
            if i in where:
                tag = "where"
            elif i in cond:
                tag = "condition"
            elif i in freq:
                tag = "frequency"
            elif tok in set(cfg.head_nouns.values()):
                tag = "head_noun"
            elif any(p in tok for p in family_patterns if len(p) > 3):
                tag = "failure_mode"  # "knock" in "knocking", "vibrat" in "vibrating"
            elif _verb_base(tok) and _verb_base(tok) in other_bases:
                tag = "verb_form"
            elif tok in MODAL_WORDS or (_verb_base(tok) or "") in MODAL_WORDS:
                tag = "modality"
            elif i in after_when:
                tag = "condition"  # the "when" preposition and everything after it
            out.append({"token": tok, "side": side, "tag": tag})
    return out


def verdict_of(diff: list[dict]) -> tuple[str, list[str]]:
    tags = {d["tag"] for d in diff}
    if not diff:
        return "MERGE-BY-TABLE", ["(identical cores)"]
    if "failure_mode" in tags:
        return "KEEP", []
    if "unknown" in tags:
        return "REVIEW", sorted(TAG_TABLE[t] for t in tags if t in TAG_TABLE)
    return "MERGE-BY-TABLE", sorted({TAG_TABLE[t] for t in tags})


def _sides(store: LookupStore) -> tuple[dict[str, list[Side]], list[Side]]:
    by_std: dict[int, list[dict]] = {}
    for a in store.alias_rows("symptom"):
        by_std.setdefault(a["standard_id"], []).append(
            {"raw": a["raw_term"], "hits": a["hit_count"], "source": a["source"]}
        )
    fams: dict[str, list[Side]] = {}
    for s in store.standards(kind="symptom"):
        if s.status == "merged":
            continue
        fams.setdefault(s.family_key or UNCLASSIFIED, []).append(
            Side(s.id, s.key, s.display, s.family_key or UNCLASSIFIED, by_std.get(s.id, []))
        )
    pend = [
        Side(
            None,
            (p.parts or {}).get("core") or p.raw_key,
            p.raw_term,
            "pending",
            [{"raw": p.raw_term, "hits": p.hit_count, "source": p.source}],
            pending=True,
        )
        for p in store.pending("symptom")
        if p.reason == "undecomposed"
    ]
    return fams, pend


def scan(store: LookupStore, family: str | None = None, bar: float = THREAD_BAR) -> dict:
    """The Phase 1 report: pairs within each family, and pending rows against every standard."""
    cfg = store.config
    patterns: list[str] = []
    for f in store.families("symptom"):
        patterns.extend(_family_words(f.patterns))
    fams, pend = _sides(store)
    pairs: list[Pair] = []
    for fam, sides in sorted(fams.items()):
        if family and fam != family:
            continue
        for i in range(len(sides)):
            for j in range(i + 1, len(sides)):
                a, b = sides[i], sides[j]
                sc = lexical_score(a.key, b.key)
                if sc < bar:
                    continue
                diff = tag_tokens(a.key, b.key, cfg, patterns)
                verdict, tables = verdict_of(diff)
                pairs.append(Pair(fam, a, b, sc, diff, verdict, tables))
    pending_pairs: list[Pair] = []
    all_sides = [s for sides in fams.values() for s in sides]
    for p in pend:
        for s in all_sides:
            if family and s.family != family:
                continue
            sc = lexical_score(p.key, s.key)
            if sc < bar:
                continue
            diff = tag_tokens(p.key, s.key, cfg, patterns)
            verdict, tables = verdict_of(diff)
            pending_pairs.append(Pair(s.family, p, s, sc, diff, verdict, tables))
    pairs.sort(key=lambda x: (x.verdict != "MERGE-BY-TABLE", -x.score, x.family))
    pending_pairs.sort(key=lambda x: (x.verdict != "MERGE-BY-TABLE", -x.score))
    return {
        "bar": bar,
        "families_scanned": sorted(
            f for f, s in fams.items() if len(s) > 1 and (not family or f == family)
        ),
        "pairs": [asdict(x) for x in pairs],
        "pending_pairs": [asdict(x) for x in pending_pairs],
        "counts": {
            v: sum(1 for x in pairs if x.verdict == v) for v in ("MERGE-BY-TABLE", "REVIEW", "KEEP")
        },
    }


def _spellings(side: dict) -> str:
    return (
        " | ".join(f"{s['raw']} ({s['hits']}, {s['source'][-8:]})" for s in side["spellings"])
        or "-"
    )


def render_text(report: dict) -> str:
    lines = [
        f"possible duplicates (lexical >= {report['bar']}; verdicts: MERGE-BY-TABLE / REVIEW / KEEP)",
        f"  families scanned: {', '.join(report['families_scanned']) or 'none with >1 standard'}",
        f"  pairs: {report['counts']}",
    ]
    for p in report["pairs"] + report["pending_pairs"]:
        head = "pending vs standard" if p["a"].get("pending") else p["family"]
        tables = f" {', '.join(p['tables'])}" if p["tables"] else ""
        lines.append("")
        lines.append(f"[{p['verdict']}{tables}]  {head}  score {p['score']}")
        lines.append(
            f"    A #{p['a']['standard_id'] or '-'} {p['a']['key']!r}  <- {_spellings(p['a'])}"
        )
        lines.append(
            f"    B #{p['b']['standard_id'] or '-'} {p['b']['key']!r}  <- {_spellings(p['b'])}"
        )
        diff = (
            ", ".join(f"{d['side']}:{d['token']}={d['tag']}" for d in p["diff"])
            or "(no token differs)"
        )
        lines.append(f"    diff: {diff}")
    if not report["pairs"] and not report["pending_pairs"]:
        lines.append("  none")
    return "\n".join(lines) + "\n"


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


_WORD = re.compile(r"[^\W\d_]+")
