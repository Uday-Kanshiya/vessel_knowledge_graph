"""
Alias table for component / equipment / symptom names (02 §3.9 stage 1): five tables in SQLite.

  standard_term   one row per standard: id, kind, key, status, merged_into, source, first_seen,
                  family_id (nullable), family_method
  alias           one row per raw spelling: raw_key -> standard_id, with method, counts, dates and
                  `parts` (JSON, nullable): what the cleaner took apart for that spelling
  family          the bounded, human-maintained layer above standards: id, kind, key, display,
                  description, patterns, sort_order. NEVER minted by rules: rows come only from
                  `import_families_csv` (config/symptom_families.csv). This is what a facet lists.
  pending_term    a raw spelling the mint guard REFUSED to mint (docs/15 §4.1): kind, raw_key,
                  raw_term, reason, parts, source, dates, hit_count. Not a standard, so `find()`
                  never sees it (the name stays unresolved on the site); the third review queue.
  resolution_log  one row per decision that was not a plain alias hit: ts, kind, raw_key,
                  method_chain, before, after, standard_id, rationale. Why a spelling landed where
                  it did; written by resolve / merge / approve / set_alias / queue / promote / dismiss.

The standard NAME is never stored. `key` is a deterministic function of the text
(normalize.normalize_for_kind), so it is a stable join key; what the UI shows is `display_name()`,
a view that picks the longest fully-expanded alias of the group at render time. Nothing ever needs
renaming. Records and the site carry `standard_id` and render the name from here.

Update rule, one raw name at a time (`resolve`):

  1. raw_key already in alias                    -> method "alias"       (count bumped)
  2. not in alias, but key is a known standard   -> method "normalised"  (case a: new spelling)
  3. not in alias, key unknown                   -> method "new_term"    (case b: minted, status auto)
  3'. ... unless the `guard` returns a reason    -> method "queued"      (pending_term, no standard)

`guard(kind, cleaned, raw) -> reason | None` is injected (resolve() parameter or the store's
default); P1 / P2 supply the real ones (undecomposed symptom, label still naming a vessel, a
position, non-Latin text, a maker / serial). With no guard every unknown key is minted as before.

`find()` is the read-only form (no insert, no count): what the site build uses (`mint=False`
semantics, a name with no alias yet is simply unresolved).

Family assignment (`assign_families`), per standard, deterministic, in precedence order:
  human / seed CSV row  >  inherited from the nearest ancestor with a family (symptom_links)
  >  keyword rule (`patterns` column, matched on the key)  >  none. The method is recorded on the
standard (`family_method`) so the lookup page can say why; re-running re-derives inherited / rule
but never overrides human / seed.

Case b lands in `review_queue()`; standards without a family in `unclassified()` (the second
queue); refused mints in `pending()` (the third). A human either `approve()`s, `merge()`s, pins a
spelling with `set_alias()`, classifies with `set_family()`, or `promote()`s / `dismiss()`es a
pending row. Everything here is deterministic. A name the rules cannot place is NOT forced to its
nearest neighbour: the embedding / adjudicator stages of 02 §3.9 run after this table and write
back through `set_alias` / `set_family` / `promote`.

The human-editable form is CSV (`import_csv` / `export_csv`, columns kind, raw_term,
standard_key, status, family_key; `import_families_csv` for the family list). Every CSV is
header-checked at import (a wrong header fails the build). The shared config tables
(config/modifier_terms.csv, config/vessel_names.txt) are read once per store from `config_dir`.
The SQLite file is a rebuildable runtime cache of those CSVs plus what extraction has seen:
re-resolving is `scripts/build_lookup.py --fresh` into a new file plus `scripts/diff_lookup.py`
against the old one, never a migration.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from src.resolution.normalize import (
    Cleaned,
    ResolverConfig,
    check_header,
    has_abbreviations,
    load_config,
    normalize_for_kind,
    strip_comments,
)

KINDS = ("equipment", "part", "symptom")
STATUSES = ("auto", "reviewed", "merged")
METHODS = ("alias", "normalised", "new_term", "human", "queued")  # Resolution.method vocabulary
ALIAS_METHODS = ("seed", "normalised", "new_term", "human")  # alias.method stays one word
PENDING_REASONS = (
    "undecomposed",
    "needs_translation",
    "contains_vessel",
    "contains_position",
    "contains_identifier",
    "candidate_term",
)
FAMILY_METHODS = ("human", "seed", "inherited", "rule", "none")
UNCLASSIFIED = "unclassified"  # pseudo-family key the UI shows for standards with no family
UNRESOLVED = "__unresolved__"  # pseudo-standard the UI shows for a name with no alias yet

Guard = Callable[[str, Cleaned, str], str | None]  # (kind, cleaned, raw) -> reason or None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS family (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                  -- equipment | part | symptom
    key         TEXT NOT NULL,                  -- stable, from the CSV: e.g. 'abnormal_noise'
    display     TEXT NOT NULL,
    description TEXT,
    patterns    TEXT,                           -- '|'-separated keyword rules, matched on the key
    status      TEXT NOT NULL DEFAULT 'reviewed',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, key)
);
CREATE TABLE IF NOT EXISTS standard_term (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,                -- equipment | part | symptom
    key           TEXT NOT NULL,                -- normalize_for_kind(raw).key: deterministic, stable
    status        TEXT NOT NULL DEFAULT 'auto', -- auto | reviewed | merged
    merged_into   INTEGER REFERENCES standard_term(id),
    source        TEXT NOT NULL,                -- seed | rule | human
    first_seen    TEXT NOT NULL,
    note          TEXT,
    family_id     INTEGER REFERENCES family(id),
    family_method TEXT NOT NULL DEFAULT 'none', -- human | seed | inherited | rule | none
    UNIQUE (kind, key)
);
CREATE TABLE IF NOT EXISTS alias (
    kind        TEXT NOT NULL,
    raw_key     TEXT NOT NULL,                  -- casefolded, punctuation-stripped raw (position kept)
    raw_term    TEXT NOT NULL,                  -- first spelling seen, for display
    stem        TEXT NOT NULL,                  -- raw minus position tokens, original case
    standard_id INTEGER NOT NULL REFERENCES standard_term(id),
    method      TEXT NOT NULL,                  -- seed | normalised | new_term | human (one word)
    source      TEXT NOT NULL,                  -- where the first sighting came from
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    parts       TEXT,                           -- JSON: Cleaned.parts for this spelling (nullable)
    PRIMARY KEY (kind, raw_key)
);
CREATE TABLE IF NOT EXISTS pending_term (
    kind        TEXT NOT NULL,
    raw_key     TEXT NOT NULL,
    raw_term    TEXT NOT NULL,
    reason      TEXT NOT NULL,                  -- undecomposed | needs_translation | contains_vessel
                                                -- | contains_position | contains_identifier | candidate_term
    parts       TEXT,                           -- JSON, nullable
    source      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, raw_key)
);
CREATE TABLE IF NOT EXISTS resolution_log (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    raw_key      TEXT NOT NULL,
    method_chain TEXT NOT NULL,                 -- step names that changed the text, '+'-joined
    "before"     TEXT,
    "after"      TEXT,
    standard_id  INTEGER,                       -- nullable: queued / dismissed rows have none
    rationale    TEXT
);
CREATE INDEX IF NOT EXISTS ix_alias_standard ON alias(standard_id);
CREATE INDEX IF NOT EXISTS ix_standard_status ON standard_term(status);
CREATE INDEX IF NOT EXISTS ix_log_kind_ts ON resolution_log(kind, ts);
"""
# created after _migrate(), so a DB from before the family layer can be opened and upgraded
_FAMILY_INDEX = "CREATE INDEX IF NOT EXISTS ix_standard_family ON standard_term(family_id)"


@dataclass(frozen=True)
class Resolution:
    standard_id: int | None  # None only when method == "queued"
    key: str
    position: str | None
    method: str  # alias | normalised | new_term | human | queued
    display: str
    reason: str | None = None  # the guard's reason when method == "queued"

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"Resolution.method must be one of {METHODS}, got {self.method!r}")
        if (self.standard_id is None) != (self.method == "queued"):
            raise ValueError("standard_id is None exactly when method == 'queued'")


@dataclass(frozen=True)
class FamilyRow:
    id: int
    kind: str
    key: str
    display: str
    description: str | None
    patterns: list[str]
    sort_order: int


@dataclass(frozen=True)
class StandardRow:
    id: int
    kind: str
    key: str
    status: str
    display: str
    aliases: list[str]
    hits: int
    first_seen: str
    family_key: str | None = None
    family_method: str = "none"


@dataclass(frozen=True)
class PendingRow:
    kind: str
    raw_key: str
    raw_term: str
    reason: str
    parts: dict | None
    source: str
    first_seen: str
    last_seen: str
    hit_count: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def raw_key_of(raw: str) -> str:
    """Casefolded, punctuation-stripped form of the raw spelling (position tokens kept)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]+", " ", raw.casefold())).strip()


def _dumps(parts: dict | None) -> str | None:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True) if parts else None


def _loads(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def chain_of(c: Cleaned, final: str = "normalised") -> str:
    """The method chain for the log: the step names the cleaner recorded in `parts["steps"]`
    (P1 / P2: vessel_strip, glossary, ...) followed by the resolver's own step. "normalised"
    alone today, because the cleaner records no steps yet."""
    steps = list((c.parts or {}).get("steps") or [])
    return "+".join([*steps, final]) if steps else final


class LookupStore:
    """SQLite-backed alias table. Pass ":memory:" for tests. `config_dir` holds the shared
    tables (modifier_terms.csv, vessel_names.txt); default the repo's config/. `guard` is the
    default mint guard for resolve() (None = mint everything, today's behaviour)."""

    CSV_FIELDS = ("kind", "raw_term", "standard_key", "status", "family_key")
    CSV_REQUIRED = ("kind", "raw_term", "standard_key", "status")  # family_key is optional
    FAMILY_CSV_FIELDS = ("key", "display", "description", "patterns", "sort_order")

    def __init__(
        self,
        path: str | Path = ":memory:",
        config_dir: str | Path | None = None,
        guard: Guard | None = None,
        adjudicator: Callable[[str], str | None] | None = None,
    ):
        self.path = str(path)
        self.config: ResolverConfig = load_config(config_dir)
        self.guard = guard
        # the model stage that translates non-Latin equipment words the glossary missed
        # (docs/13 §3 step b); None = no model, the spelling is queued needs_translation
        self.adjudicator = adjudicator
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """A DB written before the family layer gets its two columns; one written before P0
        gets alias.parts (the two new tables come from CREATE IF NOT EXISTS in _SCHEMA). The
        file is a cache, `--fresh` rebuilds it, but an old file must still open."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(standard_term)")}
        if "family_id" not in cols:
            self.db.execute(
                "ALTER TABLE standard_term ADD COLUMN family_id INTEGER REFERENCES family(id)"
            )
        if "family_method" not in cols:
            self.db.execute(
                "ALTER TABLE standard_term ADD COLUMN family_method TEXT NOT NULL DEFAULT 'none'"
            )
        alias_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(alias)")}
        if "parts" not in alias_cols:
            self.db.execute("ALTER TABLE alias ADD COLUMN parts TEXT")
        self.db.execute(_FAMILY_INDEX)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def clean(
        self, raw: str | None, kind: str, context: Mapping[str, str | None] | None = None
    ) -> Cleaned:
        """The key function for `kind` under this store's config. `context` is the record
        context a symptom needs (`equipment_type`: only the record's own equipment words are
        stripped from the wording; `vessel_name`, `maker`, `model`, `serial` for an equipment
        label: the record entry's own values are stripped from the name)."""
        return normalize_for_kind(raw, kind, self.config, context, adjudicator=self.adjudicator)

    # ------------------------------------------------------------------ resolve

    def resolve(
        self,
        raw: str | None,
        kind: str = "equipment",
        source: str = "extraction",
        mint: bool = True,
        guard: Guard | None = None,
        context: Mapping[str, str | None] | None = None,
    ) -> Resolution | None:
        """Map one raw name to a standard. None when nothing is left after cleaning (raw == "No.2")
        or when `mint` is False and the key is unknown. When the key is unknown and the guard
        (parameter, else the store's default) returns a reason, the spelling is queued in
        pending_term instead of minted and the Resolution has method "queued", standard_id None.
        A symptom's component (alias.parts.component, docs/13 §8) is resolved as kind="part" on
        the way, so it shares its standard id with action_logs[].parts[]."""
        _check_kind(kind)
        c = self.clean(raw, kind, context)
        if c.is_empty:
            return None
        now = _now()
        rk = raw_key_of(raw)
        row = self.db.execute(
            "SELECT standard_id, method FROM alias WHERE kind=? AND raw_key=?", (kind, rk)
        ).fetchone()
        if row:
            self.db.execute(
                "UPDATE alias SET hit_count=hit_count+1, last_seen=? WHERE kind=? AND raw_key=?",
                (now, kind, rk),
            )
            self.db.commit()
            sid = self._follow(row["standard_id"])
            method = "human" if row["method"] == "human" else "alias"
            return self._resolution(sid, c, method)

        std = self.db.execute(
            "SELECT id FROM standard_term WHERE kind=? AND key=?", (kind, c.key)
        ).fetchone()
        if std:
            sid, method = self._follow(std["id"]), "normalised"  # case (a)
            rationale = f"normalised: new spelling of standard #{sid}"
        else:
            if not mint:
                return None
            guard = guard or self.guard
            reason = guard(kind, c, raw) if guard else None
            if reason:
                self.queue(kind, raw, reason, parts=c.parts, source=source)  # case (b')
                return Resolution(
                    standard_id=None,
                    key=c.key,
                    position=c.position,
                    method="queued",
                    display=c.stem or raw.strip(),
                    reason=reason,
                )
            sid, method = self._mint(kind, c.key, "rule", now), "new_term"  # case (b)
            rationale = f"new_term: minted standard #{sid} (status auto)"
        if kind == "symptom" and c.parts and c.parts.get("component"):
            comp_id = self._resolve_component(c.parts, source, now)
            c = Cleaned(
                c.key, c.position, c.stem, c.expanded, {**c.parts, "component_standard_id": comp_id}
            )
        if c.parts and c.parts.get("sister_vessel"):
            rationale += f"; label names sister vessel {c.parts['sister_vessel'].upper()}"
        self._insert_alias(kind, rk, raw, c, sid, method, source, now)
        self._unqueue(kind, rk)  # a spelling that now resolves is no longer pending
        self._log(kind, rk, chain_of(c), raw.strip(), c.key, sid, rationale, now)
        self.db.commit()
        return self._resolution(sid, c, method)

    def _resolve_component(self, parts: dict, source: str, now: str) -> int:
        """The component a symptom label named, as a `part` alias: its spelling resolves like any
        action_logs part (by alias, else the canonical key from component_terms.csv, else minted),
        logged with the chain symptom_component+normalised / +new_term. Returns the part id."""
        raw, key = parts["component_raw"], parts["component"]
        rk = raw_key_of(raw)
        row = self.db.execute(
            "SELECT standard_id FROM alias WHERE kind='part' AND raw_key=?", (rk,)
        ).fetchone()
        if row:
            self.db.execute(
                "UPDATE alias SET hit_count=hit_count+1, last_seen=? WHERE kind='part' AND raw_key=?",
                (now, rk),
            )
            return self._follow(row["standard_id"])
        std = self.db.execute(
            "SELECT id FROM standard_term WHERE kind='part' AND key=?", (key,)
        ).fetchone()
        if std:
            sid, method = self._follow(std["id"]), "normalised"
        else:
            sid, method = self._mint("part", key, "rule", now), "new_term"
        pc = Cleaned(
            key=key,
            position=None,
            stem=raw,
            expanded=has_abbreviations(raw),
            parts={"steps": ["symptom_component"]},
        )
        self._insert_alias("part", rk, raw, pc, sid, method, source, now)
        self._unqueue("part", rk)
        self._log(
            "part",
            rk,
            chain_of(pc, method),
            raw,
            key,
            sid,
            f"{method}: component of a symptom label (standard #{sid})",
            now,
        )
        return sid

    def find(
        self,
        raw: str | None,
        kind: str = "equipment",
        context: Mapping[str, str | None] | None = None,
    ) -> Resolution | None:
        """Read-only resolve: by alias, else by key; never inserts, never counts. What the site
        build uses: a name with no alias yet is unresolved, not minted. Pending rows are not
        standards, so a queued spelling is unresolved here too."""
        _check_kind(kind)
        c = self.clean(raw, kind, context)
        if c.is_empty:
            return None
        row = self.db.execute(
            "SELECT standard_id, method FROM alias WHERE kind=? AND raw_key=?",
            (kind, raw_key_of(raw)),
        ).fetchone()
        if row:
            method = "human" if row["method"] == "human" else "alias"
            return self._resolution(self._follow(row["standard_id"]), c, method)
        std = self.db.execute(
            "SELECT id FROM standard_term WHERE kind=? AND key=?", (kind, c.key)
        ).fetchone()
        if std:
            return self._resolution(self._follow(std["id"]), c, "normalised")
        return None

    def find_key(self, key: str, kind: str) -> int | None:
        """Standard id for an already-normalised key (e.g. a symptom_links term), or None."""
        row = self.db.execute(
            "SELECT id FROM standard_term WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return self._follow(row["id"]) if row else None

    def resolve_many(
        self, raws: Iterable[str | None], kind: str = "equipment", source: str = "extraction"
    ) -> list[Resolution | None]:
        return [self.resolve(r, kind, source) for r in raws]

    # ------------------------------------------------------------------ pending (queue 3)

    def queue(
        self,
        kind: str,
        raw: str,
        reason: str,
        parts: dict | None = None,
        source: str = "extraction",
    ) -> PendingRow:
        """Park a spelling the guard refused to mint. Upsert on (kind, raw_key): a repeat bumps
        hit_count and last_seen, keeps the first spelling, takes the latest reason / parts. The
        first sighting is logged (method_chain "queued")."""
        _check_kind(kind)
        if reason not in PENDING_REASONS:
            raise ValueError(f"reason must be one of {PENDING_REASONS}, got {reason!r}")
        now = _now()
        rk = raw_key_of(raw)
        existed = self.db.execute(
            "SELECT 1 FROM pending_term WHERE kind=? AND raw_key=?", (kind, rk)
        ).fetchone()
        self.db.execute(
            "INSERT INTO pending_term (kind, raw_key, raw_term, reason, parts, source, first_seen, "
            "last_seen, hit_count) VALUES (?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(kind, raw_key) DO UPDATE SET hit_count=hit_count+1, last_seen=excluded.last_seen, "
            "reason=excluded.reason, parts=COALESCE(excluded.parts, pending_term.parts)",
            (kind, rk, raw.strip(), reason, _dumps(parts), source, now, now),
        )
        if not existed:
            c = self.clean(raw, kind)
            self._log(kind, rk, chain_of(c, "queued"), raw.strip(), c.key, None, reason, now)
        self.db.commit()
        return self._pending_row(
            self.db.execute(
                "SELECT * FROM pending_term WHERE kind=? AND raw_key=?", (kind, rk)
            ).fetchone()
        )

    def pending(self, kind: str | None = None) -> list[PendingRow]:
        """Queued spellings, most-hit first: the third review queue (never minted, never found)."""
        q = "SELECT * FROM pending_term"
        args: list = []
        if kind:
            q += " WHERE kind=?"
            args.append(kind)
        q += " ORDER BY hit_count DESC, kind, raw_key"
        return [self._pending_row(r) for r in self.db.execute(q, args).fetchall()]

    def dismiss(self, kind: str, raw_key: str, note: str | None = None) -> bool:
        """Drop a pending row without creating anything (noise, or handled upstream). Logged.
        Returns False when there was no such row."""
        row = self.db.execute(
            "SELECT raw_term, reason FROM pending_term WHERE kind=? AND raw_key=?", (kind, raw_key)
        ).fetchone()
        if not row:
            return False
        self.db.execute("DELETE FROM pending_term WHERE kind=? AND raw_key=?", (kind, raw_key))
        self._log(
            kind,
            raw_key,
            "dismissed",
            row["raw_term"],
            None,
            None,
            note or f"dismissed (was {row['reason']})",
            _now(),
        )
        self.db.commit()
        return True

    def promote(self, kind: str, raw_key: str, standard_key: str, source: str = "human") -> int:
        """Turn a pending row into an alias of `standard_key` (set_alias, which creates the
        standard if needed) and drop it from the queue. Raises KeyError when nothing is pending
        under (kind, raw_key). Returns the standard id."""
        row = self.db.execute(
            "SELECT raw_term, reason FROM pending_term WHERE kind=? AND raw_key=?", (kind, raw_key)
        ).fetchone()
        if not row:
            raise KeyError(f"nothing pending for {kind}:{raw_key!r}")
        sid = self.set_alias(row["raw_term"], standard_key, kind=kind, source=source)
        self._unqueue(kind, raw_key)
        self._log(
            kind,
            raw_key,
            "promoted",
            row["raw_term"],
            self._key_of(sid),
            sid,
            f"promoted from pending (was {row['reason']})",
            _now(),
        )
        self.db.commit()
        return sid

    # ------------------------------------------------------------------ curation

    def set_alias(
        self, raw: str, standard_key: str, kind: str = "equipment", source: str = "human"
    ) -> int:
        """Pin a raw spelling to a standard (given by key or by any spelling of it), creating the
        standard if needed. Overrides whatever the rules decided. Returns the standard id."""
        _check_kind(kind)
        now = _now()
        c = self.clean(raw, kind)
        key = self.clean(standard_key, kind).key or standard_key.strip().casefold()
        sid = self._standard_id(kind, key, source=source, now=now)
        rk = raw_key_of(raw)
        self.db.execute("DELETE FROM alias WHERE kind=? AND raw_key=?", (kind, rk))
        method = "human" if source == "human" else "seed"
        self._insert_alias(kind, rk, raw, c, sid, method, source, now)
        self._unqueue(kind, rk)  # a pinned spelling is no longer pending
        self._log(kind, rk, method, raw.strip(), key, sid, f"{method}: pinned by {source}", now)
        self.db.commit()
        return sid

    def approve(self, standard_id: int, note: str | None = None) -> None:
        row = self.db.execute(
            "SELECT kind, key, status FROM standard_term WHERE id=?", (standard_id,)
        ).fetchone()
        self.db.execute(
            "UPDATE standard_term SET status='reviewed', note=COALESCE(?, note) WHERE id=?",
            (note, standard_id),
        )
        if row:
            self._log(
                row["kind"],
                row["key"],
                "approve",
                row["status"],
                "reviewed",
                standard_id,
                note or "approved",
                _now(),
            )
        self.db.commit()

    def merge(self, from_id: int, into_id: int, note: str | None = None) -> None:
        """Declare two standards the same thing. Aliases of `from_id` now resolve to `into_id`;
        the row stays (status merged) so the decision is visible and reversible."""
        into_id = self._follow(into_id)
        if from_id == into_id:
            return
        src_row = self.db.execute(
            "SELECT kind, key FROM standard_term WHERE id=?", (from_id,)
        ).fetchone()
        self.db.execute(
            "UPDATE standard_term SET status='merged', merged_into=?, note=COALESCE(?, note) "
            "WHERE id=?",
            (into_id, note, from_id),
        )
        self.db.execute("UPDATE alias SET standard_id=? WHERE standard_id=?", (into_id, from_id))
        if src_row:
            self._log(
                src_row["kind"],
                src_row["key"],
                "merge",
                src_row["key"],
                self._key_of(into_id),
                into_id,
                note or f"merged #{from_id} into #{into_id}",
                _now(),
            )
        self.db.commit()

    # ------------------------------------------------------------------ log

    def log_event(
        self,
        kind: str,
        raw_key: str,
        method_chain: str,
        before: str | None = None,
        after: str | None = None,
        standard_id: int | None = None,
        rationale: str | None = None,
    ) -> None:
        """Write one resolution_log row for a decision taken outside the store (e.g. a retired
        symptom link, docs/13 §8)."""
        _check_kind(kind)
        self._log(kind, raw_key, method_chain, before, after, standard_id, rationale, _now())
        self.db.commit()

    def log(self, kind: str | None = None, since: str | None = None) -> list[dict]:
        """resolution_log rows, oldest first. `since` is an ISO timestamp (inclusive)."""
        q = "SELECT * FROM resolution_log WHERE 1=1"
        args: list = []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if since:
            q += " AND ts>=?"
            args.append(since)
        q += " ORDER BY id"
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    # ------------------------------------------------------------------ families

    def families(self, kind: str = "symptom") -> list[FamilyRow]:
        rows = self.db.execute(
            "SELECT * FROM family WHERE kind=? ORDER BY sort_order, key", (kind,)
        ).fetchall()
        return [self._family_row(r) for r in rows]

    def family_of(self, standard_id: int) -> FamilyRow | None:
        sid = self._follow(standard_id)
        r = self.db.execute(
            "SELECT f.* FROM standard_term s JOIN family f ON f.id = s.family_id WHERE s.id=?",
            (sid,),
        ).fetchone()
        return self._family_row(r) if r else None

    def set_family(
        self,
        standard_key_or_alias: str,
        family_key: str,
        kind: str = "symptom",
        source: str = "human",
    ) -> int:
        """Classify a standard (named by its key or any spelling of it) into an existing family.
        `source` human|seed sets family_method accordingly and is never overridden by the rules.
        Returns the standard id. Raises KeyError when the family does not exist (families come
        from the CSV only)."""
        _check_kind(kind)
        fam = self.db.execute(
            "SELECT id FROM family WHERE kind=? AND key=?", (kind, family_key.strip())
        ).fetchone()
        if not fam:
            raise KeyError(f"no family {kind}:{family_key!r} (families come from the CSV only)")
        found = self.find(standard_key_or_alias, kind)
        if found:
            sid = found.standard_id
        else:
            key = self.clean(standard_key_or_alias, kind).key or standard_key_or_alias.casefold()
            sid = self._standard_id(kind, key, source=source, now=_now())
        method = "human" if source == "human" else "seed"
        self.db.execute(
            "UPDATE standard_term SET family_id=?, family_method=? WHERE id=?",
            (fam["id"], method, sid),
        )
        self.db.commit()
        return sid

    def assign_families(
        self, kind: str = "symptom", ancestors_of: Callable[[str], list[str]] | None = None
    ) -> dict[str, int]:
        """Re-derive the family of every standard whose family is not human / seed:
        inherited (nearest ancestor key, via `ancestors_of(key)`, that has a family) > keyword
        rule (first family in sort_order whose pattern matches the key) > none. Deterministic;
        idempotent; never touches human / seed rows. Returns counts per method."""
        _check_kind(kind)
        fams = self.families(kind)
        compiled = [
            (f, [re.compile(re.escape(p.strip()), re.IGNORECASE) for p in f.patterns if p.strip()])
            for f in fams
        ]
        counts = {m: 0 for m in FAMILY_METHODS}
        rows = self.db.execute(
            "SELECT id, key, family_id, family_method FROM standard_term "
            "WHERE kind=? AND status != 'merged' ORDER BY id",
            (kind,),
        ).fetchall()
        for r in rows:
            if r["family_method"] in ("human", "seed") and r["family_id"] is not None:
                counts[r["family_method"]] += 1
                continue
            fid, method = None, "none"
            if ancestors_of:
                for anc in ancestors_of(r["key"]):
                    a = self.db.execute(
                        "SELECT family_id FROM standard_term WHERE kind=? AND key=? AND family_id IS NOT NULL",
                        (kind, anc),
                    ).fetchone()
                    if a:
                        fid, method = a["family_id"], "inherited"
                        break
            if fid is None:
                for f, pats in compiled:
                    if any(p.search(r["key"]) for p in pats):
                        fid, method = f.id, "rule"
                        break
            self.db.execute(
                "UPDATE standard_term SET family_id=?, family_method=? WHERE id=?",
                (fid, method, r["id"]),
            )
            counts[method] += 1
        self.db.commit()
        return counts

    def unclassified(self, kind: str = "symptom") -> list[StandardRow]:
        """Standards of `kind` with no family, most-hit first: the second review queue."""
        rows = [
            s for s in self.standards(kind=kind) if s.status != "merged" and s.family_key is None
        ]
        return sorted(rows, key=lambda s: (-s.hits, s.key))

    def import_families_csv(self, path: str | Path, kind: str = "symptom") -> int:
        """Load the family list (key, display, description, patterns, sort_order). Lines whose
        first cell starts with '#' are comments. Existing rows are updated in place by key.
        This is the ONLY way a family comes into being. Header-checked."""
        n = 0
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(strip_comments(fh))
            check_header(reader.fieldnames, self.FAMILY_CSV_FIELDS, path)
            for row in reader:
                key = (row.get("key") or "").strip()
                if not key or key.startswith("#"):
                    continue
                try:
                    order = int((row.get("sort_order") or "0").strip() or 0)
                except ValueError:
                    order = 0
                self.db.execute(
                    "INSERT INTO family (kind, key, display, description, patterns, status, sort_order) "
                    "VALUES (?,?,?,?,?, 'reviewed', ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET display=excluded.display, "
                    "description=excluded.description, patterns=excluded.patterns, sort_order=excluded.sort_order",
                    (
                        kind,
                        key,
                        (row.get("display") or key.replace("_", " ")).strip(),
                        (row.get("description") or "").strip() or None,
                        (row.get("patterns") or "").strip() or None,
                        order,
                    ),
                )
                n += 1
        self.db.commit()
        return n

    # ------------------------------------------------------------------ views

    def display_name(self, standard_id: int) -> str:
        """The name to show: the longest alias stem with no abbreviation in it, ties broken by
        hits. Falls back to the key when every alias is abbreviated. A view, never stored."""
        sid = self._follow(standard_id)
        rows = self.db.execute(
            "SELECT stem, hit_count FROM alias WHERE standard_id=? ORDER BY hit_count DESC, stem",
            (sid,),
        ).fetchall()
        best: tuple[int, int, str] | None = None
        for r in rows:
            stem = r["stem"].strip()
            if not stem or has_abbreviations(stem):
                continue
            cand = (len(stem), r["hit_count"], stem)
            if best is None or cand[:2] > best[:2]:
                best = cand
        if best:
            return best[2]
        key = self.db.execute("SELECT key FROM standard_term WHERE id=?", (sid,)).fetchone()
        return key["key"].capitalize() if key else ""

    def standards(self, kind: str | None = None, status: str | None = None) -> list[StandardRow]:
        q = (
            "SELECT s.*, f.key AS family_key FROM standard_term s "
            "LEFT JOIN family f ON f.id = s.family_id WHERE 1=1"
        )
        args: list = []
        if kind:
            q += " AND s.kind=?"
            args.append(kind)
        if status:
            q += " AND s.status=?"
            args.append(status)
        q += " ORDER BY s.kind, s.key"
        out = []
        for s in self.db.execute(q, args).fetchall():
            al = self.db.execute(
                "SELECT raw_term, hit_count FROM alias WHERE standard_id=? "
                "ORDER BY hit_count DESC, raw_term",
                (s["id"],),
            ).fetchall()
            out.append(
                StandardRow(
                    id=s["id"],
                    kind=s["kind"],
                    key=s["key"],
                    status=s["status"],
                    display=self.display_name(s["id"]),
                    aliases=[a["raw_term"] for a in al],
                    hits=sum(a["hit_count"] for a in al),
                    first_seen=s["first_seen"],
                    family_key=s["family_key"],
                    family_method=s["family_method"] or "none",
                )
            )
        return out

    def review_queue(self, kind: str | None = None) -> list[StandardRow]:
        """Standards minted by the rules and not yet looked at (case b): the first queue.
        Pending rows are not standards; see `pending()` for the third queue."""
        return self.standards(kind=kind, status="auto")

    def alias_rows(self, kind: str | None = None) -> list[dict]:
        """One row per alias for the admin lookup page (12 §5.1): alias, canonical, method, count,
        parts (dict or None)."""
        q = (
            "SELECT a.kind, a.raw_term, a.raw_key, a.stem, a.method, a.source, a.hit_count, a.first_seen, "
            "a.last_seen, a.standard_id, a.parts, s.key, s.status, s.family_method, f.key AS family_key "
            "FROM alias a JOIN standard_term s ON s.id = a.standard_id "
            "LEFT JOIN family f ON f.id = s.family_id"
        )
        args: list = []
        if kind:
            q += " WHERE a.kind=?"
            args.append(kind)
        q += " ORDER BY a.kind, s.key, a.hit_count DESC, a.raw_term"
        rows = []
        for r in self.db.execute(q, args).fetchall():
            d = dict(r)
            d["parts"] = _loads(d.get("parts"))
            d["display"] = self.display_name(r["standard_id"])
            rows.append(d)
        return rows

    def fragmentation(self, kind: str | None = None) -> dict:
        """Health numbers from 02 §3.9: standards, unreviewed mints, aliases per standard, how
        many standards have no family, and how many spellings are queued (not minted). Totals at
        the top level, plus `by_kind`."""
        std = self.standards(kind=kind)
        pend = self.pending(kind=kind)

        def _counts(rows: list[StandardRow], pending: list[PendingRow]) -> dict:
            live = [s for s in rows if s.status != "merged"]
            return {
                "standards": len(live),
                "auto": sum(1 for s in live if s.status == "auto"),
                "reviewed": sum(1 for s in live if s.status == "reviewed"),
                "merged": len(rows) - len(live),
                "aliases": sum(len(s.aliases) for s in live),
                "singletons": sum(1 for s in live if len(s.aliases) == 1),
                "unclassified": sum(1 for s in live if s.family_key is None),
                "n_pending": len(pending),
            }

        out = _counts(std, pend)
        out["by_kind"] = {
            k: _counts([s for s in std if s.kind == k], [p for p in pend if p.kind == k])
            for k in KINDS
            if kind in (None, k)
        }
        out["families"] = {k: len(self.families(k)) for k in KINDS if kind in (None, k)}
        return out

    # ------------------------------------------------------------------ CSV in / out

    def import_csv(self, path: str | Path, source: str = "seed") -> int:
        """Load a human-edited CSV (kind, raw_term, standard_key, status[, family_key]). Rows win
        over what the rules decided earlier. Blank standard_key means the normalised raw_term;
        blank family_key leaves the family unchanged. Lines whose first cell starts with '#' are
        comments. Header-checked. Returns the number of rows applied."""
        n = 0
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(strip_comments(fh))
            check_header(reader.fieldnames, self.CSV_REQUIRED, path)
            for row in reader:
                kind = (row.get("kind") or "").strip()
                if not kind or kind.startswith("#"):
                    continue
                raw = (row.get("raw_term") or "").strip()
                if not raw:
                    continue
                key = (row.get("standard_key") or "").strip() or self.clean(raw, kind).key
                if not key:
                    continue
                sid = self.set_alias(raw, key, kind=kind, source=source)
                status = (row.get("status") or "reviewed").strip()
                if status in ("auto", "reviewed"):
                    self.db.execute("UPDATE standard_term SET status=? WHERE id=?", (status, sid))
                fam = (row.get("family_key") or "").strip()
                if fam:
                    self.set_family(raw, fam, kind=kind, source=source)
                n += 1
        self.db.commit()
        return n

    def export_csv(self, path: str | Path, kind: str | None = None) -> int:
        """Write every alias as (kind, raw_term, standard_key, status, family_key) for review in a
        spreadsheet or a git diff. Re-importable; a blank family_key means unchanged."""
        rows = self.alias_rows(kind)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(self.CSV_FIELDS)
            for r in rows:
                w.writerow([r["kind"], r["raw_term"], r["key"], r["status"], r["family_key"] or ""])
        return len(rows)

    # ------------------------------------------------------------------ internals

    def _mint(self, kind: str, key: str, source: str, now: str) -> int:
        status = "reviewed" if source in ("seed", "human") else "auto"
        cur = self.db.execute(
            "INSERT INTO standard_term (kind, key, status, source, first_seen) VALUES (?,?,?,?,?)",
            (kind, key, status, source, now),
        )
        return int(cur.lastrowid)

    def _standard_id(self, kind: str, key: str, *, source: str, now: str) -> int:
        row = self.db.execute(
            "SELECT id FROM standard_term WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        if row:
            return self._follow(row["id"])
        return self._mint(kind, key, source, now)

    def _insert_alias(
        self, kind: str, rk: str, raw: str, c: Cleaned, sid: int, method: str, source: str, now: str
    ) -> None:
        assert method in ALIAS_METHODS, method
        self.db.execute(
            "INSERT INTO alias (kind, raw_key, raw_term, stem, standard_id, method, source, "
            "first_seen, last_seen, hit_count, parts) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
            (
                kind,
                rk,
                raw.strip(),
                c.stem or raw.strip(),
                sid,
                method,
                source,
                now,
                now,
                _dumps(c.parts),
            ),
        )

    def _unqueue(self, kind: str, rk: str) -> None:
        self.db.execute("DELETE FROM pending_term WHERE kind=? AND raw_key=?", (kind, rk))

    def _log(
        self,
        kind: str,
        raw_key: str,
        chain: str,
        before: str | None,
        after: str | None,
        standard_id: int | None,
        rationale: str | None,
        ts: str,
    ) -> None:
        self.db.execute(
            'INSERT INTO resolution_log (ts, kind, raw_key, method_chain, "before", "after", '
            "standard_id, rationale) VALUES (?,?,?,?,?,?,?,?)",
            (ts, kind, raw_key, chain, before, after, standard_id, rationale),
        )

    def _key_of(self, sid: int) -> str:
        row = self.db.execute("SELECT key FROM standard_term WHERE id=?", (sid,)).fetchone()
        return row["key"] if row else ""

    def _follow(self, sid: int) -> int:
        seen: set[int] = set()
        while sid not in seen:
            seen.add(sid)
            row = self.db.execute(
                "SELECT merged_into FROM standard_term WHERE id=?", (sid,)
            ).fetchone()
            if not row or row["merged_into"] is None:
                return sid
            sid = row["merged_into"]
        return sid

    def _resolution(self, sid: int, c: Cleaned, method: str) -> Resolution:
        return Resolution(
            standard_id=sid,
            key=self._key_of(sid),
            position=c.position,
            method=method,
            display=self.display_name(sid),
        )

    @staticmethod
    def _family_row(r: sqlite3.Row) -> FamilyRow:
        return FamilyRow(
            id=r["id"],
            kind=r["kind"],
            key=r["key"],
            display=r["display"],
            description=r["description"],
            patterns=[p.strip() for p in (r["patterns"] or "").split("|") if p.strip()],
            sort_order=r["sort_order"],
        )

    @staticmethod
    def _pending_row(r: sqlite3.Row) -> PendingRow:
        return PendingRow(
            kind=r["kind"],
            raw_key=r["raw_key"],
            raw_term=r["raw_term"],
            reason=r["reason"],
            parts=_loads(r["parts"]),
            source=r["source"],
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
            hit_count=r["hit_count"],
        )

    def __iter__(self) -> Iterator[StandardRow]:
        return iter(self.standards())


_strip_comments = strip_comments  # kept name for older imports


def symptom_guard(kind: str, c: Cleaned, raw: str) -> str | None:
    """The mint guard for kind="symptom" (docs/13 §4, §8): a label whose core still carries a
    component / location / modifier term (or that is only such a term) did not decompose cleanly;
    it is queued as `undecomposed` for a human rather than minted as a failure mode. Position
    tokens, the record's equipment words and fleet vessel names never reach the core, so the
    leftover list is the whole test."""
    if kind != "symptom" or not c.parts:
        return None
    if c.parts.get("leftover"):
        return "undecomposed"
    return None


def equipment_guard(kind: str, c: Cleaned, raw: str) -> str | None:
    """The mint guard for kinds equipment / part (docs/13 §4): the cleaner records in
    `parts["issues"]` what it could not get out of the key — a fleet vessel name
    (contains_vessel), a position / side token (contains_position), non-Latin text
    (needs_translation), the record entry's maker / model / serial (contains_identifier). The
    first issue is the reason; a clean label is minted as before."""
    if kind not in ("equipment", "part") or not c.parts:
        return None
    issues = c.parts.get("issues") or []
    return issues[0] if issues else None


def compose_guards(*guards: Guard | None) -> Guard:
    """One guard out of several: the first reason wins."""

    def guard(kind: str, c: Cleaned, raw: str) -> str | None:
        for g in guards:
            if g:
                reason = g(kind, c, raw)
                if reason:
                    return reason
        return None

    return guard


DEFAULT_GUARD = compose_guards(symptom_guard, equipment_guard)


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
