"""
Diff two lookup DBs (docs/13 §6, docs/15 §4.4): the re-resolve loop is rebuild + diff, never a
migration. `scripts/build_lookup.py --db NEW --fresh` builds the candidate; this module says what
changed between the current file and it, per kind:

  standards before / after      live (non-merged) standard counts
  moved                         aliases whose standard KEY changed: old key -> new key, by raw_key
  now_pending                   spellings pending in NEW that were not pending in OLD for the same
                                reason: what they resolved to before (an alias key) or that OLD had
                                never seen them
  residue                       token frequency of alias.parts.residue + pending.parts.residue in
                                NEW (top 30), with how many distinct casefiles (alias.source) carry
                                each token
  new_standards / vanished      keys in one side only, with their spellings

It reports, it does not judge: every function here is read-only and the CLI exits 0 always.
Standards are compared by KEY (ids are per-file and mean nothing across two builds).
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from src.resolution.lookup import _loads

KINDS = ("equipment", "part", "symptom")
TOP_RESIDUE = 30


@dataclass
class Snapshot:
    """What one DB says, keyed so two builds can be compared without ids."""

    label: str
    # kind -> key -> {"status", "aliases": [raw_term...]}
    standards: dict[str, dict[str, dict]] = field(default_factory=dict)
    # kind -> raw_key -> {"raw_term", "key" (of the standard after following merges), "source", "parts"}
    aliases: dict[str, dict[str, dict]] = field(default_factory=dict)
    # kind -> raw_key -> {"raw_term", "reason", "hit_count", "source", "parts"}
    pending: dict[str, dict[str, dict]] = field(default_factory=dict)

    def live_keys(self, kind: str) -> set[str]:
        return {k for k, s in self.standards.get(kind, {}).items() if s["status"] != "merged"}


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return (
        con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        is not None
    )


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def snapshot(con: sqlite3.Connection, label: str = "") -> Snapshot:
    """Read one DB (any schema version since docs/13 §2: missing tables / columns are empty)."""
    snap = Snapshot(label=label)
    for k in KINDS:
        snap.standards[k], snap.aliases[k], snap.pending[k] = {}, {}, {}
    if not _has_table(con, "standard_term"):
        return snap
    by_id: dict[int, tuple[str, str, int | None, str]] = {}
    for r in con.execute("SELECT id, kind, key, merged_into, status FROM standard_term"):
        by_id[r[0]] = (r[1], r[2], r[3], r[4])
        snap.standards.setdefault(r[1], {})[r[2]] = {"status": r[4], "aliases": []}

    def follow(sid: int) -> int:
        seen: set[int] = set()
        while sid in by_id and by_id[sid][2] is not None and sid not in seen:
            seen.add(sid)
            sid = by_id[sid][2]
        return sid

    alias_cols = _columns(con, "alias") if _has_table(con, "alias") else set()
    if alias_cols:
        parts_sql = "parts" if "parts" in alias_cols else "NULL"
        for r in con.execute(
            f"SELECT kind, raw_key, raw_term, standard_id, source, {parts_sql} FROM alias"
        ):
            kind, rk, raw, sid, source, parts = r
            final = follow(sid)
            key = by_id[final][1] if final in by_id else ""
            snap.aliases.setdefault(kind, {})[rk] = {
                "raw_term": raw,
                "key": key,
                "source": source,
                "parts": _loads(parts),
            }
            if kind in snap.standards and key in snap.standards[kind]:
                snap.standards[kind][key]["aliases"].append(raw)
    if _has_table(con, "pending_term"):
        for r in con.execute(
            "SELECT kind, raw_key, raw_term, reason, hit_count, source, parts FROM pending_term"
        ):
            snap.pending.setdefault(r[0], {})[r[1]] = {
                "raw_term": r[2],
                "reason": r[3],
                "hit_count": r[4],
                "source": r[5],
                "parts": _loads(r[6]),
            }
    return snap


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a DB file without touching it (no schema creation, no migration)."""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    try:
        return sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(str(p))


def _residue_tokens(parts: dict | None) -> list[str]:
    res = (parts or {}).get("residue")
    if not res:
        return []
    if isinstance(res, str):
        return [t for t in res.casefold().split() if t]
    if isinstance(res, (list, tuple)):
        out: list[str] = []
        for x in res:
            out.extend(str(x).casefold().split())
        return out
    return []


def diff(old: Snapshot, new: Snapshot, kind: str | None = None) -> dict:
    """Per-kind comparison; see module docstring. `kind` limits the report to one kind."""
    kinds = [k for k in KINDS if kind in (None, k)]
    report: dict = {"old": old.label, "new": new.label, "kinds": {}, "changed": False}
    for k in kinds:
        o_std, n_std = old.live_keys(k), new.live_keys(k)
        o_al, n_al = old.aliases.get(k, {}), new.aliases.get(k, {})
        n_pend = new.pending.get(k, {})

        moved: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rk, a in o_al.items():
            b = n_al.get(rk)
            if b and a["key"] != b["key"]:
                moved[(a["key"], b["key"])].append(rk)

        o_pend = old.pending.get(k, {})
        now_pending = []
        for rk, p in sorted(n_pend.items()):
            if rk in o_pend and o_pend[rk]["reason"] == p["reason"]:
                continue  # pending on both sides for the same reason: not a change
            was = o_al.get(rk)
            now_pending.append(
                {
                    "raw_key": rk,
                    "raw_term": p["raw_term"],
                    "reason": p["reason"],
                    "hits": p["hit_count"],
                    "was": was["key"] if was else None,  # the standard key it had before, or None
                    "parts": p["parts"],
                }
            )

        tok_hits: Counter = Counter()
        tok_cases: dict[str, set[str]] = defaultdict(set)
        for rows in (n_al, n_pend):
            for rk, a in rows.items():
                for t in _residue_tokens(a.get("parts")):
                    tok_hits[t] += 1
                    tok_cases[t].add(a.get("source") or "")
        residue = [
            {"token": t, "n": n, "casefiles": len(tok_cases[t])}
            for t, n in sorted(tok_hits.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_RESIDUE]
        ]

        new_keys = sorted(n_std - o_std)
        gone_keys = sorted(o_std - n_std)
        entry = {
            "standards_before": len(o_std),
            "standards_after": len(n_std),
            "aliases_before": len(o_al),
            "aliases_after": len(n_al),
            "pending_after": len(n_pend),
            "moved": [
                {"from": a, "to": b, "raw_keys": sorted(rks)}
                for (a, b), rks in sorted(moved.items())
            ],
            "now_pending": now_pending,
            "residue": residue,
            "new_standards": [
                {"key": key, "aliases": new.standards[k][key]["aliases"]} for key in new_keys
            ],
            "vanished_standards": [
                {"key": key, "aliases": old.standards[k][key]["aliases"]} for key in gone_keys
            ],
        }
        entry["changed"] = bool(
            entry["moved"]
            or entry["now_pending"]
            or entry["new_standards"]
            or entry["vanished_standards"]
            or len(o_al) != len(n_al)
        )
        report["changed"] = report["changed"] or entry["changed"]
        report["kinds"][k] = entry
    return report


def render_md(report: dict) -> str:
    """The report as Markdown (also what the CLI prints)."""
    out = [
        f"# lookup diff — `{report['old'] or 'OLD'}` → `{report['new'] or 'NEW'}`",
        "",
        "Standards compared by key; ids are per file. Reports, does not judge.",
        "",
        "| kind | standards before | after | aliases before | after | pending after | changed |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for k, e in report["kinds"].items():
        out.append(
            f"| {k} | {e['standards_before']} | {e['standards_after']} | {e['aliases_before']} | "
            f"{e['aliases_after']} | {e['pending_after']} | {'yes' if e['changed'] else 'no'} |"
        )
    if not report["changed"]:
        out += ["", "**No differences**: every spelling resolves to the same standard key in both."]
    for k, e in report["kinds"].items():
        sections = []
        if e["moved"]:
            sections.append(f"### {k}: aliases whose standard moved ({len(e['moved'])} groups)")
            for m in e["moved"]:
                sections.append(f"- `{m['from']}` → `{m['to']}`: {', '.join(m['raw_keys'])}")
        if e["now_pending"]:
            sections.append(f"### {k}: now pending, not minted ({len(e['now_pending'])})")
            sections.append("| raw spelling | reason | hits | was standard |")
            sections.append("|---|---|---:|---|")
            for p in e["now_pending"]:
                was = f"`{p['was']}`" if p["was"] else "(never seen)"
                sections.append(f"| {p['raw_term']} | {p['reason']} | {p['hits']} | {was} |")
        if e["new_standards"]:
            sections.append(f"### {k}: new standards ({len(e['new_standards'])})")
            for s in e["new_standards"]:
                sections.append(f"- `{s['key']}` ← {' | '.join(s['aliases']) or '(no alias)'}")
        if e["vanished_standards"]:
            sections.append(f"### {k}: vanished standards ({len(e['vanished_standards'])})")
            for s in e["vanished_standards"]:
                sections.append(f"- `{s['key']}` (had: {' | '.join(s['aliases']) or 'no alias'})")
        if e["residue"]:
            sections.append(
                f"### {k}: residue tokens (top {TOP_RESIDUE}, from alias / pending parts)"
            )
            sections.append("| token | rows | casefiles |")
            sections.append("|---|---:|---:|")
            for r in e["residue"]:
                sections.append(f"| {r['token']} | {r['n']} | {r['casefiles']} |")
        if sections:
            out += ["", f"## {k}", ""] + sections
    return "\n".join(out) + "\n"


def diff_paths(old_path: str | Path, new_path: str | Path, kind: str | None = None) -> dict:
    """Open both files read-only, snapshot, diff."""
    with open_readonly(old_path) as a, open_readonly(new_path) as b:
        return diff(snapshot(a, str(old_path)), snapshot(b, str(new_path)), kind)
