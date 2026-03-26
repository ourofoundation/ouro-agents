"""Token and cost tracking for OpenRouter-powered agent runs."""

import functools
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from smolagents import OpenAIModel

logger = logging.getLogger(__name__)

ReasoningCallback = Callable[[str], None]

TRACKER_INT_FIELDS = {
    "input_tokens": "total_input_tokens",
    "cached_input_tokens": "total_cached_input_tokens",
    "cache_write_tokens": "total_cache_write_tokens",
    "input_audio_tokens": "total_input_audio_tokens",
    "input_video_tokens": "total_input_video_tokens",
    "output_tokens": "total_output_tokens",
    "reasoning_tokens": "total_reasoning_tokens",
    "output_audio_tokens": "total_output_audio_tokens",
    "output_image_tokens": "total_output_image_tokens",
    "accepted_prediction_tokens": "total_accepted_prediction_tokens",
    "rejected_prediction_tokens": "total_rejected_prediction_tokens",
    "num_api_calls": "num_calls",
}

TRACKER_FLOAT_FIELDS = {
    "cost_usd": "total_cost_usd",
    "input_cost_usd": "total_input_cost_usd",
    "output_cost_usd": "total_output_cost_usd",
    "upstream_inference_cost_usd": "total_upstream_inference_cost_usd",
}

STREAM_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

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

    def finalize(self) -> "RunUsage":
        self.total_tokens = self.input_tokens + self.output_tokens
        return self

    @classmethod
    def from_tracker(
        cls,
        tracker: "UsageTracker",
        *,
        model_id: str = "",
    ) -> "RunUsage":
        usage = cls(model_id=model_id)
        for attr, tracker_attr in TRACKER_INT_FIELDS.items():
            setattr(usage, attr, getattr(tracker, tracker_attr))
        for attr, tracker_attr in TRACKER_FLOAT_FIELDS.items():
            setattr(usage, attr, getattr(tracker, tracker_attr))
        usage.is_byok = tracker.is_byok
        if usage.cost_usd is not None:
            usage.cost_source = "response"
        return usage.finalize()

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

    def __init__(
        self,
        *args,
        tracker: Optional[UsageTracker] = None,
        reasoning_callback: Optional[ReasoningCallback] = None,
        **kwargs,
    ):
        self._tracker = tracker or UsageTracker()
        self._reasoning_callback = reasoning_callback
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
                return _wrap_stream(
                    original_create(*args, **kwargs),
                    tracker,
                    reasoning_callback=self._reasoning_callback,
                )
            response = original_create(*args, **kwargs)
            _record_response(
                response,
                tracker,
                reasoning_callback=self._reasoning_callback,
            )
            return response

        client.chat.completions.create = tracked_create
        return client


def _record_response(
    response,
    tracker: UsageTracker,
    *,
    reasoning_callback: Optional[ReasoningCallback] = None,
):
    record_usage_from_response(
        response,
        tracker,
        gen_id_prefix="noid",
        reasoning_callback=reasoning_callback,
    )


def record_usage_from_response(
    response: Any,
    tracker: UsageTracker,
    *,
    gen_id_prefix: str = "usage",
    reasoning_callback: Optional[ReasoningCallback] = None,
) -> Optional[str]:
    """Record usage from an OpenAI-compatible response object."""
    gen_id = _usage_field(response, "id")
    usage_data = _extract_usage_data(_usage_field(response, "usage"))
    _emit_reasoning_texts(
        _extract_visible_reasoning_from_response(response),
        reasoning_callback,
    )
    if not gen_id and _stream_usage_has_tokens(usage_data):
        gen_id = f"{gen_id_prefix}-{uuid.uuid4().hex}"
    if gen_id:
        tracker.record(gen_id, usage_data)
    return gen_id


def _stream_usage_has_tokens(data: dict[str, Any]) -> bool:
    return any(_to_int(data.get(k)) for k in STREAM_USAGE_TOKEN_FIELDS)


def _wrap_stream(
    stream,
    tracker: UsageTracker,
    *,
    reasoning_callback: Optional[ReasoningCallback] = None,
):
    """Iterate over an OpenAI stream, capturing the generation ID and final
    usage chunk, then record them on the tracker."""
    gen_id = None
    usage_data: dict[str, Any] = {}
    reasoning_by_choice: dict[int, str] = {}
    for chunk in stream:
        cid = _usage_field(chunk, "id")
        if not gen_id and cid:
            gen_id = cid
        usage = _usage_field(chunk, "usage")
        if usage:
            usage_data = _extract_usage_data(usage)
        _merge_stream_reasoning_chunk(reasoning_by_choice, chunk)
        yield chunk
    if not gen_id and _stream_usage_has_tokens(usage_data):
        gen_id = f"stream-{uuid.uuid4().hex}"
    if gen_id:
        tracker.record(gen_id, usage_data)
    _emit_reasoning_texts(reasoning_by_choice.values(), reasoning_callback)


def _merge_stream_reasoning_chunk(reasoning_by_choice: dict[int, str], chunk: Any) -> None:
    choices = _usage_field(chunk, "choices") or []
    for index, choice in enumerate(choices):
        delta = _usage_field(choice, "delta")
        if delta is None:
            continue
        text = _extract_stream_visible_reasoning_from_message(delta)
        if not text:
            continue
        current = reasoning_by_choice.get(index, "")
        if current and text.startswith(current):
            reasoning_by_choice[index] = text
        elif not current:
            reasoning_by_choice[index] = text
        elif text not in current:
            reasoning_by_choice[index] = current + text


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


def _usage_field(obj: Any, name: str) -> Any:
    """Read a field from an OpenAI-style usage object or a plain dict (streaming)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _normalize_reasoning_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        parts = [_normalize_reasoning_value(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "reasoning", "summary", "content", "value"):
            text = _normalize_reasoning_value(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value).strip()
    return str(value).strip()


def _extract_visible_reasoning_from_message(message: Any) -> str:
    if message is None:
        return ""
    for field in ("reasoning", "reasoning_details"):
        text = _normalize_reasoning_value(_usage_field(message, field))
        if text:
            return text
    return ""


def _extract_stream_visible_reasoning_from_message(message: Any) -> str:
    if message is None:
        return ""
    reasoning = _usage_field(message, "reasoning")
    if isinstance(reasoning, str):
        return reasoning
    return _extract_visible_reasoning_from_message(message)


def _extract_visible_reasoning_from_response(response: Any) -> list[str]:
    choices = _usage_field(response, "choices") or []
    texts: list[str] = []
    seen: set[str] = set()
    for choice in choices:
        message = _usage_field(choice, "message")
        text = _extract_visible_reasoning_from_message(message)
        if text and text not in seen:
            seen.add(text)
            texts.append(text)
    return texts


def _emit_reasoning_texts(
    texts,
    reasoning_callback: Optional[ReasoningCallback],
) -> None:
    if reasoning_callback is None:
        return
    seen: set[str] = set()
    for text in texts:
        cleaned = str(text).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        try:
            reasoning_callback(cleaned)
        except Exception:
            logger.exception("Failed to emit visible reasoning text")


def _cost_details_as_dict(cost_details: Any) -> dict[str, Any]:
    if cost_details is None:
        return {}
    if isinstance(cost_details, dict):
        return cost_details
    return {
        "upstream_inference_cost": _usage_field(
            cost_details, "upstream_inference_cost"
        ),
        "upstream_inference_prompt_cost": _usage_field(
            cost_details, "upstream_inference_prompt_cost"
        ),
        "upstream_inference_completions_cost": _usage_field(
            cost_details, "upstream_inference_completions_cost"
        ),
    }


def _reasoning_tokens_from_usage(
    raw_usage: Any, completion_details: Any
) -> int:
    """Reasoning count from completion breakdown; support alternate provider keys."""
    if completion_details is not None:
        r = _usage_field(completion_details, "reasoning_tokens")
        if r is not None:
            return _to_int(r)
        t = _usage_field(completion_details, "thinking_tokens")
        if t is not None:
            return _to_int(t)
    return _to_int(_usage_field(raw_usage, "reasoning_tokens"))


def _extract_usage_data(raw_usage: Any) -> dict[str, Any]:
    """Normalize provider usage payloads into a stable internal shape."""
    if raw_usage is None:
        return {}

    prompt_details = _usage_field(raw_usage, "prompt_tokens_details")
    completion_details = _usage_field(raw_usage, "completion_tokens_details")
    cost_details = _cost_details_as_dict(_usage_field(raw_usage, "cost_details"))

    cost = _to_float(_usage_field(raw_usage, "cost"))
    if cost is None:
        cost = _to_float(_usage_field(raw_usage, "total_cost"))
    if cost is None:
        cost = _to_float(cost_details.get("upstream_inference_cost"))

    reasoning_tokens = _reasoning_tokens_from_usage(
        raw_usage, completion_details
    )

    return {
        "input_tokens": _to_int(_usage_field(raw_usage, "prompt_tokens")),
        "cached_input_tokens": _to_int(
            _usage_field(prompt_details, "cached_tokens")
        ),
        "cache_write_tokens": _to_int(
            _usage_field(prompt_details, "cache_write_tokens")
        ),
        "input_audio_tokens": _to_int(_usage_field(prompt_details, "audio_tokens")),
        "input_video_tokens": _to_int(_usage_field(prompt_details, "video_tokens")),
        "output_tokens": _to_int(_usage_field(raw_usage, "completion_tokens")),
        "reasoning_tokens": reasoning_tokens,
        "output_audio_tokens": _to_int(
            _usage_field(completion_details, "audio_tokens")
        ),
        "output_image_tokens": _to_int(
            _usage_field(completion_details, "image_tokens")
        ),
        "accepted_prediction_tokens": _to_int(
            _usage_field(completion_details, "accepted_prediction_tokens")
        ),
        "rejected_prediction_tokens": _to_int(
            _usage_field(completion_details, "rejected_prediction_tokens")
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
        "is_byok": _usage_field(raw_usage, "is_byok"),
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

    model_id = getattr(model, "model_id", str(model))
    usage = RunUsage(model_id=model_id)

    if tracker:
        usage = RunUsage.from_tracker(tracker, model_id=model_id)
    elif hasattr(agent, "monitor"):
        counts = agent.monitor.get_total_token_counts()
        usage.input_tokens = counts.input_tokens
        usage.output_tokens = counts.output_tokens
        usage.total_tokens = counts.total_tokens

    for step in agent.memory.steps:
        if isinstance(step, ActionStep):
            usage.steps += 1

    return usage.finalize()


def _format_cost_part(
    total_cost: Optional[float],
    *,
    input_cost: Optional[float] = None,
    output_cost: Optional[float] = None,
    source: str = "",
) -> Optional[str]:
    if total_cost is None:
        return None

    cost_part = f"cost=${total_cost:.6f}"
    breakdown = []
    if input_cost is not None:
        breakdown.append(f"in=${input_cost:.6f}")
    if output_cost is not None:
        breakdown.append(f"out=${output_cost:.6f}")
    if breakdown:
        cost_part += f" ({', '.join(breakdown)})"
    if source:
        cost_part += f" [{source}]"
    return cost_part


def _usage_summary_parts(
    usage: Any,
    *,
    calls_attr: str,
    include_model: bool = False,
    include_steps: bool = False,
    include_cache_write: bool = False,
    include_cost_source: bool = False,
) -> list[str]:
    parts = []
    if include_model:
        parts.append(f"model={usage.model_id}")
    if include_steps:
        parts.append(f"steps={usage.steps}")

    parts.extend(
        [
            (
                f"in={usage.input_tokens:,}tok"
                f" (cached={usage.cached_input_tokens:,}, uncached={usage.uncached_input_tokens:,})"
            ),
            f"out={usage.output_tokens:,}tok",
        ]
    )

    if usage.reasoning_tokens:
        parts.append(f"reasoning={usage.reasoning_tokens:,}tok")
    if include_cache_write and usage.cache_write_tokens:
        parts.append(f"cache_write={usage.cache_write_tokens:,}tok")

    calls = getattr(usage, calls_attr, 0)
    if calls:
        parts.append(f"calls={calls}")

    cost_part = _format_cost_part(
        usage.cost_usd,
        input_cost=usage.input_cost_usd,
        output_cost=usage.output_cost_usd,
        source=usage.cost_source if include_cost_source else "",
    )
    if cost_part:
        parts.append(cost_part)

    return parts


def format_usage_summary(usage: RunUsage) -> str:
    """One-line human-readable usage summary for logging."""
    return " | ".join(
        _usage_summary_parts(
            usage,
            calls_attr="num_api_calls",
            include_model=True,
            include_steps=True,
            include_cost_source=True,
        )
    )


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


def _sum_ledger_attr(ledger: list[tuple[str, Any]], attr: str) -> int:
    return sum(int(getattr(u, attr, 0) or 0) for _, u in ledger)


def _residual_cost(
    total_v: Optional[float], sub_sum: Optional[float]
) -> Optional[float]:
    if total_v is None:
        return None
    if sub_sum is None:
        return total_v
    return max(0.0, round(total_v - sub_sum, 8))


def _usage_call_count(usage: Any) -> int:
    return int(
        getattr(usage, "num_api_calls", getattr(usage, "llm_calls", 0)) or 0
    )


def _combine_ledgers(*ledgers: Optional[list[tuple[str, Any]]]) -> list[tuple[str, Any]]:
    combined: list[tuple[str, Any]] = []
    for ledger in ledgers:
        if ledger:
            combined.extend(ledger)
    return combined


def residual_main_usage(
    total: RunUsage,
    subagent_ledger: Optional[list[tuple[str, Any]]] = None,
    memory_ledger: Optional[list[tuple[str, Any]]] = None,
) -> RunUsage:
    """Subtract subagent and memory deltas from aggregate tracker totals."""
    ledger = _combine_ledgers(subagent_ledger, memory_ledger)
    sub_cost = _sum_subagent_costs(ledger, "cost_usd")
    sub_in_cost = _sum_subagent_costs(ledger, "input_cost_usd")
    sub_out_cost = _sum_subagent_costs(ledger, "output_cost_usd")

    r = RunUsage(
        model_id=total.model_id,
        steps=total.steps,
        input_tokens=max(0, total.input_tokens - _sum_ledger_attr(ledger, "input_tokens")),
        cached_input_tokens=max(
            0, total.cached_input_tokens - _sum_ledger_attr(ledger, "cached_input_tokens")
        ),
        cache_write_tokens=max(
            0, total.cache_write_tokens - _sum_ledger_attr(ledger, "cache_write_tokens")
        ),
        input_audio_tokens=total.input_audio_tokens,
        input_video_tokens=total.input_video_tokens,
        output_tokens=max(0, total.output_tokens - _sum_ledger_attr(ledger, "output_tokens")),
        reasoning_tokens=max(
            0, total.reasoning_tokens - _sum_ledger_attr(ledger, "reasoning_tokens")
        ),
        output_audio_tokens=total.output_audio_tokens,
        output_image_tokens=total.output_image_tokens,
        accepted_prediction_tokens=total.accepted_prediction_tokens,
        rejected_prediction_tokens=total.rejected_prediction_tokens,
        total_tokens=0,
        num_api_calls=max(0, total.num_api_calls - sum(_usage_call_count(u) for _, u in ledger)),
        cost_usd=_residual_cost(total.cost_usd, sub_cost),
        input_cost_usd=_residual_cost(total.input_cost_usd, sub_in_cost),
        output_cost_usd=_residual_cost(total.output_cost_usd, sub_out_cost),
        upstream_inference_cost_usd=total.upstream_inference_cost_usd,
        is_byok=total.is_byok,
        cost_source=total.cost_source,
    )
    return r.finalize()


def format_subagent_usage_summary(u: Any) -> str:
    """One-line usage summary for a single subagent run (logging)."""
    return " | ".join(
        _usage_summary_parts(
            u,
            calls_attr="llm_calls",
            include_cache_write=True,
        )
    )


def format_component_usage_summary(u: Any) -> str:
    """One-line usage summary for auxiliary tracked components."""
    calls_attr = "num_api_calls" if hasattr(u, "num_api_calls") else "llm_calls"
    return " | ".join(
        _usage_summary_parts(
            u,
            calls_attr=calls_attr,
            include_cache_write=True,
        )
    )


def format_usage_breakdown(
    total: RunUsage,
    subagent_ledger: Optional[list[tuple[str, Any]]] = None,
    memory_ledger: Optional[list[tuple[str, Any]]] = None,
) -> str:
    """Human-readable usage for logs: main residual, details, then task total."""
    if not subagent_ledger and not memory_ledger:
        return format_usage_summary(total)
    main = residual_main_usage(total, subagent_ledger, memory_ledger)
    lines = [
        "main: " + format_usage_summary(main),
        *[
            f"sub:{name}: " + format_subagent_usage_summary(u)
            for name, u in (subagent_ledger or [])
        ],
        *[
            f"memory:{name}: " + format_component_usage_summary(u)
            for name, u in (memory_ledger or [])
        ],
        "task_total: " + format_usage_summary(total),
    ]
    return "\n".join(lines)
