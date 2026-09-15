"""
Langfuse observability — optional, and the single owner of everything Langfuse.

Contract (do not break):
- Observability must NEVER affect the pipeline. Every function here degrades to
  a safe no-op when Langfuse is disabled, keys/host are missing, the `langfuse`
  package is absent, or any Langfuse call raises.
- `langfuse` is imported lazily, so the app boots without the package installed
  (it is an optional extra in pyproject.toml, not a core dependency).

Public API:
    obs_enabled() -> bool             is tracing effectively on?
    openai_client_classes()           (AsyncOpenAI, AsyncAzureOpenAI) — Langfuse-wrapped when on
    generation_kwargs(tracker)        extra create() kwargs naming the generation, {} when off
    trace_root(name, **kw)            context manager: root span (one casefile, one query)
    span(name, **kw)                  context manager: child span
    generation(name, **kw)            context manager: a non-OpenAI LLM call (Gemini, Mistral OCR)
    update_current_trace(**kw)        set session_id / metadata / tags
    flush()                           drain the batcher — call before a short-lived process exits

HOW TO REMOVE LANGFUSE ENTIRELY
    1. Delete this file.
    2. In src/llm/llm_client.py, on the two lines marked `# observability:`:
         - replace the import with   from openai import AsyncAzureOpenAI, AsyncOpenAI
           and delete the `openai_client_classes()` assignment line
         - delete the `params.update(generation_kwargs(...))` line
    3. Delete the LANGFUSE_* block from src/config/__init__.py and .env.example.
    4. Delete the `observability` extra from pyproject.toml.
    5. Delete the paragraph in llm_client.py's module docstring that points here.
    Nothing else imports it; `grep -rn -i 'observability\|langfuse' src/` is then empty.

Ported from qhsc_mail_process/src/utils/observability.py, which runs in prod.
"""

import logging
import sys
import threading
import time
from contextlib import contextmanager

from src.config import (
    LANGFUSE_ENABLED,
    LANGFUSE_HOST,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
)

logger = logging.getLogger(__name__)

_client = None
_init_done = False
_enabled = False
_openai_wrapped = False

# A dead/slow Langfuse host must never hang process shutdown.
_FLUSH_TIMEOUT_SECONDS = 5

# When the Langfuse host is unreachable, the OTLP exporter logs a full traceback
# per failed batch from a background thread and floods pod logs. Keep a
# breadcrumb, drop the traceback, at most one line per throttle window.
_NOISY_OTEL_LOGGERS = (
    "opentelemetry.sdk._shared_internal",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.sdk.trace.export",
)
_OTEL_LOG_THROTTLE_SECONDS = 300


class _OtelExportLogThrottle(logging.Filter):
    """Collapse exporter failure spam into one compact line per window."""

    def __init__(self, throttle_seconds: int = _OTEL_LOG_THROTTLE_SECONDS):
        super().__init__()
        self._throttle = throttle_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._last and (now - self._last) < self._throttle:
                return False
            self._last = now
        record.msg = (
            "Langfuse trace export is failing (host slow/unreachable) — traces may "
            "be dropped; pipeline is unaffected"
        )
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _init() -> None:
    """Initialise the Langfuse client once. On ANY failure, stay disabled."""
    global _client, _init_done, _enabled
    if _init_done:
        return
    _init_done = True

    if not (LANGFUSE_ENABLED and LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY and LANGFUSE_HOST):
        logger.info("Langfuse tracing disabled")
        return

    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST,
        )
        _enabled = True
        throttle = _OtelExportLogThrottle()
        for name in _NOISY_OTEL_LOGGERS:
            logging.getLogger(name).addFilter(throttle)
        logger.info("Langfuse tracing ENABLED (host=%s)", LANGFUSE_HOST)
    except Exception as e:  # ImportError, auth error, anything
        logger.warning("Langfuse init failed — tracing disabled: %s", e)
        _client = None
        _enabled = False


def get_langfuse():
    """The Langfuse client, or None when disabled/unavailable."""
    _init()
    return _client


def obs_enabled() -> bool:
    """True only when tracing is fully configured and the client initialised."""
    _init()
    return _enabled


def openai_client_classes():
    """
    Return (AsyncOpenAI, AsyncAzureOpenAI).

    When tracing is on these are Langfuse's drop-in wrappers, which capture
    model, messages, output, token usage, latency and cost for every call with
    no further code. Otherwise, or if the wrapper import fails, the plain SDK.
    """
    global _openai_wrapped
    if obs_enabled():
        try:
            from langfuse.openai import AsyncAzureOpenAI, AsyncOpenAI

            _openai_wrapped = True
            return AsyncOpenAI, AsyncAzureOpenAI
        except Exception as e:
            logger.warning("Langfuse OpenAI wrapper unavailable — using plain SDK: %s", e)

    from openai import AsyncAzureOpenAI, AsyncOpenAI

    _openai_wrapped = False
    return AsyncOpenAI, AsyncAzureOpenAI


def generation_kwargs(tracker) -> dict:
    """
    Extra kwargs for chat.completions.create() that name the generation in
    Langfuse after the pipeline stage and tag it with the casefile.

    Returns {} unless the Langfuse wrapper is active: the plain OpenAI SDK
    would forward unknown kwargs to the API and the call would fail.
    """
    if not _openai_wrapped or tracker is None:
        return {}
    return {
        "name": getattr(tracker, "stage", "llm"),
        "metadata": {
            "stage": getattr(tracker, "stage", None),
            "casefile_id": getattr(tracker, "casefile_id", None),
            "session_id": getattr(tracker, "session_id", None),
        },
    }


class _NullSpan:
    """No-op stand-in for a span/generation when tracing is off."""

    def update(self, *args, **kwargs):
        return self

    def update_trace(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return lambda *a, **k: None


@contextmanager
def _managed(factory, label):
    """
    Wrap a Langfuse context-manager factory so that a disabled client or a
    setup failure yields a _NullSpan (never raises), while an exception from
    the caller's own body still propagates — tracing must not swallow real
    errors.
    """
    client = get_langfuse()
    if client is None:
        yield _NullSpan()
        return

    try:
        cm = factory(client)
        obj = cm.__enter__()
    except Exception as e:
        logger.warning("Langfuse %s failed to start: %s", label, e)
        yield _NullSpan()
        return

    try:
        yield obj
    except Exception:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def trace_root(name: str, **kwargs):
    """Root span for one unit of work — one casefile extraction, one query."""
    return _managed(lambda c: c.start_as_current_span(name=name, **kwargs), f"trace_root({name})")


def span(name: str, **kwargs):
    """A child span — one window, one attachment digest."""
    return _managed(lambda c: c.start_as_current_span(name=name, **kwargs), f"span({name})")


def generation(name: str, **kwargs):
    """A generation for LLM calls that bypass the OpenAI wrapper (Gemini, Mistral)."""
    return _managed(
        lambda c: c.start_as_current_generation(name=name, **kwargs), f"generation({name})"
    )


def update_current_trace(**kwargs) -> None:
    """Set trace-level fields (session_id, metadata, tags). No-op when off."""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.update_current_trace(**kwargs)
    except Exception as e:
        logger.warning("Langfuse update_current_trace failed: %s", e)


def flush() -> None:
    """
    Drain the background batcher. Call before a short-lived process (backfill
    script, cronjob) exits, or its last traces are lost.

    client.flush() is a synchronous network call; run it on a daemon thread and
    give up after _FLUSH_TIMEOUT_SECONDS rather than hang shutdown.
    """
    client = get_langfuse()
    if client is None:
        return

    def _do_flush() -> None:
        try:
            client.flush()
        except Exception as e:
            logger.warning("Langfuse flush failed: %s", e)

    t = threading.Thread(target=_do_flush, name="langfuse-flush", daemon=True)
    t.start()
    t.join(_FLUSH_TIMEOUT_SECONDS)
    if t.is_alive():
        logger.warning(
            "Langfuse flush did not finish within %ss — continuing", _FLUSH_TIMEOUT_SECONDS
        )
