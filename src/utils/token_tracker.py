"""
Token accounting for one unit of LLM work.

Create one TokenTracker per (stage, casefile) — e.g. every window_update call
for one casefile — and pass it to each LLMClient call.

The model is recorded per call, not per tracker, because the phase 0 cost model
(docs/02 §6) is priced by model, and the 5% shadow sample (§2.2) runs two
models over the same casefile.

In memory only for now. Persisting to a token_log collection lands with
src/db; it will read `calls` and `get_totals()` as they are.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenTracker:
    stage: str  # window_update | consolidate | attachment_digest | ...
    casefile_id: Optional[str] = None
    session_id: Optional[str] = None
    calls: list[dict] = field(default_factory=list)

    def add_call(self, input_tokens: int, output_tokens: int, model: Optional[str] = None) -> None:
        self.calls.append(
            {"model": model, "input_tokens": input_tokens, "output_tokens": output_tokens}
        )

    def get_totals(self) -> dict:
        """Same top-level keys as qhse's TokenTracker, plus a per-model split."""
        total_in = sum(c["input_tokens"] for c in self.calls)
        total_out = sum(c["output_tokens"] for c in self.calls)

        by_model: dict[str, dict] = {}
        for c in self.calls:
            m = by_model.setdefault(
                c["model"] or "unknown", {"input_tokens": 0, "output_tokens": 0, "call_count": 0}
            )
            m["input_tokens"] += c["input_tokens"]
            m["output_tokens"] += c["output_tokens"]
            m["call_count"] += 1

        return {
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "call_count": len(self.calls),
            "by_model": by_model,
        }
