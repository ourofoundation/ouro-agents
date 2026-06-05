"""Pure naming logic for memory docs.

Logical doc keys, team qualifiers, and the rules that turn either into the
Ouro-facing post title. Kept separate from the I/O classes so callers can
import naming utilities without dragging in the doc store implementations.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Literal, get_args

_TEAM_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Wire-format prefix for period logs.
LOG_PREFIX = "LOG"
LEGACY_LOG_PREFIX = "DAILY"

# Supported memory "rhythms" — how often the agent rolls its log and dreams.
# The rhythm determines the calendar window a single LOG_PREFIX doc covers.
# `Rhythm` is the single source of truth; `RHYTHMS` is derived from it so the
# config Literal and the runtime validation can never drift apart.
Rhythm = Literal["daily", "weekly", "biweekly"]
RHYTHMS: frozenset[str] = frozenset(get_args(Rhythm))

# Fixed Monday anchor so biweekly buckets stay stable across time and machines.
# 2024-01-01 was a Monday.
_BIWEEKLY_ANCHOR = date(2024, 1, 1)

_WEEKLY_KEY_RE = re.compile(r"^\d{4}-W\d{2}$")
_BIWEEKLY_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-2w$")


def is_log_prefix(prefix: str) -> bool:
    """Return True if *prefix* is the current or legacy period-log prefix."""
    return prefix in (LOG_PREFIX, LEGACY_LOG_PREFIX)


def canonical_log_name(name: str) -> str:
    """Map a period-log logical name to the canonical ``LOG:`` form."""
    if name.startswith(f"{LEGACY_LOG_PREFIX}:"):
        return f"{LOG_PREFIX}:{name[len(LEGACY_LOG_PREFIX) + 1:]}"
    return name


def legacy_log_name(name: str) -> str | None:
    """Return the legacy ``DAILY:`` alias for a period-log name, if applicable."""
    canonical = canonical_log_name(name)
    if canonical.startswith(f"{LOG_PREFIX}:"):
        return f"{LEGACY_LOG_PREFIX}:{canonical[len(LOG_PREFIX) + 1:]}"
    return None


def log_name_lookup_keys(name: str) -> list[str]:
    """Registry / cache lookup keys for a period-log name (canonical first)."""
    canonical = canonical_log_name(name)
    keys = [canonical]
    legacy = legacy_log_name(canonical)
    if legacy and legacy not in keys:
        keys.append(legacy)
    return keys


def normalize_rhythm(rhythm: str | None) -> str:
    """Coerce an arbitrary value to a supported rhythm, defaulting to daily."""
    value = (rhythm or "daily").strip().lower()
    return value if value in RHYTHMS else "daily"


def store_rhythm(doc_store) -> str:
    """Read the configured rhythm off a doc store (defaults to daily)."""
    return normalize_rhythm(getattr(doc_store, "rhythm", "daily"))


def _period_start(rhythm: str, day: date) -> date:
    """First calendar day of the period containing ``day``."""
    rhythm = normalize_rhythm(rhythm)
    if rhythm == "weekly":
        return day - timedelta(days=day.weekday())
    if rhythm == "biweekly":
        monday = day - timedelta(days=day.weekday())
        weeks = (monday - _BIWEEKLY_ANCHOR).days // 7
        return _BIWEEKLY_ANCHOR + timedelta(weeks=(weeks // 2) * 2)
    return day


def period_key(rhythm: str, day: date | None = None) -> str:
    """Canonical bucket key for the period containing ``day``.

    daily    -> ``2026-06-02``
    weekly   -> ``2026-W23`` (ISO week)
    biweekly -> ``2026-06-01-2w`` (Monday starting a 2-week window)
    """
    rhythm = normalize_rhythm(rhythm)
    day = day or date.today()
    if rhythm == "weekly":
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if rhythm == "biweekly":
        return f"{_period_start('biweekly', day).isoformat()}-2w"
    return day.isoformat()


def period_key_offset(rhythm: str, periods: int, day: date | None = None) -> str:
    """Period key shifted by ``periods`` (e.g. ``-1`` = previous period)."""
    rhythm = normalize_rhythm(rhythm)
    start = _period_start(rhythm, day or date.today())
    if rhythm == "weekly":
        shifted = start + timedelta(weeks=periods)
    elif rhythm == "biweekly":
        shifted = start + timedelta(weeks=2 * periods)
    else:
        shifted = start + timedelta(days=periods)
    return period_key(rhythm, shifted)


def period_label_from_key(key: str) -> str:
    """Infer the rhythm adjective ("daily"/"weekly"/"biweekly") from a key."""
    if _WEEKLY_KEY_RE.match(key):
        return "weekly"
    if _BIWEEKLY_KEY_RE.match(key) or key.endswith("-2w"):
        return "biweekly"
    return "daily"


def period_log_title(rhythm: str) -> str:
    """Heading used at the top of a freshly created log doc."""
    return {
        "weekly": "Weekly Log",
        "biweekly": "Biweekly Log",
    }.get(normalize_rhythm(rhythm), "Daily Log")


def current_period_heading(rhythm: str) -> str:
    """Section heading used when injecting the current log into the prompt."""
    return {
        "weekly": "This Week's Log",
        "biweekly": "This Period's Log",
    }.get(normalize_rhythm(rhythm), "Today's Log")

# Identity prefixes are pinned to the local filesystem — they're per-machine
# state (agent identity, machine-local notes) and never persist to Ouro.
IDENTITY_PREFIXES: frozenset[str] = frozenset(
    {"SOUL", "HEARTBEAT", "NOTES", "SHARED"}
)

# Singleton prefixes name a single doc per (agent, team[, day]) tuple. The
# resolver refuses to pick a winner from ambiguous exact-name matches for
# these — duplicates indicate a real bug we want to surface.
SINGLETON_PREFIXES: frozenset[str] = frozenset(
    {"SOUL", "NOTES", "HEARTBEAT", "MEMORY", LOG_PREFIX, "USER", "SHARED"}
)


def slugify_team_key(value: str) -> str:
    """Normalize a team label for use in canonical doc names."""
    lowered = value.strip().lower()
    if not lowered:
        return ""
    return _TEAM_SLUG_RE.sub("-", lowered).strip("-")


def team_doc_key(
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Resolve the canonical team qualifier used in MEMORY/log doc names."""
    for candidate in (team_slug, team_name, team_id):
        if not candidate:
            continue
        normalized = slugify_team_key(candidate)
        if normalized:
            return normalized
    return ""


def memory_doc_name(
    agent_name: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical working-memory doc name."""
    qualifier = team_doc_key(team_slug=team_slug, team_name=team_name, team_id=team_id)
    if qualifier:
        return f"MEMORY:{agent_name}:{qualifier}"
    return f"MEMORY:{agent_name}"


def log_doc_name(
    agent_name: str,
    period: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical period-log doc name (wire prefix ``LOG_PREFIX``)."""
    qualifier = team_doc_key(team_slug=team_slug, team_name=team_name, team_id=team_id)
    if qualifier:
        return f"{LOG_PREFIX}:{agent_name}:{qualifier}:{period}"
    return f"{LOG_PREFIX}:{agent_name}:{period}"


def log_doc_display_name(name: str) -> str:
    """Return the human-facing Ouro post title for a period-log logical key."""
    parts = name.split(":")
    if not parts or not is_log_prefix(parts[0]):
        return name

    period = parts[-1]
    label = period_label_from_key(period)
    if len(parts) >= 4:
        qualifier = team_doc_key(team_slug=parts[2])
        if qualifier:
            return f"#{qualifier} {label} log {period}"
    if len(parts) >= 3:
        agent = parts[1].strip()
        owner = f"{agent} " if agent else ""
        return f"{owner}{label} log {period}"
    return name


def remote_display_name(name: str) -> str:
    """Map an internal doc key to the title shown on Ouro."""
    if is_log_prefix(name.split(":", 1)[0]):
        return log_doc_display_name(name)
    return name


def is_singleton_name(name: str) -> bool:
    """Return True for registry-first named memory docs."""
    return name.split(":", 1)[0] in SINGLETON_PREFIXES


def is_identity_name(name: str) -> bool:
    """Return True for docs that always live on the local filesystem."""
    return name.split(":", 1)[0] in IDENTITY_PREFIXES
