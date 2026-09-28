"""Outcome evidence digests for planning and reflection.

Completion is not success. This module rolls up external engagement on work
the agent produced (views, comments, reactions, downloads, quest entries from
others) so planning retrospectives and dream/reflection can grade results,
not throughput.

Prefers the platform Impact API when available; falls back to per-asset
``counts`` plus comment authorship checks so agents work before the API ships.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ouro import Ouro

from ..syncing import read_field
from ..constants import clip_text
from .planning import quest_items, quest_status, search_own_quests

if TYPE_CHECKING:
    from ..agent import OuroAgent

logger = logging.getLogger(__name__)

_ASSET_ID_RE = re.compile(
    r"(?:asset:|/posts/|/quests/|/datasets/|/files/|/assets/)?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


def _snippet(text: object, max_len: int) -> str:
    return clip_text(text, max_len)


def _extract_asset_ids(*texts: object) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _ASSET_ID_RE.finditer(str(text or "")):
            asset_id = match.group(1).lower()
            if asset_id not in seen:
                seen.add(asset_id)
                found.append(asset_id)
    return found


def _submission_asset_ids(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    raw = item.get("submission_assets")
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, dict):
                asset_id = value.get("asset_id") or value.get("id")
                if asset_id:
                    ids.append(str(asset_id))
            elif isinstance(value, str) and value:
                ids.append(value)
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict):
                asset_id = value.get("asset_id") or value.get("id")
                if asset_id:
                    ids.append(str(asset_id))
            elif isinstance(value, str) and value:
                ids.append(value)
    ids.extend(_extract_asset_ids(item.get("notes")))
    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for asset_id in ids:
        if asset_id not in seen:
            seen.add(asset_id)
            out.append(asset_id)
    return out


def _zero_counts() -> dict[str, int]:
    return {
        "views": 0,
        "comments": 0,
        "reactions": 0,
        "downloads": 0,
        "external_comments": 0,
        "external_reactions": 0,
        "quality_views": 0,
        "external_entries": 0,
    }


def _try_impact_api(
    ouro: Ouro, asset_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Return per-asset impact metrics, or None if the Impact API fails."""
    if not asset_ids:
        return None
    try:
        rows = ouro.assets.impact(asset_ids)
    except Exception as e:
        logger.debug("assets.impact unavailable: %s", e)
        return None
    return {str(row.asset_id): row.model_dump() for row in rows}


def _count_external(user_ids: list[Any], owner_user_id: str | None) -> int:
    return sum(
        1 for user_id in user_ids if user_id and str(user_id) != str(owner_user_id)
    )


def _fallback_asset_metrics(
    ouro: Ouro, asset_id: str, owner_user_id: str | None
) -> dict[str, int]:
    """Best-effort metrics via counts + comment authorship."""
    metrics = _zero_counts()
    try:
        counts = ouro.assets.counts(asset_id)
        metrics["views"] = counts.views
        metrics["comments"] = counts.comments
        metrics["reactions"] = counts.reactions
        metrics["downloads"] = counts.downloads
        # Without the Impact API we cannot filter bots; treat views as
        # a weak quality proxy.
        metrics["quality_views"] = counts.views
    except Exception as e:
        logger.debug("counts failed for %s: %s", asset_id, e)

    try:
        comments = ouro.comments.list_by_parent(asset_id)
        metrics["external_comments"] = _count_external(
            [comment.user_id for comment in comments], owner_user_id
        )
    except Exception as e:
        logger.debug("comment fallback failed for %s: %s", asset_id, e)

    return metrics


def _merge_metrics(into: dict[str, int], row: dict[str, Any]) -> None:
    for key in into:
        into[key] += int(row.get(key) or 0)


def _count_external_entries(
    ouro: Ouro, quest_id: str, owner_user_id: str | None
) -> int:
    try:
        entries = ouro.quests.list_entries(quest_id)
    except Exception as e:
        logger.debug("entries fallback failed for %s: %s", quest_id, e)
        return 0
    return _count_external([entry.user_id for entry in entries], owner_user_id)


def collect_quest_outcome(
    ouro: Any,
    quest: Any,
    *,
    owner_user_id: str | None = None,
    impact_by_asset: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute a single quest's outcome rollup."""
    quest_id = str(read_field(quest, "id") or "")
    items = quest_items(quest)
    done = sum(1 for i in items if i.get("status") in ("done", "skipped"))
    produced: list[str] = []
    for item in items:
        produced.extend(_submission_asset_ids(item))
    # Deduplicate
    seen: set[str] = set()
    produced_ids = []
    for asset_id in produced:
        if asset_id not in seen:
            seen.add(asset_id)
            produced_ids.append(asset_id)

    totals = _zero_counts()
    for asset_id in produced_ids:
        if impact_by_asset and asset_id in impact_by_asset:
            _merge_metrics(totals, impact_by_asset[asset_id])
        else:
            _merge_metrics(
                totals, _fallback_asset_metrics(ouro, asset_id, owner_user_id)
            )

    totals["external_entries"] += _count_external_entries(
        ouro, quest_id, owner_user_id
    )

    return {
        "quest_id": quest_id,
        "name": str(read_field(quest, "name") or "Untitled"),
        "status": quest_status(quest) or "unknown",
        "created_at": str(read_field(quest, "created_at") or "")[:10],
        "items_resolved": done,
        "items_total": len(items),
        "produced_asset_ids": produced_ids,
        "metrics": totals,
    }


def format_outcome_line(outcome: dict[str, Any]) -> str:
    m = outcome.get("metrics") or _zero_counts()
    return (
        f"- {outcome.get('name')} "
        f"({outcome.get('status')}, {outcome.get('created_at') or 'unknown date'}): "
        f"items {outcome.get('items_resolved')}/{outcome.get('items_total')} resolved — "
        f"{m.get('external_comments', 0)} external comments, "
        f"{m.get('external_reactions', 0)} external reactions, "
        f"{m.get('quality_views', 0)} quality views, "
        f"{m.get('downloads', 0)} downloads, "
        f"{m.get('external_entries', 0)} quest entries from others"
        f"{'' if outcome.get('produced_asset_ids') else ' (no produced assets linked)'}"
    )


def build_outcome_evidence_context(
    agent: "OuroAgent", limit: int = 10
) -> str:
    """Per-quest engagement digest for planning retrospectives."""
    try:
        ouro = agent._get_ouro_client()
    except Exception:
        return ""
    if not ouro:
        return ""

    own_user_id = getattr(agent, "own_user_id", None)
    assets = search_own_quests(agent, limit=limit)
    if not assets:
        return ""

    # Gather produced asset ids first so we can batch the Impact API.
    quests: list[Any] = []
    all_produced: list[str] = []
    for asset in assets:
        try:
            quest = ouro.quests.retrieve(str(asset.id))
        except Exception:
            continue
        quests.append(quest)
        for item in quest_items(quest):
            all_produced.extend(_submission_asset_ids(item))

    impact_by_asset = _try_impact_api(ouro, list(dict.fromkeys(all_produced)))

    lines = [
        "## Outcome Evidence",
        "External engagement on work your recent quests produced. Use these "
        "outcomes to shape the next plan's focus when useful: completion without "
        "engagement is not success. Low engagement is a signal to change "
        "approach, not a reason to skip planning.",
    ]
    any_row = False
    for quest in quests:
        outcome = collect_quest_outcome(
            ouro,
            quest,
            owner_user_id=own_user_id,
            impact_by_asset=impact_by_asset,
        )
        lines.append(format_outcome_line(outcome))
        any_row = True

    if not any_row:
        return ""
    return "\n".join(lines)


def build_outcome_lessons_for_reflection(
    agent: "OuroAgent", limit: int = 8
) -> str:
    """Compact outcome summary suitable for reflection/dream prompts."""
    context = build_outcome_evidence_context(agent, limit=limit)
    if not context:
        return ""
    return (
        context
        + "\n\nWhen consolidating learnings, prefer outcome-based lessons "
        "(what got engagement / what got silence) over process lessons "
        "(pipeline completed cleanly)."
    )
