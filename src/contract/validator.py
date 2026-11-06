"""
The deterministic validator (02 §2.4, 08 §1 step 4). No model calls.

    repair_citations(record, mail_texts, digests, casefile_digests) -> soft flags
    prune_shas(record, mail_texts, digests)                          -> log of rows changed
    validate_record(record, mail_texts, digests, max_actions)        -> flags

Hard flags (set needs_review), each `<kind>:<id or detail>`:
    schema_error          record is not valid casefile_extract.v2
    unresolved_equipment_id / unresolved_problem_id / unresolved_fault_id
    unresolved_recurrence_of  faults[].recurrence_of does not point at another fault
    quote_not_found       an evidence quote (row, supporting entry or outcome) is not
                          verbatim (normalised) in any source it cites; a trailing
                          '[EN: ...]' translation is stripped before matching
    sha_unknown           cited attachment sha has no digest
    sha_not_on_cited_mail cited sha has no link on any mail the row cites
    non_content_bearing_sha  the extractor cited a logo
    outcome_inconsistent  value != unknown without mail/quote (schema also rejects)
    outcome_before_action outcome_date earlier than date_performed
    problem_restates_symptom  a problem row that only repeats its fault's symptom
    over_cap              more than max_actions action rows
    duplicate_id          two rows share an id

Soft flags (recorded, do not set needs_review):
    symptom_too_long      a fault's symptom runs past SYMPTOM_MAX_WORDS words: the prompt asks
                          for the failure mode in <=6 words (window_update/v6, docs/13 §8); a
                          sentence here is the extractor's defect, not the record's
    quote_in_uncited_attachment  the quote was found in an attachment of the casefile
                          the row had not cited; that sha was added (repair_citations)
    same_quote_action_outcome  one sentence still evidences both an action and its
                          result after the split fallback (it is only a result clause)
    similar_faults        two faults on one equipment read alike and were judged
                          distinct at consolidate (added by the runner)

Flagged records still load; excluding them would bias the corpus toward easy cases.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache

from src.config import SCHEMA_DIR
from src.extraction.mail_text import quote_found
from src.extraction.quote_split import quotes_equivalent
from src.extraction.textsim import signatures_agree, similarity
from src.extraction.translate import strip_translation

SOFT_FLAG_PREFIXES = (
    "quote_in_uncited_attachment",
    "same_quote_action_outcome",
    "similar_faults",
    "symptom_too_long",
)
SYMPTOM_MAX_WORDS = 12  # a quality warning, not a failure: the prompt asks for <=6
PROBLEM_RESTATES_MIN_SIM = 0.85

_COLLS = (("equipment", "equipment_id"), ("problems", "problem_id"), ("faults", "fault_id"), ("action_logs", "action_id"))


@lru_cache(maxsize=1)
def _schema_validator():
    import jsonschema

    with (SCHEMA_DIR / "casefile_extract.v2.schema.json").open(encoding="utf-8") as f:
        schema = json.load(f)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=jsonschema.FormatChecker())


def schema_errors(record: dict) -> list[str]:
    errors = []
    for err in sorted(_schema_validator().iter_errors(record), key=lambda e: list(e.absolute_path)):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message[:160]}")
    return errors


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _digest_text(d: dict | None) -> list[str]:
    """What the model could read of an attachment: its text, else its summary (listed_only / vision)."""
    if not d:
        return []
    out = []
    if d.get("extracted_text"):
        out.append(d["extracted_text"])
    if d.get("summary"):
        out.append(d["summary"])
    return out


def _sources_for(entry: dict, mail_texts: Mapping[str, str], digests: Mapping[str, dict]) -> list[str]:
    """Texts cited by a provenance block or a supporting entry (same shape)."""
    texts = [mail_texts[m] for m in entry.get("mail_ids") or [] if mail_texts.get(m)]
    for sha in entry.get("attachment_shas") or []:
        texts.extend(_digest_text(digests.get(sha)))
    return texts


def _all_shas(prov: dict) -> list[str]:
    out = list(prov.get("attachment_shas") or [])
    for sup in prov.get("supporting") or []:
        out.extend(s for s in sup.get("attachment_shas") or [] if s not in out)
    return out


all_cited_shas = _all_shas  # public: every sha a row cites, primary and supporting


def _outcome_sources(row: dict, mail_texts: Mapping[str, str], digests: Mapping[str, dict]) -> list[str]:
    """The outcome mail's body plus every document the row cites (the result may be stated in a report)."""
    out = row.get("outcome") or {}
    src = mail_texts.get(out.get("outcome_in_mail") or "")
    texts = [src] if src else []
    for sha in _all_shas(row.get("provenance") or {}):
        texts.extend(_digest_text(digests.get(sha)))
    return texts


def _found(quote: str | None, sources: Iterable[str]) -> bool:
    q = strip_translation(quote)  # "original [EN: ...]": the original is what must be verbatim
    return bool(q) and any(quote_found(q, s) for s in sources)


def _check_quote(flag_id: str, quote: str | None, sources: list[str], flags: list[str]) -> None:
    if strip_translation(quote) and not _found(quote, sources):
        flags.append(f"quote_not_found:{flag_id}")


def _check_shas(flag_id: str, entry: dict, digests: Mapping[str, dict], flags: list[str], row_mail_ids: set | None = None) -> None:
    mail_ids = set(entry.get("mail_ids") or []) | (row_mail_ids or set())
    for sha in entry.get("attachment_shas") or []:
        d = digests.get(sha)
        if d is None:
            flags.append(f"sha_unknown:{flag_id}:{sha[:8]}")
            continue
        if not d.get("content_bearing", True):
            flags.append(f"non_content_bearing_sha:{flag_id}:{sha[:8]}")
        linked = {ln.get("mailbodyId") for ln in d.get("links") or []}
        if mail_ids and not (linked & mail_ids):
            flags.append(f"sha_not_on_cited_mail:{flag_id}:{sha[:8]}")


def _check_provenance(flag_id: str, prov: dict, mail_texts: Mapping[str, str], digests: Mapping[str, dict], flags: list[str]) -> None:
    _check_quote(flag_id, prov.get("evidence_quote"), _sources_for(prov, mail_texts, digests), flags)
    _check_shas(flag_id, prov, digests, flags)
    row_mails = set(prov.get("mail_ids") or [])
    for n, sup in enumerate(prov.get("supporting") or [], start=1):
        _check_quote(f"{flag_id}.supporting{n}", sup.get("evidence_quote"), _sources_for(sup, mail_texts, digests), flags)
        _check_shas(f"{flag_id}.supporting{n}", sup, digests, flags, row_mails)


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def validate_record(
    record: dict,
    mail_texts: Mapping[str, str],
    digests: Mapping[str, dict],
    max_actions: int = 25,
) -> list[str]:
    flags: list[str] = []

    for e in schema_errors(record):
        flags.append(f"schema_error:{e}")

    eq_ids = [e.get("equipment_id") for e in record.get("equipment", [])]
    pr_ids = [p.get("problem_id") for p in record.get("problems", [])]
    fa_ids = [f.get("fault_id") for f in record.get("faults", [])]
    ac_ids = [a.get("action_id") for a in record.get("action_logs", [])]
    for ids in (eq_ids, pr_ids, fa_ids, ac_ids):
        seen = set()
        for i in ids:
            if i in seen:
                flags.append(f"duplicate_id:{i}")
            seen.add(i)
    eq_set, pr_set, fa_set = set(eq_ids), set(pr_ids), set(fa_ids)
    problems = {p.get("problem_id"): p for p in record.get("problems", [])}

    for e in record.get("equipment", []):
        _check_provenance(e.get("equipment_id", "e?"), e.get("provenance") or {}, mail_texts, digests, flags)

    for p in record.get("problems", []):
        _check_provenance(p.get("problem_id", "p?"), p.get("provenance") or {}, mail_texts, digests, flags)

    for f in record.get("faults", []):
        fid = f.get("fault_id", "f?")
        if f.get("equipment_id") not in eq_set:
            flags.append(f"unresolved_equipment_id:{fid}")
        if f.get("problem_id") is not None and f.get("problem_id") not in pr_set:
            flags.append(f"unresolved_problem_id:{fid}")
        if f.get("recurrence_of") is not None and (f["recurrence_of"] == fid or f["recurrence_of"] not in fa_set):
            flags.append(f"unresolved_recurrence_of:{fid}")
        # A problem is a cause. One that only repeats the symptom adds a node and no knowledge.
        p = problems.get(f.get("problem_id"))
        if p and similarity(p.get("statement"), f.get("symptom")) >= PROBLEM_RESTATES_MIN_SIM and signatures_agree(p.get("statement"), f.get("symptom")):
            flags.append(f"problem_restates_symptom:{p['problem_id']}:{fid}")
        if len((f.get("symptom") or "").split()) > SYMPTOM_MAX_WORDS:
            flags.append(f"symptom_too_long:{fid}")
        _check_provenance(fid, f.get("provenance") or {}, mail_texts, digests, flags)

    for a in record.get("action_logs", []):
        aid = a.get("action_id", "a?")
        if a.get("fault_id") not in fa_set or any(x not in fa_set or x == a.get("fault_id") for x in a.get("also_fault_ids") or []):
            flags.append(f"unresolved_fault_id:{aid}")
        prov = a.get("provenance") or {}
        _check_provenance(aid, prov, mail_texts, digests, flags)
        out = a.get("outcome") or {}
        if out.get("value") != "unknown":
            if not out.get("outcome_in_mail") or not out.get("evidence_quote"):
                flags.append(f"outcome_inconsistent:{aid}")
            else:
                _check_quote(f"{aid}.outcome", out.get("evidence_quote"), _outcome_sources(a, mail_texts, digests), flags)
                if quotes_equivalent(out.get("evidence_quote"), prov.get("evidence_quote")):
                    flags.append(f"same_quote_action_outcome:{aid}")
            if out.get("outcome_date") and a.get("date_performed") and out["outcome_date"] < a["date_performed"]:
                flags.append(f"outcome_before_action:{aid}")
        elif out.get("verification") not in (None, "none") or out.get("outcome_in_mail") or out.get("evidence_quote"):
            flags.append(f"outcome_inconsistent:{aid}")

    if len(ac_ids) > max_actions:
        flags.append(f"over_cap:{len(ac_ids)}>{max_actions}")

    return flags


# ---------------------------------------------------------------------------
# Repairs made before validation
# ---------------------------------------------------------------------------

def _find_in_other_attachment(quote: str, entry: dict, casefile_digests: Mapping[str, dict], also_cited: Iterable[str] = ()) -> dict | None:
    """A digest of this casefile, not already cited, whose text holds the quote. Cited mails' attachments first."""
    cited_mails = set(entry.get("mail_ids") or [])
    cited_shas = set(entry.get("attachment_shas") or []) | set(also_cited)
    candidates = [d for sha, d in casefile_digests.items() if sha not in cited_shas and d.get("content_bearing") and d.get("extracted_text")]
    candidates.sort(key=lambda d: 0 if any(ln.get("mailbodyId") in cited_mails for ln in d.get("links") or []) else 1)
    for d in candidates:
        if quote_found(quote, d["extracted_text"]):
            return d
    return None


def repair_citations(record: dict, mail_texts: Mapping[str, str], digests: dict, casefile_digests: Mapping[str, dict]) -> list[str]:
    """
    A quote that is verbatim in an attachment of this casefile the row did not
    cite (the model quoted the scanned copy and cited the text copy) is a
    citation slip, not a fabrication. Add that sha — and a mail that carries
    it, if none of the cited mails does — and return a soft flag per repair.
    An outcome quote repaired this way gets its own supporting entry, so the
    row's primary quote keeps only the documents that state it.
    Mutates `record` and `digests`.
    """
    cid = record.get("casefile_id")
    soft: list[str] = []

    def linked_mail(d: dict) -> str | None:
        linked = [ln.get("mailbodyId") for ln in d.get("links") or [] if ln.get("casefileId") == cid]
        return linked[0] if linked else None

    def fix_entry(flag_id: str, entry: dict, row_prov: dict) -> None:
        """entry: a provenance block or a supporting entry (same shape)."""
        q = strip_translation(entry.get("evidence_quote"))
        if not q or _found(q, _sources_for(entry, mail_texts, digests)):
            return
        d = _find_in_other_attachment(q, entry, casefile_digests)
        if d is None:
            return
        sha = d["sha256"]
        entry.setdefault("attachment_shas", []).append(sha)
        digests[sha] = d
        mail = linked_mail(d)
        all_mails = set(entry.get("mail_ids") or []) | set(row_prov.get("mail_ids") or [])
        if mail and not ({ln.get("mailbodyId") for ln in d.get("links") or []} & all_mails):
            entry.setdefault("mail_ids", []).append(mail)
            if mail not in row_prov.setdefault("mail_ids", []):
                row_prov["mail_ids"].append(mail)
        soft.append(f"quote_in_uncited_attachment:{flag_id}:{sha[:8]}")

    for coll, id_field in _COLLS:
        for row in record.get(coll, []):
            rid = row.get(id_field, "?")
            prov = row.get("provenance") or {}
            fix_entry(rid, prov, prov)
            for n, sup in enumerate(prov.get("supporting") or [], start=1):
                fix_entry(f"{rid}.supporting{n}", sup, prov)
            if coll != "action_logs":
                continue
            out = row.get("outcome") or {}
            q = strip_translation(out.get("evidence_quote"))
            if out.get("value") == "unknown" or not q or _found(q, _outcome_sources(row, mail_texts, digests)):
                continue
            d = _find_in_other_attachment(q, {"mail_ids": [out.get("outcome_in_mail")]}, casefile_digests, _all_shas(prov))
            if d is None:
                continue
            sha = d["sha256"]
            digests[sha] = d
            mail = out.get("outcome_in_mail")
            if mail not in {ln.get("mailbodyId") for ln in d.get("links") or []}:
                mail = linked_mail(d) or mail
            prov.setdefault("supporting", []).append(
                {"mail_ids": [mail], "attachment_shas": [sha], "evidence_quote": out["evidence_quote"], "fields": ["outcome"]}
            )
            if mail and mail not in prov.setdefault("mail_ids", []):
                prov["mail_ids"].append(mail)
            soft.append(f"quote_in_uncited_attachment:{rid}.outcome:{sha[:8]}")
    return soft


def prune_shas(record: dict, mail_texts: Mapping[str, str], digests: Mapping[str, dict]) -> list[dict]:
    """
    One quote, the documents that state it. When a provenance block (or a
    supporting entry) cites several attachments and the quote is verbatim in
    some of them, the others are not evidence for that quote and are removed.
    Nothing is pruned when no cited attachment holds the quote (it is in the
    mail body, or unverifiable) — that case is the validator's to report.
    Returns [{row, kept, dropped}] for the run log.
    """
    log: list[dict] = []

    def prune(flag_id: str, entry: dict) -> None:
        shas = entry.get("attachment_shas") or []
        q = strip_translation(entry.get("evidence_quote"))
        if len(shas) < 2 or not q:
            return
        holds = [s for s in shas if any(quote_found(q, t) for t in _digest_text(digests.get(s)))]
        if holds and len(holds) < len(shas):
            log.append({"row": flag_id, "kept": [s[:8] for s in holds], "dropped": [s[:8] for s in shas if s not in holds]})
            entry["attachment_shas"] = holds

    for coll, id_field in _COLLS:
        for row in record.get(coll, []):
            prov = row.get("provenance") or {}
            prune(row.get(id_field, "?"), prov)
            for n, sup in enumerate(prov.get("supporting") or [], start=1):
                prune(f"{row.get(id_field, '?')}.supporting{n}", sup)
    return log


def apply_validation(record: dict, flags: list[str]) -> dict:
    record["validation_flags"] = flags
    record["needs_review"] = any(not f.startswith(SOFT_FLAG_PREFIXES) for f in flags)
    return record
