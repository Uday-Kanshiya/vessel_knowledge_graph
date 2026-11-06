"""
One window: assemble the prompt, call the model, apply the delta.

The model sees: an id-keyed SUMMARY of the record so far (every equipment,
problem, fault and action with its id and the fields needed to recognise it,
plus the next free id of each family), then the window's mails in pagenum
order, each with id, date, from/to/subject, body text and the digests of its
attachments (sha + digest, never a URL). The summary is the model's memory
across windows: it updates a row by id or mints the next free id. With a
non-zero overlap, mails already folded in by the previous window are marked
as context.

Retries: a provider error whose cause is a rate limit / timeout / connection
error / 5xx is retried with exponential backoff (EXTRACTION_LLM_RETRIES). A
delta the code cannot apply (DeltaError) is retried ONCE with the reason
appended to the prompt; if it fails again the window is recorded as rejected
and the record is left as it was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import (
    EXTRACTION_LLM_RETRIES,
    EXTRACTION_LLM_RETRY_BASE_SECONDS,
    EXTRACTION_WINDOW_MAX_TOKENS,
    EXTRACTION_WINDOW_MAX_TOKENS_CAP,
)
from src.extraction.record import (
    Applied,
    DeltaError,
    WindowContext,
    apply_delta,
    summary_for_prompt,
)
from src.llm import LLMClient, LLMError
from src.utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

_RETRYABLE = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError", "TimeoutError", "ConnectError", "ReadTimeout")


def is_retryable(err: Exception) -> bool:
    cause = err.__cause__ or err
    name = type(cause).__name__
    if name in _RETRYABLE:
        return True
    status = getattr(cause, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def is_length_cutoff(err: Exception) -> bool:
    return "finish_reason='length'" in str(err)


async def json_call_with_budget(llm, prompt: str, system_prompt: str, tracker, label: str, start: int = EXTRACTION_WINDOW_MAX_TOKENS, cap: int = EXTRACTION_WINDOW_MAX_TOKENS_CAP) -> Any:
    """json_completion with retries, doubling max_tokens on a length cut-off up to `cap`."""
    budget = start
    while True:
        try:
            return await call_with_retry(
                lambda p=prompt, b=budget: llm.json_completion(p, system_message=system_prompt, token_tracker=tracker, max_tokens=b),
                label,
            )
        except LLMError as e:
            if is_length_cutoff(e) and budget < cap:
                budget = min(budget * 2, cap)
                logger.warning("%s: output cut at the token limit, retrying with max_tokens=%d", label, budget)
                continue
            raise


async def call_with_retry(coro_factory, tracker_label: str, retries: int = EXTRACTION_LLM_RETRIES, base: float = EXTRACTION_LLM_RETRY_BASE_SECONDS) -> Any:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await coro_factory()
        except LLMError as e:
            if attempt >= max(1, retries) or not is_retryable(e):
                raise
            delay = base * (2 ** (attempt - 1))
            logger.warning("%s: retry %d/%d in %.1fs after %s", tracker_label, attempt, retries, delay, e)
            await asyncio.sleep(delay)


@dataclass
class WindowMail:
    mail_id: str
    pagenum: int
    date: Optional[str]
    sender: Optional[str]
    to: Optional[str]
    subject: Optional[str]
    body: str
    attachments: list[dict] = field(default_factory=list)  # digest_for_prompt() dicts
    context_only: bool = False  # already processed by the previous window


def render_window(record: dict, mails: list[WindowMail]) -> str:
    summary = summary_for_prompt(record)
    nxt = summary["next_free_ids"]
    parts = [
        "## RECORD SO FAR (summary with ids)\n",
        "Update an existing row by its id when this window continues it; otherwise mint the next free id.\n",
        f"next_free_ids: equipment {nxt['equipment']}, problems {nxt['problems']}, "
        f"faults {nxt['faults']}, action_logs {nxt['action_logs']}\n",
        json.dumps(summary, ensure_ascii=False, indent=1),
        "\n\n## WINDOW\n",
    ]
    for m in mails:
        tag = " (context only — already processed in the previous window)" if m.context_only else ""
        parts.append(f"\n### MAIL mail_id={m.mail_id} pagenum={m.pagenum}{tag}\n")
        parts.append(f"date: {m.date}\nfrom: {m.sender}\nto: {m.to}\nsubject: {m.subject}\n")
        if m.attachments:
            parts.append("attachments:\n")
            for a in m.attachments:
                parts.append(json.dumps(a, ensure_ascii=False) + "\n")
        else:
            parts.append("attachments: none\n")
        parts.append("body:\n" + m.body + "\n")
    parts.append("\n## RESPOND\nReturn the JSON delta for this window only.")
    return "".join(parts)


@dataclass
class WindowResult:
    index: int
    pagenums: list[int]
    mail_ids: list[str]
    applied: Optional[Applied]
    raw_response: Any
    attempts: int
    rejected: bool
    error: Optional[str]
    seconds: float
    prompt_chars: int
    # What the model was GIVEN (audit, docs/14). prompt_text is the exact user prompt of the
    # first attempt (render_window output); a retry appends feedback, kept in attempts_log.
    prompt_text: str = ""
    attempts_log: list[dict] = field(default_factory=list)
    record_summary: dict = field(default_factory=dict)


async def run_window(
    llm: LLMClient,
    system_prompt: str,
    record: dict,
    mails: list[WindowMail],
    ctx: WindowContext,
    tracker: Optional[TokenTracker],
    index: int,
) -> WindowResult:
    t0 = time.monotonic()
    record_summary = summary_for_prompt(record)  # the memory embedded in the prompt, before apply
    user_prompt = render_window(record, mails)
    pagenums = [m.pagenum for m in mails]
    mail_ids = [m.mail_id for m in mails]
    attempts = 0
    feedback = ""
    raw: Any = None
    attempts_log: list[dict] = []
    while True:
        attempts += 1
        prompt = user_prompt + feedback
        raw = await json_call_with_budget(llm, prompt, system_prompt, tracker, f"window {index}")
        entry = {"attempt": attempts, "prompt_chars": len(prompt), "feedback": feedback, "raw_response": raw, "error": None}
        attempts_log.append(entry)
        try:
            applied = apply_delta(record, raw, ctx)
            return WindowResult(
                index, pagenums, mail_ids, applied, raw, attempts, False, None, time.monotonic() - t0, len(prompt),
                prompt_text=user_prompt, attempts_log=attempts_log, record_summary=record_summary,
            )
        except DeltaError as e:
            logger.warning("window %d: delta rejected (%s)", index, e)
            entry["error"] = str(e)
            if attempts >= 2:
                return WindowResult(
                    index, pagenums, mail_ids, None, raw, attempts, True, str(e), time.monotonic() - t0, len(prompt),
                    prompt_text=user_prompt, attempts_log=attempts_log, record_summary=record_summary,
                )
            feedback = (
                "\n\n## CORRECTION\nYour previous response could not be applied: "
                f"{e}. Return the corrected JSON delta. Existing ids must not be reused; new ids continue each sequence."
            )
