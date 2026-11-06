"""
One sentence must not be the proof for both an action and its result.

In the first 10-casefile run 17 % of actions (33 / 193) had
outcome.evidence_quote identical to provenance.evidence_quote, because the
source sentence carries both halves:

    "We changed the exhaust valve sensor, and reset the CCU4 but issue still persist."

The window prompt now asks for the action clause and the result clause
separately. This module is the fallback for the rows where the model still
copied the whole sentence: one batched call per window asks for the cut, and
code accepts a cut only when BOTH parts are verbatim substrings of the
original sentence and differ from each other. A sentence that is only a result
("found no continuity") cannot be cut; it is left alone and the validator
reports it as `same_quote_action_outcome` (advisory).

Runs before translate_missing(), so the two new quotes get their own [EN: ...].
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from src.extraction.mail_text import normalise_for_match
from src.extraction.translate import strip_translation
from src.llm import LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)


def quotes_equivalent(a: Optional[str], b: Optional[str]) -> bool:
    """Same sentence: equal after normalisation, or one inside the other covering most of it."""
    na, nb = normalise_for_match(strip_translation(a) or ""), normalise_for_match(strip_translation(b) or "")
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    return short in long_ and len(short) >= 0.85 * len(long_)


def shared_quote_actions(record: dict) -> list[dict]:
    """Actions whose stated outcome is evidenced by the same sentence as the action itself."""
    out = []
    for a in record.get("action_logs", []):
        o = a.get("outcome") or {}
        if o.get("value") not in (None, "unknown") and quotes_equivalent(o.get("evidence_quote"), (a.get("provenance") or {}).get("evidence_quote")):
            out.append(a)
    return out


def _valid_part(part: Optional[str], sentence_norm: str) -> Optional[str]:
    if not isinstance(part, str) or not part.strip():
        return None
    p = part.strip()
    return p if normalise_for_match(p) in sentence_norm else None


async def split_shared_quotes(llm, system_prompt: str, record: dict, tracker: Optional[TokenTracker]) -> int:
    """Cut shared sentences into an action quote and a result quote. Returns rows changed. Never raises."""
    rows = shared_quote_actions(record)
    if not rows or llm is None:
        return 0
    items = []
    for a in rows:
        sentence = strip_translation(a["provenance"]["evidence_quote"])
        other = strip_translation(a["outcome"]["evidence_quote"])
        if len(other or "") > len(sentence or ""):
            sentence = other
        items.append({"id": a["action_id"], "action": a["action"], "outcome": a["outcome"]["value"], "sentence": sentence})
    prompt = "Cut each sentence. Return JSON {\"splits\": [{\"id\", \"action_part\", \"result_part\"}]}, one per item.\n\n" + json.dumps(
        {"items": items}, ensure_ascii=False, indent=1
    )
    try:
        resp = await llm.json_completion(prompt, system_message=system_prompt, token_tracker=tracker)
    except LLMError as e:
        logger.warning("quote split call failed: %s", e)
        return 0
    splits = resp.get("splits") if isinstance(resp, dict) else None
    if not isinstance(splits, list):
        return 0
    by_id = {a["action_id"]: a for a in rows}
    sentences = {i["id"]: normalise_for_match(i["sentence"]) for i in items}
    changed = 0
    for s in splits:
        if not isinstance(s, dict) or s.get("id") not in by_id:
            continue
        sent = sentences[s["id"]]
        action_part, result_part = _valid_part(s.get("action_part"), sent), _valid_part(s.get("result_part"), sent)
        if not action_part or not result_part:
            continue  # a sentence that is only the result cannot be cut
        if quotes_equivalent(action_part, result_part) or normalise_for_match(result_part) == sent:
            continue
        a = by_id[s["id"]]
        a["provenance"]["evidence_quote"] = action_part
        a["outcome"]["evidence_quote"] = result_part
        changed += 1
    return changed
