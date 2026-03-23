"""Post-run refinement: learn from each execution to improve the next one.

A reusable pattern for any repeated agent task. After a run completes, a
cheap LLM call reviews the conversation log and produces actionable
learnings — things to do differently, skip, or remember next time.

Learnings accumulate over runs so the agent gets progressively better at
a recurring task without the user having to re-explain things.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_LEARNINGS = 20  # cap to keep prompt injection bounded

REFINEMENT_PROMPT = """\
You are a task refinement engine. You just watched an agent execute a recurring task.
Your job is to extract actionable learnings that will make the NEXT run smoother.

Focus on:
- Mistakes or dead ends the agent hit (so it can avoid them)
- Successful patterns worth repeating
- Missing context the agent had to discover (API quirks, data formats, etc.)
- Quality improvements for the output (formatting, depth, structure)

Rules:
- Each learning should be a concrete, actionable instruction (not an observation).
  Good: "Use the search_assets tool with type='post' filter to avoid getting dataset results"
  Bad: "The agent had trouble finding the right assets"
- Only include things that would be NON-OBVIOUS to a fresh agent reading the original prompt.
- Deduplicate against existing learnings — don't repeat what's already known.
- If the run went perfectly with no issues, return an empty list.
- Return 0-5 new learnings per run. Quality over quantity.

Output ONLY valid JSON, no markdown fences:
{
  "new_learnings": ["string"],
  "drop_learnings": ["string"],
  "summary": "string"
}

- new_learnings: Actionable instructions to add for future runs.
- drop_learnings: Copy exact text of any EXISTING learnings that are now outdated or wrong.
- summary: One-line summary of how this run went (for logging).
"""


@dataclass
class RefinementResult:
    new_learnings: list[str] = field(default_factory=list)
    drop_learnings: list[str] = field(default_factory=list)
    summary: str = ""


def build_refinement_context(
    original_prompt: str,
    existing_learnings: list[str],
    conversation_log: list[dict],
) -> str:
    """Build the user message for the refinement LLM call."""
    learnings_text = (
        "\n".join(f"- {l}" for l in existing_learnings)
        if existing_learnings
        else "(none yet)"
    )

    # Trim conversation to last N turns to stay within budget
    recent = conversation_log[-12:]
    turns_text = "\n".join(
        f"{t.get('role', '?')}: {str(t.get('content', ''))[:500]}"
        for t in recent
    )

    return (
        f"## Original Task Prompt\n{original_prompt}\n\n"
        f"## Existing Learnings\n{learnings_text}\n\n"
        f"## Conversation Log (this run)\n{turns_text}"
    )


def refine(
    original_prompt: str,
    existing_learnings: list[str],
    conversations_dir: Path,
    conversation_id: str,
    model,
) -> RefinementResult:
    """Run a refinement LLM call after a task execution.

    Parameters
    ----------
    original_prompt : the task's base prompt
    existing_learnings : learnings accumulated from prior runs
    conversations_dir : path to conversation JSONL files
    conversation_id : the conversation to analyze
    model : a cheap smolagents model instance
    """
    # Load the conversation log
    jsonl_path = conversations_dir / f"{conversation_id}.jsonl"
    if not jsonl_path.exists():
        return RefinementResult()

    turns = []
    for line in jsonl_path.read_text().strip().split("\n"):
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not turns:
        return RefinementResult()

    user_content = build_refinement_context(original_prompt, existing_learnings, turns)

    try:
        result = model(
            [
                {"role": "system", "content": REFINEMENT_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return RefinementResult(
            new_learnings=data.get("new_learnings", []),
            drop_learnings=data.get("drop_learnings", []),
            summary=data.get("summary", ""),
        )
    except Exception as e:
        logger.warning("Refinement LLM call failed: %s", e)
        return RefinementResult()


def apply_learnings(
    existing: list[str],
    result: RefinementResult,
) -> list[str]:
    """Merge refinement results into the existing learnings list.

    Drops outdated learnings, appends new ones, and caps at MAX_LEARNINGS.
    """
    # Remove learnings flagged for dropping
    drop_set = set(result.drop_learnings)
    updated = [l for l in existing if l not in drop_set]

    # Append new learnings (deduplicate)
    existing_set = set(updated)
    for learning in result.new_learnings:
        if learning not in existing_set:
            updated.append(learning)
            existing_set.add(learning)

    # Cap to prevent unbounded growth — keep most recent
    if len(updated) > MAX_LEARNINGS:
        updated = updated[-MAX_LEARNINGS:]

    return updated


def format_learnings_for_prompt(learnings: list[str]) -> str:
    """Format learnings as a section to inject into the agent's task prompt."""
    if not learnings:
        return ""
    items = "\n".join(f"- {l}" for l in learnings)
    return (
        f"\n\n## Learnings from Previous Runs\n"
        f"These are things you've learned from past executions of this task. "
        f"Apply them to avoid repeating mistakes:\n{items}"
    )
