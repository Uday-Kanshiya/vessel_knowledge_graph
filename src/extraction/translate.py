"""
Evidence quotes in a language other than English are stored as

    <verbatim original> [EN: <English translation>]

The original stays verbatim, so the validator can still find it in the source
(strip_translation() removes the bracket before matching). The window prompt
asks the model to write quotes this way; translate_missing() is the fallback
that adds the bracket, with one batched model call per window, to any quote
the model left untranslated. No model call happens when every quote is
English or already carries its bracket.

"Not English" is decided by script: any alphabetic character outside the Latin
ranges (CJK, Cyrillic, Arabic, Greek, Thai, ...). Accented Latin text (Turkish,
Vietnamese) is treated as Latin and left alone.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional

from src.llm import LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

# Trailing "[EN: ...]" in a few spellings the model may use.
_TRANSLATION_RX = re.compile(r"\s*[\[(]\s*(?:EN|En|en|ENG|Eng|English|english|Translation|translation)\s*[:：]\s*(.*?)\s*[\])]\s*$", re.S)
_LATIN_MAX = 0x024F  # end of Latin Extended-B


def needs_translation(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(ch.isalpha() and ord(ch) > _LATIN_MAX for ch in text)


def split_quote(quote: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(original, translation or None)."""
    if not quote:
        return quote, None
    m = _TRANSLATION_RX.search(quote)
    if not m:
        return quote, None
    return quote[: m.start()].rstrip(), m.group(1).strip() or None


def strip_translation(quote: Optional[str]) -> Optional[str]:
    return split_quote(quote)[0]


def with_translation(original: str, translation: Optional[str]) -> str:
    original = original.strip()
    if not translation:
        return original
    return f"{original} [EN: {translation.strip()}]"


def normalise_quote(quote: Optional[str]) -> Optional[str]:
    """Canonical form: original, plus ' [EN: ...]' only when the original is not English."""
    if not quote:
        return quote
    original, translation = split_quote(quote)
    if translation and not needs_translation(original):
        return original  # an English quote needs no bracket
    return with_translation(original, translation)


def _quote_slots(record: dict) -> Iterable[tuple[dict, str]]:
    """(container dict, key) for every evidence quote field in the record."""
    for coll in ("equipment", "problems", "faults", "action_logs"):
        for row in record.get(coll, []):
            prov = row.get("provenance") or {}
            if "evidence_quote" in prov:
                yield prov, "evidence_quote"
            for sup in prov.get("supporting") or []:
                if sup.get("evidence_quote"):
                    yield sup, "evidence_quote"
            for uq in prov.get("update_quotes") or []:  # records built before provenance.supporting
                yield uq, "quote"
            if coll == "action_logs":
                out = row.get("outcome") or {}
                if out.get("evidence_quote"):
                    yield out, "evidence_quote"


def quotes_missing_translation(record: dict) -> list[tuple[dict, str, str]]:
    """(container, key, original) for non-English quotes without a bracket."""
    out = []
    for container, key in _quote_slots(record):
        original, translation = split_quote(container.get(key))
        if original and translation is None and needs_translation(original):
            out.append((container, key, original))
    return out


async def translate_missing(llm, system_prompt: str, record: dict, tracker: Optional[TokenTracker]) -> int:
    """
    Add ' [EN: ...]' to every non-English quote in the record that lacks one.
    One model call for the whole batch; returns the number of quotes translated.
    Never raises: on failure the quotes stay as they are and the next window
    (or --validate-only) is not affected.
    """
    missing = quotes_missing_translation(record)
    if not missing or llm is None:
        return 0
    originals = list(dict.fromkeys(o for _, _, o in missing))  # unique, ordered
    prompt = "Translate each quote to English. Return JSON {\"translations\": [...]} with one string per input, same order.\n\n" + json.dumps(
        {"quotes": originals}, ensure_ascii=False, indent=1
    )
    try:
        resp = await llm.json_completion(prompt, system_message=system_prompt, token_tracker=tracker)
    except LLMError as e:
        logger.warning("quote translation call failed: %s", e)
        return 0
    translations = resp.get("translations") if isinstance(resp, dict) else None
    if not isinstance(translations, list) or len(translations) != len(originals):
        logger.warning("quote translation returned %s items for %d quotes", None if translations is None else len(translations), len(originals))
        return 0
    lookup = {o: (t.strip() if isinstance(t, str) else "") for o, t in zip(originals, translations)}
    n = 0
    for container, key, original in missing:
        t = lookup.get(original)
        if t:
            container[key] = with_translation(original, t)
            n += 1
    return n
