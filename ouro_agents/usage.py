"""Token and cost tracking for OpenRouter-powered agent runs.

Captures generation IDs from every OpenRouter API call (streaming and
non-streaming) and queries the per-generation cost endpoint for exact
billing data — no race conditions with shared API keys.
"""

import functools
import logging
import os
from dataclasses import dataclass
from typing import Optional

from smolagents import OpenAIModel

logger = logging.getLogger(__name__)


@dataclass
class RunUsage:
    """Aggregated usage metrics for a single agent run."""

    model_id: str = ""
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    num_api_calls: int = 0
    cost_usd: Optional[float] = None

    def dict(self) -> dict:
        d = {
            "model": self.model_id,
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "num_api_calls": self.num_api_calls,
        }
        if self.cost_usd is not None:
            d["cost_usd"] = round(self.cost_usd, 6)
        return d


# ---------------------------------------------------------------------------
# Generation-level tracking
# ---------------------------------------------------------------------------


class UsageTracker:
    """Shared accumulator that collects generation metadata across model calls.

    Attach a single instance to every ``TrackedOpenAIModel`` created for a run
    so all API calls (main agent loop + auxiliary) are recorded together.
    """

    def __init__(self):
        self._generations: list[dict] = []

    def record(self, gen_id: str, input_tokens: int = 0, output_tokens: int = 0):
        self._generations.append(
            {"id": gen_id, "input_tokens": input_tokens, "output_tokens": output_tokens}
        )

    @property
    def generation_ids(self) -> list[str]:
        return [g["id"] for g in self._generations]

    @property
    def total_input_tokens(self) -> int:
        return sum(g.get("input_tokens", 0) for g in self._generations)

    @property
    def total_output_tokens(self) -> int:
        return sum(g.get("output_tokens", 0) for g in self._generations)

    @property
    def num_calls(self) -> int:
        return len(self._generations)

    def reset(self):
        self._generations.clear()


# ---------------------------------------------------------------------------
# Tracked model — wraps the underlying OpenAI client to capture gen IDs
# ---------------------------------------------------------------------------


class TrackedOpenAIModel(OpenAIModel):
    """``OpenAIModel`` that intercepts every API call to record generation IDs
    and per-call token counts on a shared :class:`UsageTracker`."""

    def __init__(self, *args, tracker: Optional[UsageTracker] = None, **kwargs):
        self._tracker = tracker or UsageTracker()
        super().__init__(*args, **kwargs)

    @property
    def tracker(self) -> UsageTracker:
        return self._tracker

    def create_client(self):
        client = super().create_client()
        original_create = client.chat.completions.create
        tracker = self._tracker

        @functools.wraps(original_create)
        def tracked_create(*args, **kwargs):
            if kwargs.get("stream"):
                return _wrap_stream(original_create(*args, **kwargs), tracker)
            response = original_create(*args, **kwargs)
            _record_response(response, tracker)
            return response

        client.chat.completions.create = tracked_create
        return client


def _record_response(response, tracker: UsageTracker):
    gen_id = getattr(response, "id", None)
    if not gen_id:
        return
    in_tok = 0
    out_tok = 0
    if response.usage:
        in_tok = getattr(response.usage, "prompt_tokens", 0) or 0
        out_tok = getattr(response.usage, "completion_tokens", 0) or 0
    tracker.record(gen_id, in_tok, out_tok)


def _wrap_stream(stream, tracker: UsageTracker):
    """Iterate over an OpenAI stream, capturing the generation ID and final
    usage chunk, then record them on the tracker."""
    gen_id = None
    in_tok = 0
    out_tok = 0
    for chunk in stream:
        if not gen_id and getattr(chunk, "id", None):
            gen_id = chunk.id
        usage = getattr(chunk, "usage", None)
        if usage:
            in_tok = getattr(usage, "prompt_tokens", 0) or 0
            out_tok = getattr(usage, "completion_tokens", 0) or 0
        yield chunk
    if gen_id:
        tracker.record(gen_id, in_tok, out_tok)


# ---------------------------------------------------------------------------
# Cost lookup via OpenRouter generation endpoint
# ---------------------------------------------------------------------------


def _fetch_generation_cost(gen_id: str, api_key: str) -> Optional[float]:
    try:
        import httpx

        resp = httpx.get(
            f"https://openrouter.ai/api/v1/generation?id={gen_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("total_cost")
    except Exception as e:
        logger.debug("Failed to fetch cost for generation %s: %s", gen_id, e)
    return None


def fetch_run_cost(tracker: UsageTracker) -> Optional[float]:
    """Query OpenRouter for exact cost of every generation in the run."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key or not tracker.generation_ids:
        return None

    total = 0.0
    for gen_id in tracker.generation_ids:
        cost = _fetch_generation_cost(gen_id, api_key)
        if cost is not None:
            total += cost
    return round(total, 6)


# ---------------------------------------------------------------------------
# Post-run collection helpers
# ---------------------------------------------------------------------------


def collect_run_usage(
    agent,
    model,
    tracker: Optional[UsageTracker] = None,
) -> RunUsage:
    """Build a :class:`RunUsage` from a completed smolagents run."""
    from smolagents import ActionStep

    usage = RunUsage()
    usage.model_id = getattr(model, "model_id", str(model))

    if tracker:
        usage.input_tokens = tracker.total_input_tokens
        usage.output_tokens = tracker.total_output_tokens
        usage.total_tokens = usage.input_tokens + usage.output_tokens
        usage.num_api_calls = tracker.num_calls
        cost = fetch_run_cost(tracker)
        if cost is not None:
            usage.cost_usd = cost
    elif hasattr(agent, "monitor"):
        counts = agent.monitor.get_total_token_counts()
        usage.input_tokens = counts.input_tokens
        usage.output_tokens = counts.output_tokens
        usage.total_tokens = counts.total_tokens

    for step in agent.memory.steps:
        if isinstance(step, ActionStep):
            usage.steps += 1

    return usage


def format_usage_summary(usage: RunUsage) -> str:
    """One-line human-readable usage summary for logging."""
    parts = [
        f"model={usage.model_id}",
        f"steps={usage.steps}",
        f"in={usage.input_tokens:,}tok",
        f"out={usage.output_tokens:,}tok",
    ]
    if usage.num_api_calls:
        parts.append(f"calls={usage.num_api_calls}")
    if usage.cost_usd is not None:
        parts.append(f"cost=${usage.cost_usd:.4f}")
    return " | ".join(parts)
