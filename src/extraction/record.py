"""
The carried record: header from the export, then window deltas applied by code.

The model returns a *delta* (add / update), not a rewritten record. That makes
rule 1 of 02 §2.2 ("update, never regenerate; nothing disappears") structural
instead of hoped-for — but `assert_no_dropped()` is still run after every
merge, because a rule that is only true by construction today is a rule
nobody checks tomorrow.

Everything the model can get wrong in shape is repaired here with a flag, not
an exception: an unknown enum falls back, a malformed date becomes null, a
mail id from outside the window is dropped, an outcome without a quote is
downgraded to `unknown`. A delta that cannot be applied at all (ids collide,
references do not resolve) raises DeltaError so the caller can retry once with
the reason in the prompt.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import EXTRACTION_VERSION
from src.corpus.export import Casefile
from src.extraction.translate import normalise_quote

_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OID_RX = re.compile(r"^[0-9a-f]{24}$")
_SHA_RX = re.compile(r"^[0-9a-f]{64}$")

KINDS = {"corrective", "diagnostic", "preventive", "administrative", "commercial"}
ROLES = {"vessel_crew", "superintendent", "oem", "vendor", "yard", "agent", "class", "unknown"}
ASSERTED_BY = {"vessel", "superintendent", "oem", "vendor", "yard", "class"}
VERIFICATIONS = {"confirmed_by_vessel", "confirmed_by_oem", "confirmed_by_vendor", "reported_only", "none"}
OUTCOMES_WORK = {"resolved", "partial", "no_effect", "made_worse", "unknown"}
OUTCOMES_DIAG = {"conclusive", "inconclusive", "no_information", "unknown"}

ID_PREFIX = {"equipment": "e", "problems": "p", "faults": "f", "action_logs": "a"}
ID_FIELD = {"equipment": "equipment_id", "problems": "problem_id", "faults": "fault_id", "action_logs": "action_id"}

UNKNOWN_OUTCOME = {"value": "unknown", "verification": "none", "outcome_in_mail": None, "outcome_date": None, "evidence_quote": None}


class DeltaError(Exception):
    """The delta cannot be applied; the message is fit to show the model on retry."""


def new_record(casefile: Casefile) -> dict:
    """Step 1 of 08 §1: the header, no model."""
    return {
        "casefile_id": casefile.casefile_id,
        "imo": casefile.imo if casefile.imo is not None else 0,
        "vessel_name": casefile.vessel_name or "",
        "extraction_version": EXTRACTION_VERSION,
        "progress": {"last_pagenum_processed": -1, "complete": False},
        "problem_statement": (casefile.title or "")[:160],
        "export_meta": casefile.export_meta(),
        "equipment": [],
        "problems": [],
        "faults": [],
        "action_logs": [],
        "validation_flags": [],
        "needs_review": False,
    }


def ids_of(record: dict) -> dict[str, set[str]]:
    return {coll: {row[ID_FIELD[coll]] for row in record.get(coll, [])} for coll in ID_PREFIX}


def all_ids(record: dict) -> set[str]:
    return set().union(*ids_of(record).values())


def next_id(record: dict, coll: str) -> str:
    prefix = ID_PREFIX[coll]
    nums = [int(i[1:]) for i in ids_of(record)[coll] if i[1:].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1}"


def assert_no_dropped(before: dict[str, set[str]], record: dict) -> list[str]:
    """dropped_object flags for ids that existed before and do not now."""
    after = ids_of(record)
    return [f"dropped_object:{i}" for coll in before for i in sorted(before[coll] - after[coll])]


# ---------------------------------------------------------------------------
# Field coercion helpers — each returns (value, flag_or_None)
# ---------------------------------------------------------------------------

def _date(v: Any, where: str) -> tuple[Optional[str], Optional[str]]:
    if v is None or v == "":
        return None, None
    s = str(v).strip()
    if _DATE_RX.match(s):
        return s, None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]", s)
    if m:
        return m.group(1), None
    return None, f"bad_date:{where}:{s[:20]}"


def _plus_days(day: str, n: int) -> str:
    """YYYY-MM-DD plus n days; the input unchanged when it is not a date."""
    from datetime import date, timedelta

    try:
        return (date.fromisoformat(day[:10]) + timedelta(days=n)).isoformat()
    except ValueError:
        return day


def _enum(v: Any, allowed: set[str], fallback: str, where: str) -> tuple[str, Optional[str]]:
    s = str(v).strip().lower() if v is not None else ""
    if s in allowed:
        return s, None
    return fallback, f"bad_enum:{where}:{s[:30] or 'null'}"


def _str(v: Any, default: Optional[str] = None) -> Optional[str]:
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if x is not None and str(x).strip()]


def _na(v: Any) -> str:
    s = _str(v)
    return s if s else "NA"


@dataclass
class WindowContext:
    """What the model was allowed to cite in this window."""

    mail_ids: set[str]
    mail_dates: dict[str, str]  # mail_id -> ISO date-time of the page
    shas: set[str]
    max_actions: int = 25


@dataclass
class Applied:
    added: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in ID_PREFIX})
    updated: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in ID_PREFIX})
    flags: list[str] = field(default_factory=list)
    dropped_rows: list[str] = field(default_factory=list)


def _provenance(raw: Any, ctx: WindowContext, where: str, flags: list[str]) -> Optional[dict]:
    """A valid provenance or None when nothing citable remains."""
    if not isinstance(raw, dict):
        flags.append(f"missing_provenance:{where}")
        return None
    mail_ids = []
    for m in _str_list(raw.get("mail_ids")):
        if m in ctx.mail_ids:
            if m not in mail_ids:
                mail_ids.append(m)
        else:
            flags.append(f"mail_id_out_of_window:{where}:{m[:24]}")
    shas = []
    for s in _str_list(raw.get("attachment_shas")):
        s = s.lower()
        if s in ctx.shas:
            if s not in shas:
                shas.append(s)
        else:
            flags.append(f"sha_out_of_window:{where}:{s[:8]}")
    quote = normalise_quote(_str(raw.get("evidence_quote")))
    if not mail_ids or not quote:
        flags.append(f"row_without_evidence:{where}")
        return None
    return {"mail_ids": mail_ids, "attachment_shas": shas, "evidence_quote": quote}


def _merge_provenance(existing: dict, add: Any, ctx: WindowContext, where: str, flags: list[str], fields: Optional[list[str]] = None) -> None:
    """
    Evidence for an UPDATE goes into provenance.supporting as its own entry —
    its mails, its attachment, its quote, the fields it changed. The row's
    primary quote and attachment_shas are left alone: unioning an update's
    attachment into them made one quote look cited to documents that never
    contained it (10-casefile review, "one quote under several hashes").
    """
    if not isinstance(add, dict):
        return
    mails = [m for m in dict.fromkeys(_str_list(add.get("mail_ids"))) if m in ctx.mail_ids]
    for m in mails:
        if m not in existing["mail_ids"]:
            existing["mail_ids"].append(m)
    shas = [s for s in dict.fromkeys(x.lower() for x in _str_list(add.get("attachment_shas"))) if s in ctx.shas]
    q = normalise_quote(_str(add.get("evidence_quote")))
    if q and mails:
        if q == existing.get("evidence_quote") and not shas:
            return  # the same words again add nothing
        entry = {"mail_ids": mails, "attachment_shas": shas, "evidence_quote": q, "fields": sorted(fields or [])}
        sup = existing.setdefault("supporting", [])
        if entry not in sup:
            sup.append(entry)
    elif shas:
        flags.append(f"update_without_quote:{where}")


def _outcome(raw: Any, kind: str, ctx: WindowContext, where: str, flags: list[str]) -> dict:
    if not isinstance(raw, dict):
        return dict(UNKNOWN_OUTCOME)
    allowed = OUTCOMES_DIAG if kind == "diagnostic" else OUTCOMES_WORK
    value, f = _enum(raw.get("value"), allowed, "unknown", f"{where}.outcome.value")
    if f:
        flags.append(f)
    if value == "unknown":
        return dict(UNKNOWN_OUTCOME)
    mail = _str(raw.get("outcome_in_mail"))
    quote = normalise_quote(_str(raw.get("evidence_quote")))
    if not mail or mail not in ctx.mail_ids or not quote:
        flags.append(f"outcome_downgraded:{where}:{'no_mail' if not mail else 'mail_out_of_window' if mail not in ctx.mail_ids else 'no_quote'}")
        return dict(UNKNOWN_OUTCOME)
    verification, f = _enum(raw.get("verification"), VERIFICATIONS - {"none"}, "reported_only", f"{where}.outcome.verification")
    if f:
        flags.append(f)
    date, f = _date(raw.get("outcome_date"), f"{where}.outcome_date")
    if f:
        flags.append(f)
    if date is None:
        date = (ctx.mail_dates.get(mail) or "")[:10] or None
    return {"value": value, "verification": verification, "outcome_in_mail": mail, "outcome_date": date, "evidence_quote": quote}


# ---------------------------------------------------------------------------
# Row builders for `add`
# ---------------------------------------------------------------------------

def _build_equipment(raw: dict, ctx: WindowContext, flags: list[str]) -> Optional[dict]:
    eid = raw.get("equipment_id")
    prov = _provenance(raw.get("provenance"), ctx, str(eid), flags)
    name = _str(raw.get("name"))
    if prov is None or not name:
        return None
    return {
        "equipment_id": eid,
        "name": name,
        "equipment_type": _str(raw.get("equipment_type"), "unknown"),
        "position": _str(raw.get("position")),
        "maker": _na(raw.get("maker")),
        "model": _na(raw.get("model")),
        "serial": _str(raw.get("serial")),
        "details": _str(raw.get("details")),
        "provenance": prov,
    }


def _build_problem(raw: dict, ctx: WindowContext, flags: list[str]) -> Optional[dict]:
    pid = raw.get("problem_id")
    prov = _provenance(raw.get("provenance"), ctx, str(pid), flags)
    statement = _str(raw.get("statement"))
    if prov is None or not statement:
        return None
    asserted_by, f = _enum(raw.get("asserted_by"), ASSERTED_BY, "vessel", f"{pid}.asserted_by")
    if f:
        flags.append(f)
    date, f = _date(raw.get("concluded_at"), f"{pid}.concluded_at")
    if f:
        flags.append(f)
    return {"problem_id": pid, "statement": statement, "asserted_by": asserted_by, "concluded_at": date, "provenance": prov}


def _build_fault(
    raw: dict, ctx: WindowContext, flags: list[str], eq_ids: set[str], pr_ids: set[str], fault_ids: Optional[set[str]] = None
) -> Optional[dict]:
    fid = raw.get("fault_id")
    prov = _provenance(raw.get("provenance"), ctx, str(fid), flags)
    symptom = _str(raw.get("symptom"))
    if prov is None or not symptom:
        return None
    eq = _str(raw.get("equipment_id"))
    if eq not in eq_ids:
        raise DeltaError(f"fault {fid} points at equipment_id {eq!r}, which is neither in the record nor in add.equipment")
    pr = _str(raw.get("problem_id"))
    if pr is not None and pr not in pr_ids:
        flags.append(f"unresolved_problem_id:{fid}:{pr}")
        pr = None
    date, f = _date(raw.get("first_observed_at"), f"{fid}.first_observed_at")
    if f:
        flags.append(f)
    # A returning symptom is its own row, linked to the earlier fault.
    rec_of = _str(raw.get("recurrence_of"))
    if rec_of is not None and (rec_of == fid or rec_of not in (fault_ids or set())):
        flags.append(f"unresolved_recurrence_of:{fid}:{rec_of}")
        rec_of = None
    return {
        "fault_id": fid,
        "symptom": symptom,
        "alarms": _str_list(raw.get("alarms")),
        "equipment_id": eq,
        "position": _str(raw.get("position")),
        "problem_id": pr,
        "suspected_cause": _str(raw.get("suspected_cause")),
        "first_observed_at": date,
        "recurrence_of": rec_of,
        "provenance": prov,
    }


def _build_action(raw: dict, ctx: WindowContext, flags: list[str], fault_ids: set[str], seq: int) -> Optional[dict]:
    aid = raw.get("action_id")
    prov = _provenance(raw.get("provenance"), ctx, str(aid), flags)
    action = _str(raw.get("action"))
    if prov is None or not action:
        return None
    fid = _str(raw.get("fault_id"))
    if fid not in fault_ids:
        raise DeltaError(f"action {aid} points at fault_id {fid!r}, which is neither in the record nor in add.faults")
    kind, f = _enum(raw.get("kind"), KINDS, "corrective", f"{aid}.kind")
    if f:
        flags.append(f)
    role, f = _enum(raw.get("performed_by_role"), ROLES, "unknown", f"{aid}.performed_by_role")
    if f:
        flags.append(f)
    asserted = _str(raw.get("asserted_in_mail"))
    if asserted not in ctx.mail_ids:
        # Fall back to the first cited mail: the row is still evidenced.
        flags.append(f"asserted_in_mail_repaired:{aid}")
        asserted = prov["mail_ids"][0]
    date_reported = ctx.mail_dates.get(asserted) or ""
    if not date_reported:
        flags.append(f"missing_mail_date:{aid}")
        date_reported = "1970-01-01T00:00:00Z"
    date_performed, f = _date(raw.get("date_performed"), f"{aid}.date_performed")
    if f:
        flags.append(f)
    # date_performed is for work DONE. A date later than the mail that reports it
    # (one day of slack for vessel-local vs UTC) is a plan, not a performance.
    if date_performed and date_reported[:4] != "1970" and date_performed > _plus_days(date_reported, 1):
        flags.append(f"future_date_performed:{aid}:{date_performed}")
        date_performed = None
    # One event is one row: other faults the same event serves are listed, not copied.
    also = []
    for x in dict.fromkeys(_str_list(raw.get("also_fault_ids"))):
        if x == fid:
            continue
        if x in fault_ids:
            also.append(x)
        else:
            flags.append(f"unresolved_fault_id:{aid}:{x}")
    return {
        "action_id": aid,
        "fault_id": fid,
        "also_fault_ids": also,
        "seq": seq,
        "occurrence_no": 1,
        "target": _str(raw.get("target")),
        "action": action,
        "kind": kind,
        "performed_by_role": role,
        "advised_by_org": _str(raw.get("advised_by_org")),
        "parts": _str_list(raw.get("parts")),
        "date_performed": date_performed,
        "asserted_in_mail": asserted,
        "date_reported": date_reported,
        "outcome": _outcome(raw.get("outcome"), kind, ctx, str(aid), flags),
        "finding": _str(raw.get("finding")),
        "provenance": prov,
    }


# ---------------------------------------------------------------------------
# apply_delta
# ---------------------------------------------------------------------------

def _check_new_ids(record: dict, adds: dict[str, list[dict]]) -> None:
    """New ids must be unused and continue each sequence without gaps."""
    existing = ids_of(record)
    for coll, rows in adds.items():
        prefix = ID_PREFIX[coll]
        field_name = ID_FIELD[coll]
        nums = sorted(int(i[1:]) for i in existing[coll] if i[1:].isdigit())
        expected = (nums[-1] + 1) if nums else 1
        seen: set[str] = set()
        for row in rows:
            rid = row.get(field_name)
            if not isinstance(rid, str) or not re.match(rf"^{prefix}\d+$", rid):
                raise DeltaError(f"add.{coll}: {field_name} {rid!r} must look like {prefix}<n>")
            if rid in existing[coll] or rid in seen:
                raise DeltaError(f"add.{coll}: id {rid} already exists — new ids must continue the sequence (next free is {prefix}{expected})")
            if int(rid[1:]) != expected:
                raise DeltaError(f"add.{coll}: id {rid} skips the sequence — next free id is {prefix}{expected}")
            seen.add(rid)
            expected += 1


def _rows(delta_part: Any, key: str) -> list[dict]:
    if not isinstance(delta_part, dict):
        return []
    v = delta_part.get(key) or []
    if not isinstance(v, list):
        raise DeltaError(f"{key} must be a list")
    return [r for r in v if isinstance(r, dict)]


def apply_delta(record: dict, delta: Any, ctx: WindowContext) -> Applied:
    """
    Mutate `record` with a validated delta. Raises DeltaError when the delta is
    structurally unusable (nothing has been changed in that case).
    """
    if not isinstance(delta, dict):
        raise DeltaError("response must be a JSON object with keys add, update")
    add, update = delta.get("add") or {}, delta.get("update") or {}
    if not isinstance(add, dict) or not isinstance(update, dict):
        raise DeltaError("add and update must be objects")

    adds = {coll: _rows(add, coll) for coll in ID_PREFIX}
    _check_new_ids(record, adds)

    work = copy.deepcopy(record)
    applied = Applied()
    flags = applied.flags

    eq_ids = ids_of(work)["equipment"] | {r["equipment_id"] for r in adds["equipment"]}
    fa_ids = ids_of(work)["faults"] | {r["fault_id"] for r in adds["faults"]}

    # Rows that fail evidence checks are dropped; their ids are then holes in the
    # sequence, which is why ids are re-checked on the *kept* rows below.
    built: dict[str, list[dict]] = {k: [] for k in ID_PREFIX}
    for raw in adds["equipment"]:
        row = _build_equipment(raw, ctx, flags)
        (built["equipment"].append(row) if row else applied.dropped_rows.append(str(raw.get("equipment_id"))))
    for raw in adds["problems"]:
        row = _build_problem(raw, ctx, flags)
        (built["problems"].append(row) if row else applied.dropped_rows.append(str(raw.get("problem_id"))))
    kept_eq = ids_of(work)["equipment"] | {r["equipment_id"] for r in built["equipment"]}
    kept_pr = ids_of(work)["problems"] | {r["problem_id"] for r in built["problems"]}
    for raw in adds["faults"]:
        if _str(raw.get("equipment_id")) in eq_ids and _str(raw.get("equipment_id")) not in kept_eq:
            flags.append(f"row_without_evidence:{raw.get('fault_id')}:equipment_dropped")
            applied.dropped_rows.append(str(raw.get("fault_id")))
            continue
        known_faults = ids_of(work)["faults"] | {r["fault_id"] for r in built["faults"]}
        row = _build_fault(raw, ctx, flags, kept_eq, kept_pr, known_faults)
        (built["faults"].append(row) if row else applied.dropped_rows.append(str(raw.get("fault_id"))))
    kept_fa = ids_of(work)["faults"] | {r["fault_id"] for r in built["faults"]}
    seq = len(work["action_logs"])
    for raw in adds["action_logs"]:
        if _str(raw.get("fault_id")) in fa_ids and _str(raw.get("fault_id")) not in kept_fa:
            flags.append(f"row_without_evidence:{raw.get('action_id')}:fault_dropped")
            applied.dropped_rows.append(str(raw.get("action_id")))
            continue
        row = _build_action(raw, ctx, flags, kept_fa, seq + 1)
        if row:
            built["action_logs"].append(row)
            seq += 1
        else:
            applied.dropped_rows.append(str(raw.get("action_id")))

    # Renumber kept rows so the sequence has no holes (a dropped row must not
    # leave a gap the next window would trip over). References inside this
    # delta are remapped accordingly.
    remap: dict[str, str] = {}
    for coll in ("equipment", "problems", "faults", "action_logs"):
        field_name = ID_FIELD[coll]
        nxt = int(next_id(work, coll)[1:])
        for row in built[coll]:
            old = row[field_name]
            new = f"{ID_PREFIX[coll]}{nxt}"
            if old != new:
                remap[old] = new
                row[field_name] = new
            nxt += 1
    if remap:
        flags.append("ids_renumbered_after_drop:" + ",".join(f"{o}>{n}" for o, n in remap.items()))
        for row in built["faults"]:
            row["equipment_id"] = remap.get(row["equipment_id"], row["equipment_id"])
            if row["problem_id"]:
                row["problem_id"] = remap.get(row["problem_id"], row["problem_id"])
            if row.get("recurrence_of"):
                row["recurrence_of"] = remap.get(row["recurrence_of"], row["recurrence_of"])
        for row in built["action_logs"]:
            row["fault_id"] = remap.get(row["fault_id"], row["fault_id"])
            row["also_fault_ids"] = [remap.get(x, x) for x in row.get("also_fault_ids") or []]

    for coll in ID_PREFIX:
        work[coll].extend(built[coll])
        applied.added[coll] = [r[ID_FIELD[coll]] for r in built[coll]]

    # ---- updates -----------------------------------------------------------
    by_id = {coll: {r[ID_FIELD[coll]]: r for r in work[coll]} for coll in ID_PREFIX}

    for raw in _rows(update, "equipment"):
        eid = _str(raw.get("equipment_id"))
        row = by_id["equipment"].get(eid)
        if not row:
            flags.append(f"update_unknown_id:{eid}")
            continue
        changed = False
        for k in ("maker", "model"):
            if _str(raw.get(k)) and _str(raw.get(k)).upper() != "NA":
                row[k] = _str(raw[k])
                changed = True
        for k in ("serial", "details", "position", "name", "equipment_type"):
            if _str(raw.get(k)):
                row[k] = _str(raw[k])
                changed = True
        _merge_provenance(
            row["provenance"], raw.get("provenance_add"), ctx, eid, flags,
            fields=[k for k in raw if k not in ("equipment_id", "provenance_add")],
        )
        if changed:
            applied.updated["equipment"].append(eid)

    for raw in _rows(update, "faults"):
        fid = _str(raw.get("fault_id"))
        row = by_id["faults"].get(fid)
        if not row:
            flags.append(f"update_unknown_id:{fid}")
            continue
        changed = False
        if "problem_id" in raw:
            pid = _str(raw.get("problem_id"))
            if pid is None or pid in by_id["problems"]:
                if pid is not None:
                    row["problem_id"] = pid
                    changed = True
            else:
                flags.append(f"unresolved_problem_id:{fid}:{pid}")
        for k in ("suspected_cause", "position", "symptom"):
            if _str(raw.get(k)):
                row[k] = _str(raw[k])
                changed = True
        if raw.get("alarms"):
            for a in _str_list(raw.get("alarms")):
                if a not in row["alarms"]:
                    row["alarms"].append(a)
                    changed = True
        if raw.get("first_observed_at"):
            d, f = _date(raw.get("first_observed_at"), f"{fid}.first_observed_at")
            if f:
                flags.append(f)
            elif d and (row["first_observed_at"] is None or d < row["first_observed_at"]):
                row["first_observed_at"] = d
                changed = True
        _merge_provenance(
            row["provenance"], raw.get("provenance_add"), ctx, fid, flags,
            fields=[k for k in raw if k not in ("fault_id", "provenance_add")],
        )
        if changed:
            applied.updated["faults"].append(fid)

    for raw in _rows(update, "action_logs"):
        aid = _str(raw.get("action_id"))
        row = by_id["action_logs"].get(aid)
        if not row:
            flags.append(f"update_unknown_id:{aid}")
            continue
        changed = False
        new_fault = _str(raw.get("fault_id"))
        if new_fault and new_fault != row["fault_id"]:
            # Correcting a wrong link (v4): allowed, always flagged in the window audit.
            if new_fault in by_id["faults"]:
                flags.append(f"fault_id_corrected:{aid}:{row['fault_id']}>{new_fault}")
                row["fault_id"] = new_fault
                changed = True
            else:
                flags.append(f"unresolved_fault_id:{aid}:{new_fault}")
        if isinstance(raw.get("outcome"), dict):
            new = _outcome(raw["outcome"], row["kind"], ctx, aid, flags)
            if new["value"] != "unknown":
                row["outcome"] = new
                changed = True
        for x in dict.fromkeys(_str_list(raw.get("also_fault_ids"))):
            if x in by_id["faults"] and x != row["fault_id"]:
                if x not in row.setdefault("also_fault_ids", []):
                    row["also_fault_ids"].append(x)
                    changed = True
            elif x != row["fault_id"]:
                flags.append(f"unresolved_fault_id:{aid}:{x}")
        if row.get("also_fault_ids"):
            row["also_fault_ids"] = [x for x in row["also_fault_ids"] if x != row["fault_id"]]
        if raw.get("date_performed"):
            d, f = _date(raw.get("date_performed"), f"{aid}.date_performed")
            latest = max((v[:10] for v in ctx.mail_dates.values() if v), default="")
            if f:
                flags.append(f)
            elif d and latest and d > _plus_days(latest, 1):
                flags.append(f"future_date_performed:{aid}:{d}")  # a plan, not a performance
            elif d:
                row["date_performed"] = d
                changed = True
        for k in ("target", "finding", "advised_by_org"):
            if _str(raw.get(k)):
                row[k] = _str(raw[k])
                changed = True
        if raw.get("parts"):
            for p in _str_list(raw.get("parts")):
                if p not in row["parts"]:
                    row["parts"].append(p)
                    changed = True
        _merge_provenance(
            row["provenance"], raw.get("provenance_add"), ctx, aid, flags,
            fields=[k for k in raw if k not in ("action_id", "provenance_add")],
        )
        if changed:
            applied.updated["action_logs"].append(aid)

    dropped = assert_no_dropped(ids_of(record), work)
    if dropped:  # cannot happen with a delta; kept as the guard 02 §2.2 asks for
        flags.extend(dropped)
        raise DeltaError("internal: rows disappeared during merge")

    record.clear()
    record.update(work)
    return applied


def strip_sidecars(record: dict) -> dict:
    """
    Remove keys the schema does not allow (`provenance.update_quotes`) for the
    stored record; they are kept in the window audit files instead.
    """
    out = copy.deepcopy(record)
    for coll in ID_PREFIX:
        for row in out.get(coll, []):
            prov = row.get("provenance") or {}
            prov.pop("update_quotes", None)
    return out


def collect_update_quotes(record: dict) -> list[dict]:
    out = []
    for coll in ID_PREFIX:
        for row in record.get(coll, []):
            for q in (row.get("provenance") or {}).get("update_quotes") or []:
                out.append({"row": row[ID_FIELD[coll]], **q})
    return out


def summary_for_prompt(record: dict) -> dict:
    """
    The record as the WINDOW prompt shows it: every row with its id and the
    fields the model needs to recognise it again — no provenance, no quotes,
    no seq. This is the model's memory across windows (it replaces the mail
    overlap): it updates a row by id or mints the next free id.
    """
    eq = [
        {"equipment_id": e["equipment_id"], "name": e["name"], "equipment_type": e["equipment_type"],
         "position": e.get("position"), "maker": e.get("maker"), "model": e.get("model")}
        for e in record.get("equipment", [])
    ]
    pr = [
        {"problem_id": p["problem_id"], "statement": p["statement"], "asserted_by": p.get("asserted_by"),
         "concluded_at": p.get("concluded_at")}
        for p in record.get("problems", [])
    ]
    fa = [
        {"fault_id": f["fault_id"], "symptom": f["symptom"], "equipment_id": f["equipment_id"],
         "position": f.get("position"), "problem_id": f.get("problem_id"),
         "suspected_cause": f.get("suspected_cause"), "first_observed_at": f.get("first_observed_at"),
         "recurrence_of": f.get("recurrence_of")}
        for f in record.get("faults", [])
    ]
    ac = [
        {"action_id": a["action_id"], "fault_id": a["fault_id"], "also_fault_ids": a.get("also_fault_ids") or [],
         "action": a["action"], "kind": a["kind"],
         "target": a.get("target"), "performed_by_role": a.get("performed_by_role"),
         "date_performed": a.get("date_performed"), "date_reported": (a.get("date_reported") or "")[:10],
         "outcome": a["outcome"]["value"], "outcome_verification": a["outcome"]["verification"]}
        for a in record.get("action_logs", [])
    ]
    return {
        "equipment": eq,
        "problems": pr,
        "faults": fa,
        "action_logs": ac,
        "next_free_ids": {coll: next_id(record, coll) for coll in ID_PREFIX},
    }


def compact_for_prompt(record: dict) -> dict:
    """The record as the CONSOLIDATE prompt shows it: the four lists with their primary evidence only."""
    out = strip_sidecars({k: v for k, v in record.items() if k in ID_PREFIX})
    for coll in ID_PREFIX:
        for row in out.get(coll, []):
            (row.get("provenance") or {}).pop("supporting", None)
    return out
