"""
Configuration.

Every environment variable in this repo is read HERE and nowhere else
(docs/03-repo-guide.md §3). Modules import named constants; nobody calls
`os.environ` directly.

Local development reads `.env` at the repo root. In k8s the same names arrive
from the ConfigMap (non-secret) and Secret (credentials).

Only the sections something actually uses exist yet. Add a section when the
first consumer of it lands, not before.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw:
        try:
            return int(raw)
        except ValueError:
            print(f"[config] invalid int for {key}: {raw!r}, using {default}")
    return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw:
        try:
            return float(raw)
        except ValueError:
            print(f"[config] invalid float for {key}: {raw!r}, using {default}")
    return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# =============================================================================
# Environment
# =============================================================================
ENV = _env("APP_ENV", "dev")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
PROJECT_ROOT = Path(__file__).parent.parent.parent

# =============================================================================
# LLM
# LLM_API_KEY is deliberately NOT required at import: unit tests must run with
# no .env at all (repo guide §8). LLMClient raises when it is constructed
# without a key instead.
# =============================================================================
LLM_PROVIDER = _env("LLM_PROVIDER", "openai")  # openai | azure
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 8192)
# Low by default: extraction wants the same facts from the same mails on a
# re-run. Reasoning models (gpt-5, o-series) ignore this — they reject the param.
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.1)

LLM_AZURE_ENDPOINT = _env("LLM_AZURE_ENDPOINT", "")
LLM_AZURE_API_VERSION = _env("LLM_AZURE_API_VERSION", "2025-04-01-preview")

# =============================================================================
# Langfuse (optional). Tracing is on only when LANGFUSE_ENABLED is true AND all
# three of key/secret/host are set AND the `langfuse` package is installed.
# Any of those missing => silently off. See src/utils/observability.py.
# =============================================================================
LANGFUSE_ENABLED = _env_bool("LANGFUSE_ENABLED", False)
LANGFUSE_PUBLIC_KEY = _env("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = _env("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = _env("LANGFUSE_HOST", "")


def get_llm_config() -> dict:
    """Provider settings for LLMClient."""
    config = {
        "provider": LLM_PROVIDER,
        "api_key": LLM_API_KEY,
        "model": LLM_MODEL,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
    }
    if LLM_PROVIDER == "azure":
        config["azure_endpoint"] = LLM_AZURE_ENDPOINT
        config["azure_api_version"] = LLM_AZURE_API_VERSION
    else:
        config["base_url"] = LLM_BASE_URL
    return config


# =============================================================================
# Extraction run (docs/06 §4, 08 §1, 10). Everything the windowed v2 build reads.
# =============================================================================
# Where the casefile export JSON lives (the 40-casefile export). The CLI's
# --export overrides it. Not committed: it is company mail.
CASEFILE_EXPORT_PATH = _env("CASEFILE_EXPORT_PATH", "")

# Local run store: mail-body cache, attachment digests, per-casefile records.
# Everything in here is rebuildable from the export + the network (03 §4).
EXTRACTION_DATA_DIR = _env("EXTRACTION_DATA_DIR", "data")

# Mails per LLM call. 5 is a hard requirement of the run (10 §1.1): a window is
# never larger than this. Overlap 0: every window is 5 NEW mails. Reply context
# comes from the id-keyed summary of the record so far that every window
# prompt carries (the model updates rows by id or mints the next free id), not
# from re-sending a mail. Set 1 to restore the one-mail overlap of 02 §2.2.
EXTRACTION_WINDOW_SIZE = _env_int("EXTRACTION_WINDOW_SIZE", 5)
EXTRACTION_WINDOW_OVERLAP = _env_int("EXTRACTION_WINDOW_OVERLAP", 0)

# Body text passed per mail. Long mails are almost always quoted history; the
# text is cut (not summarised) and the cut is marked so the model knows.
EXTRACTION_MAIL_MAX_CHARS = _env_int("EXTRACTION_MAIL_MAX_CHARS", 12000)
# Strip quoted earlier mails ("-----Original Message-----", "On ... wrote:")
# from a body. The validator checks quotes against the same stripped text.
EXTRACTION_STRIP_QUOTES = _env_bool("EXTRACTION_STRIP_QUOTES", True)

# Cap on action_logs per casefile (02 §2.2 rule 5). Exceeding it flags
# over_cap + needs_review; nothing is truncated.
EXTRACTION_MAX_ACTIONS = _env_int("EXTRACTION_MAX_ACTIONS", 25)
# Consolidate may merge two action rows only as duplicates of ONE event. A
# proposed merge is refused when both rows state a different date_performed,
# or (when either has none) their mails are more than this many days apart;
# the pair is kept as a repeat group instead. -1 disables the report-gap test.
CONSOLIDATE_MERGE_MAX_DAYS = _env_int("CONSOLIDATE_MERGE_MAX_DAYS", 3)
# Two faults on one equipment whose symptoms are at least this similar (numbers
# ignored) are put to the consolidate model as a pair to judge: same |
# recurrence | distinct. Lower = more pairs judged; the merge itself is still
# guarded by equipment, numbers/sides and dates in code.
CONSOLIDATE_FAULT_PAIR_MIN_SIM = _env_float("CONSOLIDATE_FAULT_PAIR_MIN_SIM", 0.6)

# Provider errors that are worth retrying (rate limit, timeout, 5xx) get this
# many attempts with exponential backoff before the window is given up.
# Output budget for a window / consolidate call. Reasoning models (gpt-5/6,
# o-series) count their thinking against it, and a dense window of 5 mails
# with attachments can exceed LLM_MAX_TOKENS (8192): 3 of 10 casefiles paused
# on finish_reason=length in the first full run. On that error the call is
# retried with double the budget, up to the cap.
EXTRACTION_WINDOW_MAX_TOKENS = _env_int("EXTRACTION_WINDOW_MAX_TOKENS", 16384)
EXTRACTION_WINDOW_MAX_TOKENS_CAP = _env_int("EXTRACTION_WINDOW_MAX_TOKENS_CAP", 65536)
EXTRACTION_LLM_RETRIES = _env_int("EXTRACTION_LLM_RETRIES", 3)
EXTRACTION_LLM_RETRY_BASE_SECONDS = _env_float("EXTRACTION_LLM_RETRY_BASE_SECONDS", 2.0)

# Attachments (02 §2.3). Mode: off = mails only; list = fetch+sha+junk filter,
# names only, no digest call; digest = one digest call per content-bearing file.
EXTRACTION_ATTACHMENT_MODE = _env("EXTRACTION_ATTACHMENT_MODE", "digest")  # off | list | digest
EXTRACTION_ATTACHMENT_CONCURRENCY = _env_int("EXTRACTION_ATTACHMENT_CONCURRENCY", 4)
# Text handed to one digest call. Service reports fit; a 200-row spreadsheet
# is cut with a marker.
EXTRACTION_DIGEST_TEXT_MAX_CHARS = _env_int("EXTRACTION_DIGEST_TEXT_MAX_CHARS", 20000)
# Images: vision calls are the most expensive route (04 §4). Off => every image
# is `listed_only` (name and size recorded, not read).
EXTRACTION_VISION_ENABLED = _env_bool("EXTRACTION_VISION_ENABLED", False)
# Junk-filter thresholds (02 §2.3): signature/inline images are small.
ATTACHMENT_JUNK_MAX_BYTES = _env_int("ATTACHMENT_JUNK_MAX_BYTES", 15 * 1024)
ATTACHMENT_JUNK_MAX_PIXELS = _env_int("ATTACHMENT_JUNK_MAX_PIXELS", 200)
# An image named like an inline signature (image013.png) but larger than this
# is a pasted photo, not a logo (04 §2.1: the name rule alone dropped 2 real
# files on KING ONE). Kept as content-bearing above the threshold.
ATTACHMENT_INLINE_NAME_MAX_BYTES = _env_int("ATTACHMENT_INLINE_NAME_MAX_BYTES", 100 * 1024)
# A PDF page with fewer chars than this in its text layer is treated as scanned.
PDF_OCR_PAGE_THRESHOLD = _env_int("PDF_OCR_PAGE_THRESHOLD", 200)
# OCR of scanned pages (src/attachments/ocr.py). KING ONE: 19 of 30 PDFs,
# including every SOF and the service report, have no text layer. vision =
# the chat model transcribes rendered pages (one call per page); tesseract
# needs pytesseract + a binary; off = scanned pages stay listed_only.
PDF_OCR_BACKEND = _env("PDF_OCR_BACKEND", "vision")  # off | vision | tesseract
PDF_OCR_MAX_PAGES = _env_int("PDF_OCR_MAX_PAGES", 10)  # per file; the rest is noted, not read
PDF_OCR_DPI = _env_int("PDF_OCR_DPI", 150)
# Vision-capable model for OCR when LLM_MODEL is not; empty = LLM_MODEL.
EXTRACTION_OCR_MODEL = _env("EXTRACTION_OCR_MODEL", "")
# Non-English evidence quotes are stored as "original [EN: translation]".
# The window prompt asks for it; this fallback adds it (one batched call per
# window) when the model forgot. The validator matches on the original.
EXTRACTION_TRANSLATE_QUOTES = _env_bool("EXTRACTION_TRANSLATE_QUOTES", True)
# One sentence must not evidence both an action and its result (17 % of actions
# in the first 10-casefile run). The prompt asks for the two clauses; this
# fallback cuts the ones the model still copied whole (one batched call per
# window, both parts verified as verbatim substrings).
EXTRACTION_SPLIT_SHARED_QUOTES = _env_bool("EXTRACTION_SPLIT_SHARED_QUOTES", True)

# HTTP fetch of mail bodies / attachments (assets-prod.siya.com).
FETCH_TIMEOUT_SECONDS = _env_float("FETCH_TIMEOUT_SECONDS", 30.0)
FETCH_RETRIES = _env_int("FETCH_RETRIES", 3)
FETCH_MAX_BYTES = _env_int("FETCH_MAX_BYTES", 40 * 1024 * 1024)

# Prompt versions (03 §7): a prompt change is a new vN.md and a flip here.
# v1: one-mail overlap. v2: id-keyed record summary, no overlap. v3: v2 plus
# crew attributions are suspicions, logistics/operational events are not
# actions, non-English quotes carry an [EN: ...] translation. v4: a returning
# symptom is a new fault row (recurrence_of), updates must match the row's
# party and subject, post-repair actions attach to the repaired fault, the
# guarantee claim / PO and vessel consequences are kept, single-line quotes.
# v5: one event is one row (also_fault_ids, no per-fault copies); the action
# quote and the outcome quote are different clauses; one quote cites one
# document; fault.position always filled; a problem is not the symptom again;
# date_performed only for work done.
WINDOW_UPDATE_PROMPT_VERSION = _env("WINDOW_UPDATE_PROMPT_VERSION", "v7")
# v2: also judges look-alike fault pairs (same | recurrence | distinct).
CONSOLIDATE_PROMPT_VERSION = _env("CONSOLIDATE_PROMPT_VERSION", "v4")
ATTACHMENT_DIGEST_PROMPT_VERSION = _env("ATTACHMENT_DIGEST_PROMPT_VERSION", "v1")
OCR_TRANSCRIBE_PROMPT_VERSION = _env("OCR_TRANSCRIBE_PROMPT_VERSION", "v1")
TRANSLATE_QUOTES_PROMPT_VERSION = _env("TRANSLATE_QUOTES_PROMPT_VERSION", "v1")
SPLIT_QUOTE_PROMPT_VERSION = _env("SPLIT_QUOTE_PROMPT_VERSION", "v1")

# Stamped on every record (03 §6 rule 3). Schema major.minor + the three
# prompt versions, so "which code produced this?" has one answer.
SCHEMA_VERSION = "casefile_extract.v2"
EXTRACTION_VERSION = (
    f"{SCHEMA_VERSION}+wu.{WINDOW_UPDATE_PROMPT_VERSION}"
    f"+co.{CONSOLIDATE_PROMPT_VERSION}+ad.{ATTACHMENT_DIGEST_PROMPT_VERSION}"
)

PROMPTS_DIR = PROJECT_ROOT / "src" / "prompts"
SCHEMA_DIR = PROJECT_ROOT / "schema"


def get_prompt(name: str, version: str) -> str:
    """
    Read src/prompts/<name>/<version>.md. The one place prompt files are opened,
    so the version in use is always visible here (03 §7).
    """
    path = PROMPTS_DIR / name / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt file is empty: {path}")
    return text


# =============================================================================
# Entity resolution (02 §3.9, docs/13). Stage 1: the alias table.
# =============================================================================
# SQLite file holding standard_term + alias. A rebuildable cache: the human-edited
# truth is src/resolution/seed_aliases.csv plus whatever scripts/build_lookup.py
# exports next to it. Empty => <EXTRACTION_DATA_DIR>/resolution/lookup.sqlite.
RESOLUTION_DB_PATH = _env("RESOLUTION_DB_PATH", "") or str(
    Path(EXTRACTION_DATA_DIR) / "resolution" / "lookup.sqlite"
)
RESOLUTION_SEED_CSV = _env(
    "RESOLUTION_SEED_CSV", str(PROJECT_ROOT / "src" / "resolution" / "seed_aliases.csv")
)
# Shared resolver config tables (docs/13 §3, docs/16 P0): modifier_terms.csv (position / side
# words, left -> port, right -> starboard) and vessel_names.txt (the fleet list, generated by
# scripts/build_lookup.py from the records and the export). LookupStore(config_dir=...) reads them.
RESOLUTION_CONFIG_DIR = _env("RESOLUTION_CONFIG_DIR", str(PROJECT_ROOT / "config"))


# =============================================================================
# Casefile browser (docs/12). Hand-maintained, reviewed broader_than links between
# normalised symptom groups; a missing file means no links.
# =============================================================================
SYMPTOM_LINKS_PATH = _env("SYMPTOM_LINKS_PATH", str(PROJECT_ROOT / "config" / "symptom_links.json"))
# The bounded family layer above symptom standards (docs/13 §8) and the human-reviewed symptom
# aliases. Families come ONLY from this CSV; a facet lists family keys, never wording.
SYMPTOM_FAMILIES_CSV = _env(
    "SYMPTOM_FAMILIES_CSV", str(PROJECT_ROOT / "config" / "symptom_families.csv")
)
SEED_SYMPTOM_ALIASES_CSV = _env(
    "SEED_SYMPTOM_ALIASES_CSV", str(PROJECT_ROOT / "config" / "seed_symptom_aliases.csv")
)
