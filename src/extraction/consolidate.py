"""
Step 3 of 08 §1: consolidate. Deterministic clean-up, one model call over the
record only, then guarded application.

Order:
  1. collapse_fanout()        code only. ONE event recorded as N rows under N
                              faults (same mail, same quote, same kind, similar
                              text) becomes one row: fault_id is the first
                              fault, the others go to also_fault_ids.
  2. fault_pair_candidates()  code only. Pairs of faults on the same equipment
                              whose symptoms read alike, not yet linked.
  3. the model (consolidate/vN) judges each pair — same | recurrence | distinct —
     and proposes duplicate action merges and repeat groups.
  4. apply_fault_verdicts()   guarded: same equipment; the numbers and sides in
                              symptom+position must not conflict (cylinder 3 is
                              not cylinder 6); a "same" verdict becomes a
                              recurrence when an action on the earlier fault was
                              stated resolved before the later one was observed.
  5. apply_consolidation()    action merges (guarded by fault, kind and dates),
                              repeat groups -> occurrence_no, seq by date.

Removing a row (a merged action, a collapsed fan-out row, a merged fault) is
the one sanctioned removal in the pipeline. Every removed row is kept whole in
run_log.consolidate so it can always be traced back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.config import CONSOLIDATE_FAULT_PAIR_MIN_SIM, CONSOLIDATE_MERGE_MAX_DAYS
from src.extraction.mail_text import normalise_for_match
from src.extraction.record import compact_for_prompt
from src.extraction.textsim import leading_phrase, signatures_agree, similarity
from src.extraction.translate import strip_translation
from src.extraction.window_update import json_call_with_budget
from src.llm import LLMClient, LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

FANOUT_MIN_SIM = 0.7  # action texts of one event differ only in which fault they name
FANOUT_MIN_SIM_SAME_LEAD = 0.5  # ...and when they open with the same verb phrase, the tail may differ more
SIMILAR_FAULTS_FLAG_SIM = 0.85  # pairs left distinct above this are reported (advisory)
MAX_FAULT_PAIRS = 40


def _order_key(action: dict) -> tuple:
    d = action.get("date_performed") or (action.get("date_reported") or "")[:10] or "9999-12-31"
    num = int(action["action_id"][1:]) if action["action_id"][1:].isdigit() else 0
    return (d, action.get("date_reported") or "", num)


def _id_num(row_id: str) -> int:
    return int(row_id[1:]) if row_id[1:].isdigit() else 0


def assign_seq(record: dict) -> None:
    """seq 1..n by date_performed else date_reported, ties by id order."""
    for i, a in enumerate(sorted(record["action_logs"], key=_order_key), start=1):
        a["seq"] = i
    record["action_logs"].sort(key=lambda a: a["seq"])


def _union_provenance(keep: dict, drop: dict) -> None:
    kp, dp = keep["provenance"], drop["provenance"]
    for m in dp.get("mail_ids") or []:
        if m not in kp["mail_ids"]:
            kp["mail_ids"].append(m)
    for s in dp.get("attachment_shas") or []:
        if s not in kp.setdefault("attachment_shas", []):
            kp["attachment_shas"].append(s)
    for sup in dp.get("supporting") or []:
        if sup not in kp.setdefault("supporting", []):
            kp["supporting"].append(sup)


def _merge_pair(keep: dict, drop: dict) -> None:
    """Fold `drop` into `keep`: union provenance and parts, prefer a stated outcome."""
    _union_provenance(keep, drop)
    for p in drop.get("parts") or []:
        if p not in keep["parts"]:
            keep["parts"].append(p)
    if keep["outcome"]["value"] == "unknown" and drop["outcome"]["value"] != "unknown":
        keep["outcome"] = drop["outcome"]
    for k in ("date_performed", "target", "finding", "advised_by_org"):
        if not keep.get(k) and drop.get(k):
            keep[k] = drop[k]
    for f in [drop["fault_id"], *(drop.get("also_fault_ids") or [])]:
        if f != keep["fault_id"] and f not in keep.setdefault("also_fault_ids", []):
            keep["also_fault_ids"].append(f)


def merge_blocked_by_dates(a: dict, b: dict, max_gap_days: int) -> Optional[str]:
    """
    Why two rows cannot be duplicates of ONE event, or None. Different stated
    date_performed is decisive; without both dates, mails further apart than
    max_gap_days are treated as separate occurrences (-1 disables that test).
    """
    da, db = a.get("date_performed"), b.get("date_performed")
    if da and db:
        return None if da == db else f"different date_performed ({da} vs {db})"
    ra, rb = (a.get("date_reported") or "")[:10], (b.get("date_reported") or "")[:10]
    if max_gap_days >= 0 and ra and rb:
        from datetime import date

        try:
            gap = abs((date.fromisoformat(ra) - date.fromisoformat(rb)).days)
        except ValueError:
            return None
        if gap > max_gap_days:
            return f"reported {gap} days apart (limit {max_gap_days})"
    return None


# ---------------------------------------------------------------------------
# 1. Fan-out: one event, several rows
# ---------------------------------------------------------------------------

def _same_event_text(a: str, b: str) -> bool:
    """
    Copies of one event differ in the fault they name ("...the pending knocking
    sound on valve 4" / "...the pending starting failure"), not in what was done.
    """
    sim = similarity(a, b)
    if sim >= FANOUT_MIN_SIM:
        return True
    lead = leading_phrase(a)
    return bool(lead) and lead == leading_phrase(b) and sim >= FANOUT_MIN_SIM_SAME_LEAD


def collapse_fanout(record: dict) -> list[dict]:
    """
    Rows that are ONE event recorded once per fault: same announcing mail, same
    provenance sentence, same kind, action texts that read alike, different
    faults, outcomes that do not contradict. Keep the lowest id; the other
    faults go to also_fault_ids. Rows on the SAME fault are left for the
    duplicate-merge step. Returns the log (dropped rows whole).
    """
    groups: dict[tuple, list[dict]] = {}
    for a in record["action_logs"]:
        quote = normalise_for_match(strip_translation(a["provenance"].get("evidence_quote")) or "")
        groups.setdefault((a.get("asserted_in_mail"), quote, a.get("kind")), []).append(a)

    log: list[dict] = []
    dropped: set[str] = set()
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows = sorted(rows, key=lambda a: _id_num(a["action_id"]))
        keep = rows[0]
        folded = []
        for other in rows[1:]:
            if other["fault_id"] == keep["fault_id"] or other["fault_id"] in (keep.get("also_fault_ids") or []):
                continue
            if not _same_event_text(keep["action"], other["action"]):
                continue  # two different actions reported in one sentence
            ko, oo = keep["outcome"]["value"], other["outcome"]["value"]
            if ko != "unknown" and oo != "unknown" and ko != oo:
                continue  # it worked for one fault and not the other: genuinely two rows
            targets = [t for t in (keep.get("target"), other.get("target")) if t]
            _merge_pair(keep, other)
            if len(set(targets)) > 1:
                keep["target"] = "; ".join(dict.fromkeys(targets))
            dropped.add(other["action_id"])
            folded.append(other)
        if folded:
            log.append({"keep": keep["action_id"], "fault_ids": [keep["fault_id"], *keep["also_fault_ids"]], "dropped_rows": folded})
    if dropped:
        record["action_logs"] = [a for a in record["action_logs"] if a["action_id"] not in dropped]
    return log


# ---------------------------------------------------------------------------
# 2-4. Faults that read alike
# ---------------------------------------------------------------------------

def _fault_text(f: dict) -> str:
    return f"{f.get('position') or ''} {f.get('symptom') or ''}"


def _linked(a: dict, b: dict) -> bool:
    return a.get("recurrence_of") == b["fault_id"] or b.get("recurrence_of") == a["fault_id"]


def fault_pair_candidates(record: dict, min_sim: float = CONSOLIDATE_FAULT_PAIR_MIN_SIM) -> list[dict]:
    """Unlinked fault pairs on one equipment whose symptoms read alike, most similar first."""
    faults = record.get("faults", [])
    pairs = []
    for i, a in enumerate(faults):
        for b in faults[i + 1:]:
            if a["equipment_id"] != b["equipment_id"] or _linked(a, b):
                continue
            sim = similarity(a["symptom"], b["symptom"])
            if sim >= min_sim:
                pairs.append({"a": a["fault_id"], "b": b["fault_id"], "similarity": sim, "numbers_or_sides_differ": not signatures_agree(_fault_text(a), _fault_text(b))})
    pairs.sort(key=lambda p: -p["similarity"])
    return pairs[:MAX_FAULT_PAIRS]


def _resolved_before(record: dict, fault_id: str, when: Optional[str]) -> bool:
    """Was an action on `fault_id` stated resolved on or before `when`?"""
    if not when:
        return False
    for a in record["action_logs"]:
        if a["fault_id"] == fault_id and a["outcome"]["value"] == "resolved":
            d = a["outcome"].get("outcome_date") or a.get("date_performed")
            if d and d <= when:
                return True
    return False


def _merge_faults(record: dict, keep: dict, drop: dict) -> None:
    kid, did = keep["fault_id"], drop["fault_id"]
    for a in record["action_logs"]:
        if a["fault_id"] == did:
            a["fault_id"] = kid
        also = [kid if x == did else x for x in a.get("also_fault_ids") or []]
        a["also_fault_ids"] = [x for x in dict.fromkeys(also) if x != a["fault_id"]]
    for f in record["faults"]:
        if f.get("recurrence_of") == did:
            f["recurrence_of"] = kid if f["fault_id"] != kid else None
    kp, dp = keep["provenance"], drop["provenance"]
    # The dropped row's own evidence becomes a supporting entry of the kept one.
    kp.setdefault("supporting", []).append(
        {"mail_ids": list(dp.get("mail_ids") or []), "attachment_shas": list(dp.get("attachment_shas") or []),
         "evidence_quote": dp.get("evidence_quote"), "fields": [f"merged_from:{did}"]}
    )
    for sup in dp.get("supporting") or []:
        kp["supporting"].append(sup)
    for m in dp.get("mail_ids") or []:
        if m not in kp["mail_ids"]:
            kp["mail_ids"].append(m)
    for al in drop.get("alarms") or []:
        if al not in keep.setdefault("alarms", []):
            keep["alarms"].append(al)
    for k in ("position", "problem_id", "suspected_cause"):
        if not keep.get(k) and drop.get(k):
            keep[k] = drop[k]
    dates = [d for d in (keep.get("first_observed_at"), drop.get("first_observed_at")) if d]
    keep["first_observed_at"] = min(dates) if dates else None
    record["faults"] = [f for f in record["faults"] if f["fault_id"] != did]


def apply_fault_verdicts(record: dict, verdicts: Any, candidates: list[dict]) -> dict:
    """Apply the model's same/recurrence/distinct verdicts on the candidate pairs, with guards."""
    log = {"merged": [], "recurrences": [], "refused": [], "left_distinct": []}
    allowed = {frozenset((c["a"], c["b"])) for c in candidates}
    judged: set[frozenset] = set()
    gone: dict[str, str] = {}  # merged-away fault id -> surviving id

    for v in verdicts if isinstance(verdicts, list) else []:
        if not isinstance(v, dict):
            continue
        a_id, b_id = gone.get(str(v.get("a")), str(v.get("a"))), gone.get(str(v.get("b")), str(v.get("b")))
        verdict = str(v.get("verdict") or "").lower()
        pair = frozenset((str(v.get("a")), str(v.get("b"))))
        by_id = {f["fault_id"]: f for f in record["faults"]}
        if pair not in allowed or a_id == b_id or a_id not in by_id or b_id not in by_id:
            continue  # only pairs the code proposed; never a merge the model invented
        judged.add(pair)
        if verdict not in ("same", "recurrence"):
            continue
        first, second = sorted((by_id[a_id], by_id[b_id]), key=lambda f: (f.get("first_observed_at") or "9999", _id_num(f["fault_id"])))
        entry = {"earlier": first["fault_id"], "later": second["fault_id"], "verdict": verdict, "reason": v.get("reason")}
        if first["equipment_id"] != second["equipment_id"]:
            log["refused"].append({**entry, "why": "different equipment"})
            continue
        if not signatures_agree(_fault_text(first), _fault_text(second)):
            log["refused"].append({**entry, "why": "numbers or sides differ (not the same place)"})
            continue
        if verdict == "same" and _resolved_before(record, first["fault_id"], second.get("first_observed_at")):
            verdict = "recurrence"  # it was fixed, then it came back: that is a return, not a duplicate
            entry["verdict"] = "recurrence (was 'same'; an action was resolved in between)"
        if verdict == "recurrence":
            if not second.get("recurrence_of"):
                second["recurrence_of"] = first["fault_id"]
            log["recurrences"].append(entry)
            continue
        keep, drop = sorted((first, second), key=lambda f: _id_num(f["fault_id"]))
        log["merged"].append({**entry, "keep": keep["fault_id"], "drop": drop["fault_id"], "dropped_row": json.loads(json.dumps(drop))})
        _merge_faults(record, keep, drop)
        gone[drop["fault_id"]] = keep["fault_id"]

    by_id = {f["fault_id"]: f for f in record["faults"]}
    for c in candidates:
        a, b = by_id.get(c["a"]), by_id.get(c["b"])
        if a and b and not _linked(a, b) and c["similarity"] >= SIMILAR_FAULTS_FLAG_SIM and not c["numbers_or_sides_differ"]:
            log["left_distinct"].append([c["a"], c["b"]])
    return log


# ---------------------------------------------------------------------------
# 5. Actions: duplicates and repeats
# ---------------------------------------------------------------------------

def apply_consolidation(record: dict, proposal: Any, max_gap_days: int = CONSOLIDATE_MERGE_MAX_DAYS) -> dict:
    """Apply a (possibly empty or malformed) proposal. Returns the log entry."""
    log = {"merged": [], "rejected_merges": [], "repeat_groups": [], "notes": None}
    if not isinstance(proposal, dict):
        proposal = {}
    by_id = {a["action_id"]: a for a in record["action_logs"]}

    dropped: set[str] = set()
    forced_groups: list[list[str]] = []
    for m in proposal.get("merge") or []:
        if not isinstance(m, dict):
            continue
        keep_id, drop_id = str(m.get("keep", "")), str(m.get("drop", ""))
        keep, drop = by_id.get(keep_id), by_id.get(drop_id)
        why = None
        if not keep or not drop:
            why = "unknown id"
        elif keep_id == drop_id:
            why = "same row"
        elif keep["fault_id"] != drop["fault_id"]:
            why = "different fault_id"
        elif keep_id in dropped or drop_id in dropped:
            why = "already merged"
        elif keep["kind"] != drop["kind"]:
            why = "different kind"
        elif not signatures_agree(f"{keep.get('target') or ''} {keep['action']}", f"{drop.get('target') or ''} {drop['action']}"):
            why = "numbers or sides differ (not the same target)"
        blocked = None if why else merge_blocked_by_dates(keep, drop, max_gap_days)
        if blocked:
            # Same action, separate occasions: keep both rows and number them.
            log["rejected_merges"].append({"keep": keep_id, "drop": drop_id, "why": blocked, "kept_as": "repeat_group"})
            forced_groups.append([keep_id, drop_id])
            continue
        if why:
            log["rejected_merges"].append({"keep": keep_id, "drop": drop_id, "why": why})
            continue
        _merge_pair(keep, drop)
        dropped.add(drop_id)
        log["merged"].append({"keep": keep_id, "drop": drop_id, "reason": m.get("reason"), "dropped_row": drop})
    if dropped:
        record["action_logs"] = [a for a in record["action_logs"] if a["action_id"] not in dropped]
        by_id = {a["action_id"]: a for a in record["action_logs"]}

    for a in record["action_logs"]:
        a["occurrence_no"] = 1
    groups = list(proposal.get("repeat_groups") or []) + [
        {"action_ids": ids, "reason": "merge refused by date guard"} for ids in forced_groups
    ]
    for g in groups:
        if not isinstance(g, dict):
            continue
        ids = [str(i) for i in (g.get("action_ids") or []) if str(i) in by_id]
        if len(ids) < 2:
            continue
        faults = {by_id[i]["fault_id"] for i in ids}
        if len(faults) != 1:
            log["repeat_groups"].append({"action_ids": ids, "applied": False, "why": "different faults"})
            continue
        ordered = sorted(ids, key=lambda i: _order_key(by_id[i]))
        for n, i in enumerate(ordered, start=1):
            by_id[i]["occurrence_no"] = n
        log["repeat_groups"].append({"action_ids": ordered, "applied": True, "reason": g.get("reason")})

    assign_seq(record)
    log["notes"] = proposal.get("notes") if isinstance(proposal.get("notes"), str) else None
    return log


async def consolidate(llm: Optional[LLMClient], system_prompt: str, record: dict, tracker: Optional[TokenTracker]) -> dict:
    """Run the whole consolidate step and apply it. Returns the log entry."""
    fanout = collapse_fanout(record)
    candidates = fault_pair_candidates(record)

    proposal: Any = {}
    error = None
    call = llm is not None and (len(record["action_logs"]) >= 2 or bool(candidates))
    if call:
        prompt = (
            "## RECORD\n"
            + json.dumps(compact_for_prompt(record), ensure_ascii=False, indent=1)
            + "\n\n## FAULT PAIRS TO JUDGE\n"
            + (json.dumps(candidates, ensure_ascii=False, indent=1) if candidates else "[]")
            + "\n\n## RESPOND\nReturn the JSON proposal."
        )
        try:
            proposal = await json_call_with_budget(llm, prompt, system_prompt, tracker, "consolidate")
        except LLMError as e:
            error = str(e)
            logger.warning("consolidate call failed, applying deterministic steps only: %s", e)
    if not isinstance(proposal, dict):
        proposal = {}

    faults_log = apply_fault_verdicts(record, proposal.get("fault_pairs"), candidates)
    log = apply_consolidation(record, proposal)
    log["fanout_collapsed"] = fanout
    log["fault_pairs"] = {"candidates": candidates, **faults_log}
    log["called"] = call
    log["error"] = error
    return log
