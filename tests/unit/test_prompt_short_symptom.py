"""
P3 (docs/16): the extractor asks for the failure mode in <=6 words. The prompts' field notes, the
schema description and the validator's soft flag agree, and the prompts still name no key the
schema does not have (no derived / tier-2 field is asked for).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src import config
from src.contract.validator import SOFT_FLAG_PREFIXES, SYMPTOM_MAX_WORDS

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / "schema" / "casefile_extract.v2.schema.json").read_text(encoding="utf-8")
)
SHORT = "≤6 words"


def _prompt(name: str, version: str) -> str:
    return config.get_prompt(name, version)


def _schema_keys() -> set[str]:
    keys: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "properties" and isinstance(v, dict):
                    keys.update(v)
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(SCHEMA)
    return keys


def test_current_prompts_ask_for_a_short_symptom_once_each():
    # the source defaults are v7 / v4 (test_default_config_is_five_new_mails_no_overlap); a local
    # .env may pin an older version for a cached run, so the files are named here, not read via config
    for name, version in (("window_update", "v7"), ("consolidate", "v4")):
        text = _prompt(name, version)
        assert text.count(SHORT) == 1, (name, version)
        line = next(ln for ln in text.splitlines() if SHORT in ln)
        assert "`position`" in line and "`provenance.evidence_quote`" in line
        # the three examples and no more (inside the "(≤6 words: ...)" clause)
        clause = line[line.index("(" + SHORT) : line.index(")", line.index("(" + SHORT)) + 1]
        assert re.findall(r"'([^']+)'", clause) == [
            "knocking sound",
            "water leak",
            "fails to start",
        ]
    # the earlier versions are untouched (cached runs keep their prompt)
    assert SHORT not in _prompt("window_update", "v5") and SHORT not in _prompt("consolidate", "v2")
    assert config.EXTRACTION_VERSION.startswith(
        config.SCHEMA_VERSION
    )  # the run stamp carries the versions


def _symptom_description() -> str:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            sym = (
                node.get("properties", {}).get("symptom")
                if isinstance(node.get("properties"), dict)
                else None
            )
            if isinstance(sym, dict) and "description" in sym:
                found.append(sym["description"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(SCHEMA)
    assert len(found) == 1, found
    return found[0]


def test_schema_description_matches_and_prompts_name_no_key_outside_the_schema():
    desc = _symptom_description()
    assert SHORT in desc and "position" in desc and "provenance.evidence_quote" in desc
    keys = _schema_keys()
    for name, version in (("window_update", "v6"), ("consolidate", "v3")):
        text = _prompt(name, version)
        # every dotted `a.b` path the prompt names must be a schema field (a derived or tier-2
        # field such as a count would show up here as an unknown key), and the fields the new
        # note names exist
        for ref in re.findall(r"`([a-z_]+(?:\[\])?\.[a-z_.]+)`", text):
            leaf = ref.split(".")[-1]
            assert leaf in keys, (name, ref)
        assert {"symptom", "position", "evidence_quote", "provenance"} <= keys
        for bad in ("count", "counts", "score", "confidence", "summary", "n_faults", "word_count"):
            assert f"`{bad}`" not in text, (name, bad)
            assert bad not in keys or bad == "summary", (
                bad
            )  # no such key exists in the schema either


def test_symptom_too_long_is_a_soft_flag():
    """The quality test itself lives in test_v5_quality.py; here only the contract."""
    assert "symptom_too_long" in SOFT_FLAG_PREFIXES and SYMPTOM_MAX_WORDS == 12
