"""
Small, dependency-free text similarity for the consolidate step and the
validator — never for writing values into a record.

Two rules learned from the first 10-casefile review:

- Token similarity is blind to numbers: "Knocking sound from M/E Cylinder No.3"
  and "...No.6" score 0.97. So every merge decision goes through
  signatures_agree(): the numbers and side words in both texts must not
  conflict. similarity() deliberately ignores numbers; the signature carries them.
- Abbreviations vary by writer ("M/E", "main-engine", "unit 5", "cyl 5"). They
  are folded here for COMPARISON only. The record keeps the documents' wording;
  canonical names are the resolver's job (02 §3.9).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_FOLD = [
    (re.compile(r"\bm\s*/\s*e\b|\bmain[- ]engine\b"), "mainengine"),
    (re.compile(r"\ba\s*/\s*e\b|\baux(?:iliary)?[- ]engine\b|\bd\s*/\s*g\b|\bdg(?=\d|\b)|\bgenerator\b"), "auxengine"),
    (re.compile(r"\bcyl(?:inder)?\b\.?|\bunit\b"), "cylinder"),
    (re.compile(r"\bno\b\.?\s*(?=\d)|#"), " "),
    (re.compile(r"\bstbd\b|\bstarboard\b|\bright\b"), "starboard"),
    (re.compile(r"\bleft\b"), "port"),
    (re.compile(r"\bfwd\b"), "forward"),
]
_STOP = {"the", "a", "an", "of", "on", "in", "at", "to", "for", "from", "and", "or", "with", "was", "were", "is", "are", "be", "been", "by", "its", "it", "that", "this"}
_SIDES = {"port", "starboard", "forward", "aft", "upper", "lower", "inboard", "outboard"}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def fold(text: str | None) -> str:
    """Lower-case, NFKC, fold the common marine abbreviations. For comparison only."""
    t = unicodedata.normalize("NFKC", text or "").lower()
    for rx, rep in _FOLD:
        t = rx.sub(rep, t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str | None) -> set[str]:
    """Word tokens without numbers and stop words."""
    return {w for w in _WORD.findall(fold(text)) if w not in _STOP and len(w) > 1}


def numbers(text: str | None) -> frozenset[str]:
    return frozenset(n.lstrip("0") or "0" for n in _NUM.findall(fold(text)))


def sides(text: str | None) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(fold(text)) if w in _SIDES)


def leading_phrase(text: str | None, n: int = 4) -> str:
    """The first n words, folded: the verb phrase of an action line."""
    return " ".join([w for w in _WORD.findall(fold(text)) if len(w) > 1][:n])


def similarity(a: str | None, b: str | None) -> float:
    """0..1. Half token-set Jaccard, half character ratio, numbers ignored."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    sa = " ".join(sorted(ta))
    sb = " ".join(sorted(tb))
    return round(0.5 * jac + 0.5 * SequenceMatcher(None, sa, sb).ratio(), 3)


def signatures_agree(a: str | None, b: str | None) -> bool:
    """
    False when the two texts name different numbers (cylinder 3 vs 6, tank 1 vs
    2) or different sides (port vs starboard). A text with no number / side
    does not conflict with one that has it.
    """
    na, nb = numbers(a), numbers(b)
    if na and nb and na != nb:
        return False
    sa, sb = sides(a), sides(b)
    if sa and sb and sa != sb:
        return False
    return True
