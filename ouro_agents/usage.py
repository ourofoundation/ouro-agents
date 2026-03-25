"""Token and cost tracking for OpenRouter-powered agent runs."""

import functools
import logging
from dataclasses import dataclass
from typing import Any, Optional

from smolagents import OpenAIModel

logger = logging.getLogger(__name__)

@dataclass
class RunUsage:
    """Aggregated usage metrics for a single agent run."""

    model_id: str = ""
    steps: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    input_audio_tokens: int = 0
    input_video_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    output_audio_tokens: int = 0
    output_image_tokens: int = 0
    accepted_prediction_tokens: int = 0
    rejected_prediction_tokens: int = 0
    total_tokens: int = 0
    num_api_calls: int = 0
    cost_usd: Optional[float] = None
    input_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None
    upstream_inference_cost_usd: Optional[float] = None
    is_byok: Optional[bool] = None
    cost_source: str = ""

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)

    def dict(self) -> dict:
        d = {
            "model": self.model_id,
            "steps": self.steps,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "num_api_calls": self.num_api_calls,
            "input": {
                "tokens": self.input_tokens,
                "cached_tokens": self.cached_input_tokens,
                "uncached_tokens": self.uncached_input_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "audio_tokens": self.input_audio_tokens,
                "video_tokens": self.input_video_tokens,
            },
            "output": {
                "tokens": self.output_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "audio_tokens": self.output_audio_tokens,
                "image_tokens": self.output_image_tokens,
                "accepted_prediction_tokens": self.accepted_prediction_tokens,
                "rejected_prediction_tokens": self.rejected_prediction_tokens,
            },
        }
        if self.cost_usd is not None:
            d["cost_usd"] = round(self.cost_usd, 8)
        costs = {}
        if self.cost_usd is not None:
            costs["total_usd"] = round(self.cost_usd, 8)
        if self.input_cost_usd is not None:
            costs["input_usd"] = round(self.input_cost_usd, 8)
        if self.output_cost_usd is not None:
            costs["output_usd"] = round(self.output_cost_usd, 8)
        if self.upstream_inference_cost_usd is not None:
            costs["upstream_inference_usd"] = round(
                self.upstream_inference_cost_usd, 8
            )
        if self.is_byok is not None:
            costs["is_byok"] = self.is_byok
        if self.cost_source:
            costs["source"] = self.cost_source
        if costs:
            d["costs"] = costs
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

    def record(
        self,
        gen_id: str,
        usage: Optional[dict[str, Any]] = None,
    ):
        """Record a generation with token and cost metadata from the response."""
        generation = {"id": gen_id}
        if usage:
            generation.update(usage)
        self._generations.append(generation)

    def _sum_int(self, field: str) -> int:
        return sum(int(g.get(field, 0) or 0) for g in self._generations)

    def _sum_float(self, field: str) -> Optional[float]:
        values = [float(v) for g in self._generations if (v := g.get(field)) is not None]
        if not values:
            return None
        return round(sum(values), 8)

    def _aggregate_bool(self, field: str) -> Optional[bool]:
        values = [bool(g[field]) for g in self._generations if g.get(field) is not None]
        if not values:
            return None
        return all(values)

    @property
    def generation_ids(self) -> list[str]:
        return [g["id"] for g in self._generations]

    @property
    def total_input_tokens(self) -> int:
        return self._sum_int("input_tokens")

    @property
    def total_output_tokens(self) -> int:
        return self._sum_int("output_tokens")

    @property
    def total_cached_input_tokens(self) -> int:
        return self._sum_int("cached_input_tokens")

    @property
    def total_uncached_input_tokens(self) -> int:
        return max(0, self.total_input_tokens - self.total_cached_input_tokens)

    @property
    def total_cache_write_tokens(self) -> int:
        return self._sum_int("cache_write_tokens")

    @property
    def total_input_audio_tokens(self) -> int:
        return self._sum_int("input_audio_tokens")

    @property
    def total_input_video_tokens(self) -> int:
        return self._sum_int("input_video_tokens")

    @property
    def total_reasoning_tokens(self) -> int:
        return self._sum_int("reasoning_tokens")

    @property
    def total_output_audio_tokens(self) -> int:
        return self._sum_int("output_audio_tokens")

    @property
    def total_output_image_tokens(self) -> int:
        return self._sum_int("output_image_tokens")

    @property
    def total_accepted_prediction_tokens(self) -> int:
        return self._sum_int("accepted_prediction_tokens")

    @property
    def total_rejected_prediction_tokens(self) -> int:
        return self._sum_int("rejected_prediction_tokens")

    @property
    def total_cost_usd(self) -> Optional[float]:
        return self._sum_float("cost_usd")

    @property
    def total_input_cost_usd(self) -> Optional[float]:
        return self._sum_float("input_cost_usd")

    @property
    def total_output_cost_usd(self) -> Optional[float]:
        return self._sum_float("output_cost_usd")

    @property
    def total_upstream_inference_cost_usd(self) -> Optional[float]:
        return self._sum_float("upstream_inference_cost_usd")

    @property
    def is_byok(self) -> Optional[bool]:
        return self._aggregate_bool("is_byok")

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
    tracker.record(gen_id, _extract_usage_data(getattr(response, "usage", None)))


def _wrap_stream(stream, tracker: UsageTracker):
    """Iterate over an OpenAI stream, capturing the generation ID and final
    usage chunk, then record them on the tracker."""
    gen_id = None
    usage_data: dict[str, Any] = {}
    for chunk in stream:
        if not gen_id and getattr(chunk, "id", None):
            gen_id = chunk.id
        usage = getattr(chunk, "usage", None)
        if usage:
            usage_data = _extract_usage_data(usage)
        yield chunk
    if gen_id:
        tracker.record(gen_id, usage_data)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_usage_data(raw_usage: Any) -> dict[str, Any]:
    """Normalize provider usage payloads into a stable internal shape."""
    if raw_usage is None:
        return {}

    prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
    completion_details = getattr(raw_usage, "completion_tokens_details", None)
    cost_details = getattr(raw_usage, "cost_details", None) or {}
    if not isinstance(cost_details, dict):
        cost_details = {}

    cost = _to_float(getattr(raw_usage, "cost", None))
    if cost is None:
        cost = _to_float(getattr(raw_usage, "total_cost", None))
    if cost is None:
        cost = _to_float(cost_details.get("upstream_inference_cost"))

    return {
        "input_tokens": _to_int(getattr(raw_usage, "prompt_tokens", 0)),
        "cached_input_tokens": _to_int(getattr(prompt_details, "cached_tokens", 0)),
        "cache_write_tokens": _to_int(
            getattr(prompt_details, "cache_write_tokens", 0)
        ),
        "input_audio_tokens": _to_int(getattr(prompt_details, "audio_tokens", 0)),
        "input_video_tokens": _to_int(getattr(prompt_details, "video_tokens", 0)),
        "output_tokens": _to_int(getattr(raw_usage, "completion_tokens", 0)),
        "reasoning_tokens": _to_int(
            getattr(completion_details, "reasoning_tokens", 0)
        ),
        "output_audio_tokens": _to_int(
            getattr(completion_details, "audio_tokens", 0)
        ),
        "output_image_tokens": _to_int(
            getattr(completion_details, "image_tokens", 0)
        ),
        "accepted_prediction_tokens": _to_int(
            getattr(completion_details, "accepted_prediction_tokens", 0)
        ),
        "rejected_prediction_tokens": _to_int(
            getattr(completion_details, "rejected_prediction_tokens", 0)
        ),
        "cost_usd": cost,
        "input_cost_usd": _to_float(
            cost_details.get("upstream_inference_prompt_cost")
        ),
        "output_cost_usd": _to_float(
            cost_details.get("upstream_inference_completions_cost")
        ),
        "upstream_inference_cost_usd": _to_float(
            cost_details.get("upstream_inference_cost")
        ),
        "is_byok": getattr(raw_usage, "is_byok", None),
    }


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
        usage.cached_input_tokens = tracker.total_cached_input_tokens
        usage.cache_write_tokens = tracker.total_cache_write_tokens
        usage.input_audio_tokens = tracker.total_input_audio_tokens
        usage.input_video_tokens = tracker.total_input_video_tokens
        usage.output_tokens = tracker.total_output_tokens
        usage.reasoning_tokens = tracker.total_reasoning_tokens
        usage.output_audio_tokens = tracker.total_output_audio_tokens
        usage.output_image_tokens = tracker.total_output_image_tokens
        usage.accepted_prediction_tokens = tracker.total_accepted_prediction_tokens
        usage.rejected_prediction_tokens = tracker.total_rejected_prediction_tokens
        usage.total_tokens = usage.input_tokens + usage.output_tokens
        usage.num_api_calls = tracker.num_calls
        usage.input_cost_usd = tracker.total_input_cost_usd
        usage.output_cost_usd = tracker.total_output_cost_usd
        usage.upstream_inference_cost_usd = tracker.total_upstream_inference_cost_usd
        usage.is_byok = tracker.is_byok
        cost = tracker.total_cost_usd
        if cost is not None:
            usage.cost_source = "response"
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
        (
            f"in={usage.input_tokens:,}tok"
            f" (cached={usage.cached_input_tokens:,}, uncached={usage.uncached_input_tokens:,})"
        ),
        f"out={usage.output_tokens:,}tok",
    ]
    if usage.reasoning_tokens:
        parts.append(f"reasoning={usage.reasoning_tokens:,}tok")
    if usage.num_api_calls:
        parts.append(f"calls={usage.num_api_calls}")
    if usage.cost_usd is not None:
        cost_part = f"cost=${usage.cost_usd:.6f}"
        cost_breakdown = []
        if usage.input_cost_usd is not None:
            cost_breakdown.append(f"in=${usage.input_cost_usd:.6f}")
        if usage.output_cost_usd is not None:
            cost_breakdown.append(f"out=${usage.output_cost_usd:.6f}")
        if cost_breakdown:
            cost_part += f" ({', '.join(cost_breakdown)})"
        if usage.cost_source:
            cost_part += f" [{usage.cost_source}]"
        parts.append(cost_part)
    return " | ".join(parts)


def _sum_subagent_costs(
    ledger: list[tuple[str, Any]],
    attr: str,
) -> Optional[float]:
    vals = [
        float(v)
        for _, u in ledger
        if (v := getattr(u, attr, None)) is not None
    ]
    if not vals:
        return None
    return round(sum(vals), 8)


def residual_main_usage(
    total: RunUsage,
    ledger: list[tuple[str, Any]],
) -> RunUsage:
    """Subtract per-subagent deltas from aggregate tracker totals (shared tracker)."""
    s_in = sum(u.input_tokens for _, u in ledger)
    s_out = sum(u.output_tokens for _, u in ledger)
    s_cached = sum(u.cached_input_tokens for _, u in ledger)
    s_cw = sum(u.cache_write_tokens for _, u in ledger)
    s_reason = sum(u.reasoning_tokens for _, u in ledger)
    s_calls = sum(u.llm_calls for _, u in ledger)

    def _residual_cost(
        total_v: Optional[float], sub_sum: Optional[float]
    ) -> Optional[float]:
        if total_v is None:
            return None
        if sub_sum is None:
            return total_v
        return max(0.0, round(total_v - sub_sum, 8))

    sub_cost = _sum_subagent_costs(ledger, "cost_usd")
    sub_in_cost = _sum_subagent_costs(ledger, "input_cost_usd")
    sub_out_cost = _sum_subagent_costs(ledger, "output_cost_usd")

    r = RunUsage(
        model_id=total.model_id,
        steps=total.steps,
        input_tokens=max(0, total.input_tokens - s_in),
        cached_input_tokens=max(0, total.cached_input_tokens - s_cached),
        cache_write_tokens=max(0, total.cache_write_tokens - s_cw),
        input_audio_tokens=total.input_audio_tokens,
        input_video_tokens=total.input_video_tokens,
        output_tokens=max(0, total.output_tokens - s_out),
        reasoning_tokens=max(0, total.reasoning_tokens - s_reason),
        output_audio_tokens=total.output_audio_tokens,
        output_image_tokens=total.output_image_tokens,
        accepted_prediction_tokens=total.accepted_prediction_tokens,
        rejected_prediction_tokens=total.rejected_prediction_tokens,
        total_tokens=0,
        num_api_calls=max(0, total.num_api_calls - s_calls),
        cost_usd=_residual_cost(total.cost_usd, sub_cost),
        input_cost_usd=_residual_cost(total.input_cost_usd, sub_in_cost),
        output_cost_usd=_residual_cost(total.output_cost_usd, sub_out_cost),
        upstream_inference_cost_usd=total.upstream_inference_cost_usd,
        is_byok=total.is_byok,
        cost_source=total.cost_source,
    )
    r.total_tokens = r.input_tokens + r.output_tokens
    return r


def format_subagent_usage_summary(u: Any) -> str:
    """One-line usage summary for a single subagent run (logging)."""
    parts = [
        f"calls={u.llm_calls}",
        (
            f"in={u.input_tokens:,}tok"
            f" (cached={u.cached_input_tokens:,}, uncached={u.uncached_input_tokens:,})"
        ),
        f"out={u.output_tokens:,}tok",
    ]
    if u.reasoning_tokens:
        parts.append(f"reasoning={u.reasoning_tokens:,}tok")
    if u.cache_write_tokens:
        parts.append(f"cache_write={u.cache_write_tokens:,}tok")
    if u.cost_usd is not None:
        cost_part = f"cost=${u.cost_usd:.6f}"
        breakdown = []
        if u.input_cost_usd is not None:
            breakdown.append(f"in=${u.input_cost_usd:.6f}")
        if u.output_cost_usd is not None:
            breakdown.append(f"out=${u.output_cost_usd:.6f}")
        if breakdown:
            cost_part += f" ({', '.join(breakdown)})"
        parts.append(cost_part)
    return " | ".join(parts)


def format_usage_breakdown(
    total: RunUsage,
    ledger: list[tuple[str, Any]],
) -> str:
    """Human-readable usage for logs: main residual, each subagent, then task total."""
    if not ledger:
        return format_usage_summary(total)
    main = residual_main_usage(total, ledger)
    lines = [
        "main: " + format_usage_summary(main),
        *[f"sub:{name}: " + format_subagent_usage_summary(u) for name, u in ledger],
        "task_total: " + format_usage_summary(total),
    ]
    return "\n".join(lines)
