"""Context loader subagent for internal context gathering.

Runs in its own context window to search memory, load entity files, and
synthesize a concise briefing for the parent agent. This keeps raw search
results and intermediate reasoning out of the parent's context.
"""

import logging

from ..constants import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

_BRIEFING_PROMPT = (
    "You are a research assistant preparing a briefing for an AI agent that is about "
    "to handle a user request. You've been given raw context from multiple sources. "
    "Your job is to synthesize this into a concise, actionable briefing.\n\n"
    "Rules:\n"
    "- Lead with the most relevant information for the specific request\n"
    "- Drop anything that isn't relevant to the current request\n"
    "- Preserve specific facts, names, IDs, and decisions — don't over-summarize these\n"
    "- Merge duplicate information across sources\n"
    "- Keep the briefing under {max_tokens} tokens\n"
    "- Use a flat structure with clear sections only if there are distinct topics\n"
    "- If nothing is relevant, say 'No relevant context found.'\n\n"
    "Output ONLY the briefing text, no preamble."
)


def synthesize_briefing(
    task: str,
    raw_context: dict[str, str],
    model,
    max_tokens: int = 1500,
) -> str:
    """Use a cheap LLM call to synthesize raw context into a concise briefing.

    If the raw context is already small enough, skip the LLM call and
    return it directly.
    """
    if not raw_context:
        return ""

    # Format raw context for the synthesizer
    parts: list[str] = []
    for source, content in raw_context.items():
        label = source.replace("_", " ").title()
        parts.append(f"### Source: {label}\n{content}")

    combined = "\n\n---\n\n".join(parts)
    combined_tokens = len(combined) // CHARS_PER_TOKEN

    # If already under budget, skip the synthesis LLM call
    if combined_tokens <= max_tokens:
        logger.info(
            "Context loader: raw context (%d tokens) within budget, skipping synthesis",
            combined_tokens,
        )
        return combined

    # Synthesize with LLM
    try:
        result = model(
            [
                {
                    "role": "system",
                    "content": _BRIEFING_PROMPT.format(max_tokens=max_tokens),
                },
                {
                    "role": "user",
                    "content": (
                        f"User request:\n{task[:500]}\n\n"
                        f"Raw context:\n{combined}"
                    ),
                },
            ],
        )
        text = result.content if hasattr(result, "content") else str(result)
        briefing = text.strip()
        logger.info(
            "Context loader: synthesized %d token context into ~%d token briefing",
            combined_tokens,
            len(briefing) // CHARS_PER_TOKEN,
        )
        return briefing
    except Exception as e:
        logger.warning("Context loader synthesis failed, using truncated raw: %s", e)
        max_chars = max_tokens * CHARS_PER_TOKEN
        return combined[:max_chars] + "\n[...truncated]"

