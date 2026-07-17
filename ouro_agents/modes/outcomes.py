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
from typing import TYPE_CHECKING, Any, Optional

from ..syncing import read_field
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
    flat = " ".join(str(text or "").split())
    return flat[: max_len - 1] + "…" if len(flat) > max_len else flat


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
    ouro: Any, user_id: str | None, asset_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Return per-asset impact maps, or None if the API is unavailable."""
    if not ouro or not asset_ids:
        return None

    # Prefer batch assets.impact when present.
    assets_api = getattr(ouro, "assets", None)
    impact_batch = getattr(assets_api, "impact", None) or getattr(
        assets_api, "impact_batch", None
    )
    if callable(impact_batch):
        try:
            raw = impact_batch(asset_ids)
            return _normalize_impact_payload(raw)
        except Exception as e:
            logger.debug("assets.impact unavailable: %s", e)

    users_api = getattr(ouro, "users", None)
    user_impact = getattr(users_api, "impact", None)
    if callable(user_impact) and user_id:
        try:
            raw = user_impact(user_id, asset_ids=asset_ids)
            return _normalize_impact_payload(raw)
        except Exception as e:
            logger.debug("users.impact unavailable: %s", e)

    return None


def _normalize_impact_payload(raw: Any) -> dict[str, dict[str, Any]]:
    """Normalize Impact API responses into ``{asset_id: metrics}``."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        assets = raw.get("assets") or raw.get("data") or raw
        if isinstance(assets, list):
            out: dict[str, dict[str, Any]] = {}
            for row in assets:
                if not isinstance(row, dict):
                    continue
                asset_id = str(row.get("asset_id") or row.get("id") or "")
                if asset_id:
                    out[asset_id] = row
            return out
        if isinstance(assets, dict):
            # Already keyed by asset id, or a single metrics blob.
            if any(isinstance(v, dict) for v in assets.values()):
                return {
                    str(k): v for k, v in assets.items() if isinstance(v, dict)
                }
    if isinstance(raw, list):
        out = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            asset_id = str(row.get("asset_id") or row.get("id") or "")
            if asset_id:
                out[asset_id] = row
        return out
    return {}


def _fallback_asset_metrics(
    ouro: Any, asset_id: str, owner_user_id: str | None
) -> dict[str, int]:
    """Best-effort metrics via counts + comment authorship."""
    metrics = _zero_counts()
    assets_api = getattr(ouro, "assets", None)
    counts_fn = getattr(assets_api, "counts", None)
    if callable(counts_fn):
        try:
            counts = counts_fn(asset_id) or {}
            metrics["views"] = int(counts.get("views") or 0)
            metrics["comments"] = int(counts.get("comments") or 0)
            metrics["reactions"] = int(counts.get("reactions") or 0)
            metrics["downloads"] = int(counts.get("downloads") or 0)
            # Without the Impact API we cannot filter bots; treat views as
            # a weak quality proxy.
            metrics["quality_views"] = metrics["views"]
        except Exception as e:
            logger.debug("counts failed for %s: %s", asset_id, e)

    # External comments: count comment assets under this parent not by owner.
    get_comments = None
    for path in (
        getattr(getattr(ouro, "comments", None), "list", None),
        getattr(getattr(ouro, "assets", None), "comments", None),
    ):
        if callable(path):
            get_comments = path
            break
    # MCP-shaped client may expose get via quests/posts — also try generic.
    if get_comments is None:
        retrieve_comments = getattr(getattr(ouro, "comments", None), "get", None)
        if callable(retrieve_comments):
            get_comments = retrieve_comments

    if callable(get_comments):
        try:
            raw = get_comments(asset_id)
            if isinstance(raw, dict):
                raw = raw.get("data") or raw.get("comments") or raw.get("results") or []
            external = 0
            for comment in raw or []:
                if not isinstance(comment, dict):
                    author = str(read_field(comment, "user_id") or "")
                else:
                    author = str(
                        comment.get("user_id")
                        or (comment.get("user") or {}).get("user_id")
                        or ""
                    )
                if owner_user_id and author and author == str(owner_user_id):
                    continue
                if author:
                    external += 1
            metrics["external_comments"] = external
        except TypeError:
            # Some clients want parent_id= kwarg.
            try:
                raw = get_comments(parent_id=asset_id)
                if isinstance(raw, dict):
                    raw = (
                        raw.get("data")
                        or raw.get("comments")
                        or raw.get("results")
                        or []
                    )
                external = 0
                for comment in raw or []:
                    author = str(
                        (comment.get("user_id") if isinstance(comment, dict) else "")
                        or read_field(comment, "user_id")
                        or ""
                    )
                    if owner_user_id and author and author == str(owner_user_id):
                        continue
                    if author:
                        external += 1
                metrics["external_comments"] = external
            except Exception as e:
                logger.debug("comment fallback failed for %s: %s", asset_id, e)
        except Exception as e:
            logger.debug("comment fallback failed for %s: %s", asset_id, e)

    # If we couldn't attribute comments, fall back to total comments as an
    # upper bound on external engagement (conservative for "zero" detection
    # only when both are zero).
    if metrics["external_comments"] == 0 and metrics["comments"] > 0:
        # Unknown authorship — report comments but keep external at 0 so
        # planning doesn't celebrate self-replies as engagement.
        pass

    return metrics


def _merge_metrics(into: dict[str, int], row: dict[str, Any]) -> None:
    mapping = {
        "views": ("views", "views_count"),
        "comments": ("comments", "comments_count"),
        "reactions": ("reactions", "reactions_count"),
        "downloads": ("downloads", "downloads_count"),
        "external_comments": ("external_comments", "comments_external"),
        "external_reactions": ("external_reactions", "reactions_external"),
        "quality_views": ("quality_views", "views_quality"),
        "external_entries": ("external_entries", "entries_external"),
    }
    for key, aliases in mapping.items():
        for alias in aliases:
            if alias in row and row[alias] is not None:
                into[key] += int(row[alias] or 0)
                break


def _count_external_entries(
    ouro: Any, quest_id: str, owner_user_id: str | None
) -> int:
    quests_api = getattr(ouro, "quests", None)
    list_entries = getattr(quests_api, "list_entries", None) or getattr(
        quests_api, "entries", None
    )
    if not callable(list_entries):
        return 0
    try:
        raw = list_entries(quest_id)
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("entries") or raw.get("results") or []
        count = 0
        for entry in raw or []:
            author = str(
                (entry.get("user_id") if isinstance(entry, dict) else "")
                or read_field(entry, "user_id")
                or ""
            )
            if owner_user_id and author == str(owner_user_id):
                continue
            if author:
                count += 1
        return count
    except Exception as e:
        logger.debug("entries fallback failed for %s: %s", quest_id, e)
        return 0


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
        quest_id = str(asset.get("id") or "")
        if not quest_id:
            continue
        try:
            quest = ouro.quests.retrieve(quest_id)
        except Exception:
            continue
        quests.append(quest)
        for item in quest_items(quest):
            all_produced.extend(_submission_asset_ids(item))

    impact_by_asset = _try_impact_api(ouro, own_user_id, list(dict.fromkeys(all_produced)))

    lines = [
        "## Outcome Evidence",
        "External engagement on work your recent quests produced. Grade plans "
        "against these outcomes — completion without engagement is not success. "
        "A pattern with repeated near-zero external engagement must be named as "
        "failing and changed.",
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
