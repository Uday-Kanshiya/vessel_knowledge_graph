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
