"""Asset helpers for the Ouro-native artifact system.

Subagents persist their output as real Ouro assets (posts, files, datasets)
via MCP tools and return a structured asset object as their final_answer.
This module provides helpers to:

  - PrefetchSpec / resolve_prefetch: declare what context to pre-fetch for a
    run (assets, comment threads, etc.) and resolve it into formatted markdown.
  - fetch_asset_content: retrieve asset content via get_asset for injection
    into subagent task prompts (replacing the old ArtifactStore.format_for_prompt)
  - parse_asset_result: extract a structured asset object from a subagent's
    final_answer text (JSON with asset_id, asset_type, name, description, content)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .constants import CHARS_PER_TOKEN

logger = logging.getLogger(__name__)

ASSET_REQUIRED_KEYS = {"asset_id", "name"}


def _extract_asset_body(data: dict[str, Any]) -> str:
    """Return the most useful human-readable body from a full asset payload."""
    for key in ("content_text", "content", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    extra_fields = {}
    for key in (
        "preview",
        "schema",
        "stats",
        "routes",
        "method",
        "path",
        "route_description",
        "parameters",
        "request_body",
        "input_type",
        "output_type",
        "file_url",
        "mime_type",
        "size",
    ):
        value = data.get(key)
        if value not in (None, "", [], {}):
            extra_fields[key] = value

    if extra_fields:
        return json.dumps(extra_fields, indent=2, sort_keys=True)
    return ""


def parse_asset_result(text: str) -> Optional[dict[str, Any]]:
    """Try to parse a subagent final_answer as a structured asset object.

    Returns a dict with keys asset_id, asset_type, name, description, content
    if the text is valid JSON with at least asset_id and name present.
    Returns None if the text is plain text (not an asset object).
    """
    if not text or not text.strip().startswith("{"):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    if not ASSET_REQUIRED_KEYS.issubset(data):
        return None

    return {
        "asset_id": data["asset_id"],
        "asset_type": data.get("asset_type", "post"),
        "name": data["name"],
        "description": data.get("description", ""),
        "content": data.get("content", ""),
    }


def fetch_asset_content(
    deferred_tools: dict,
    refs: list[str],
    max_tokens: int = 4000,
) -> str:
    """Fetch Ouro asset content for injection into a subagent task prompt.

    Calls ouro:get_asset for each UUID in refs and formats the results
    similarly to the old ArtifactStore.format_for_prompt.
    """
    if not refs:
        return ""

    get_asset = deferred_tools.get("ouro:get_asset")
    if not get_asset:
        logger.warning("Cannot fetch asset content: ouro:get_asset not available")
        return ""

    parts: list[str] = []
    total_chars = 0
    max_chars = max_tokens * CHARS_PER_TOKEN

    for ref in refs:
        try:
            raw = get_asset(id=ref, detail="full")
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.warning("Failed to fetch asset %s: %s", ref, e)
            parts.append(f"### Asset {ref}\n- status: failed to fetch")
            continue

        if isinstance(data, dict):
            name = data.get("name", ref)
            description = data.get("description", "")
            asset_type = data.get("asset_type", data.get("type", "unknown"))
            content = _extract_asset_body(data)
        else:
            name = ref
            description = ""
            asset_type = "unknown"
            content = str(data)

        header_lines = [
            f"### {name}",
            f"- id: {ref}",
            f"- type: {asset_type}",
        ]
        if description:
            header_lines.append(f"- description: {description}")
        header = "\n".join(header_lines)

        remaining = max_chars - total_chars
        if remaining < 200:
            parts.append("\n[...additional assets truncated]")
            break

        body = (content or "").strip()
        body_budget = max(0, remaining - len(header) - 20)
        if body and len(body) > body_budget:
            body = body[:body_budget] + "\n[...truncated]"
        block = f"{header}\n\n{body}".strip()
        parts.append(block)
        total_chars += len(block)

    return "\n\n".join(parts)


def _fetch_comment_thread(
    deferred_tools: dict,
    parent_ids: list[str],
    max_tokens: int = 3000,
) -> str:
    """Pre-fetch comments for assets and format as a readable thread."""
    if not parent_ids:
        return ""

    get_comments = deferred_tools.get("ouro:get_comments")
    if not get_comments:
        logger.warning("Cannot fetch comments: ouro:get_comments not available")
        return ""

    parts: list[str] = []
    total_chars = 0
    max_chars = max_tokens * CHARS_PER_TOKEN

    for parent_id in parent_ids:
        try:
            raw = get_comments(parent_id=parent_id)
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.warning("Failed to fetch comments for %s: %s", parent_id, e)
            continue

        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            continue

        thread_lines: list[str] = [f"### Comment thread on {parent_id}"]
        for comment in results:
            author = comment.get("author", "unknown")
            text = comment.get("text", "").strip()
            created = comment.get("created_at", "")
            cid = comment.get("id", "")
            reply_count = comment.get("reply_count", 0)

            if not text:
                continue

            entry = f"- **@{author}**"
            if created:
                entry += f" ({created})"
            entry += f": {text}"
            if reply_count:
                entry += f" _({reply_count} replies)_"
            if cid:
                entry += f" [id: {cid}]"
            thread_lines.append(entry)

        block = "\n".join(thread_lines)
        remaining = max_chars - total_chars
        if remaining < 200:
            parts.append("[...additional comment threads truncated]")
            break
        if len(block) > remaining:
            block = block[:remaining] + "\n[...truncated]"
        parts.append(block)
        total_chars += len(block)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# PrefetchSpec — declares what context to pre-fetch for a run
# ---------------------------------------------------------------------------


@dataclass
class PrefetchSpec:
    """Declares what context should be pre-fetched before an agent run.

    Add new fields here when new prefetch types are needed — no signature
    changes required anywhere in the call chain.
    """

    asset_ids: list[str] = field(default_factory=list)
    comment_parent_ids: list[str] = field(default_factory=list)
    thread_comment_parent_ids: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return (
            not self.asset_ids
            and not self.comment_parent_ids
            and not self.thread_comment_parent_ids
        )


def resolve_prefetch(deferred_tools: dict, spec: PrefetchSpec) -> str:
    """Resolve a PrefetchSpec into formatted markdown context.

    Each block is self-contained — adding a new prefetch type means adding
    a field to PrefetchSpec and a block here.
    """
    if spec.empty:
        return ""

    parts: list[str] = []

    asset_ctx = fetch_asset_content(deferred_tools, spec.asset_ids)
    if asset_ctx:
        parts.append(f"## Input Assets\n{asset_ctx}")

    comment_ctx = _fetch_comment_thread(deferred_tools, spec.comment_parent_ids)
    if comment_ctx:
        parts.append(f"## Top-Level Comments\n{comment_ctx}")

    thread_ctx = _fetch_comment_thread(deferred_tools, spec.thread_comment_parent_ids)
    if thread_ctx:
        parts.append(f"## Current Thread\n{thread_ctx}")

    return "\n\n".join(parts)
