"""STANDING — global, bounded, self-expiring directives.

A single root-scope doc (``SHARED:standing`` → ``STANDING.md``) holding the
short list of directives that are *currently binding* across every team and
run mode: "Modal routes are paused until the controller says otherwise",
"do not post to team X this week", and so on.

It is deliberately not a feed and not a memory store:

- One doc, hard-capped in entries and characters, so its prompt cost is
  bounded by construction.
- Entries are edited and cleared, never appended-to-and-forgotten. Every
  entry carries ``since`` / ``from`` / ``until`` so a reader can tell whether
  it still applies.
- It is always loaded, in every team scope and every mode, alongside SOUL.
  Vector memory is query-driven and team memory is siloed; this is the one
  place a cross-team directive cannot be lost.

Writers: the reflector (controller directives heard in any run), the agent
(``standing_set`` / ``standing_clear`` tools), and the operator (CLI or a
text editor — the on-disk format is plain markdown).

On-disk format::

    # STANDING

    - [a1b2c3] since 2026-09-14 14:43Z · from @mmoderwell · until controller all-clear on post 01a0a05f
      Modal-backed routes are paused (credits). No route calls, deploys, or smoke tests.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STANDING_DOC = "SHARED:standing"

MAX_ENTRIES = 8
MAX_ENTRY_CHARS = 400
# Entries with no dated ``until`` are treated as overdue after this long, so
# a forgotten "until further notice" cannot pin the prompt forever.
DEFAULT_TTL_DAYS = 7

_HEADER = "# STANDING"
_INTRO = (
    "Currently-binding directives across every team and mode. Edited, not "
    "appended: clear an entry when its `until` condition is met."
)

_ENTRY_RE = re.compile(
    r"^- \[(?P<id>[0-9a-f]{6})\]\s+since\s+(?P<since>\S+(?: \S+)?)"
    r"\s+·\s+from\s+(?P<source>\S+)"
    r"(?:\s+·\s+until\s+(?P<until>.*))?$"
)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?Z?$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_ts(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _parse_ts(text: str) -> Optional[datetime]:
    match = _DATE_RE.match(text.strip())
    if not match:
        return None
    day = date.fromisoformat(match.group(1))
    hour = int(match.group(2) or 0)
    minute = int(match.group(3) or 0)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


@dataclass
class StandingEntry:
    id: str
    text: str
    since: str
    source: str
    until: str = ""

    @property
    def until_at(self) -> Optional[datetime]:
        """The ``until`` field as a datetime when it is a date, else ``None``."""
        return _parse_ts(self.until) if self.until else None

    @property
    def since_at(self) -> Optional[datetime]:
        return _parse_ts(self.since)

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        """Dated ``until`` in the past, or undated and older than the TTL."""
        now = now or _now()
        due = self.until_at
        if due is not None:
            return now >= due
        started = self.since_at
        if started is None:
            return False
        return now >= started + timedelta(days=DEFAULT_TTL_DAYS)

    def render(self) -> str:
        head = f"- [{self.id}] since {self.since} · from {self.source}"
        if self.until:
            head += f" · until {self.until}"
        return f"{head}\n  {self.text}"


@dataclass
class StandingDoc:
    entries: list[StandingEntry] = field(default_factory=list)

    def get(self, entry_id: str) -> Optional[StandingEntry]:
        needle = (entry_id or "").strip().lower()
        for entry in self.entries:
            if entry.id == needle:
                return entry
        return None


def parse_standing(markdown: str) -> StandingDoc:
    """Parse the on-disk markdown into entries; tolerant of hand edits."""
    doc = StandingDoc()
    current: Optional[StandingEntry] = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current, body
        if current is not None:
            current.text = _clean(" ".join(body))[:MAX_ENTRY_CHARS]
            if current.text:
                doc.entries.append(current)
        current, body = None, []

    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        match = _ENTRY_RE.match(line)
        if match:
            flush()
            current = StandingEntry(
                id=match.group("id"),
                text="",
                since=match.group("since").strip(),
                source=match.group("source").strip(),
                until=_clean(match.group("until") or ""),
            )
            continue
        if current is not None:
            if line.startswith("  ") or line.startswith("\t"):
                body.append(line.strip())
            elif line.strip() == "":
                continue
            else:
                # Anything else at column 0 ends the entry.
                flush()
    flush()
    return doc


def render_standing(doc: StandingDoc) -> str:
    parts = [_HEADER, "", _INTRO, ""]
    if not doc.entries:
        parts.append("(none)")
    else:
        parts.extend(entry.render() for entry in doc.entries)
    return "\n".join(parts).rstrip() + "\n"


def load_standing(doc_store) -> StandingDoc:
    if doc_store is None:
        return StandingDoc()
    try:
        return parse_standing(doc_store.read(STANDING_DOC))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", STANDING_DOC, exc)
        return StandingDoc()


def save_standing(doc_store, doc: StandingDoc) -> bool:
    if doc_store is None:
        return False
    try:
        return bool(doc_store.write(STANDING_DOC, render_standing(doc)))
    except Exception as exc:
        logger.warning("Failed to write %s: %s", STANDING_DOC, exc)
        return False


def _new_id(existing: set[str]) -> str:
    while True:
        candidate = secrets.token_hex(3)
        if candidate not in existing:
            return candidate


def _near_duplicate(a: str, b: str) -> bool:
    from .reflection import is_near_duplicate_text

    return is_near_duplicate_text(a, b)


def set_standing(
    doc_store,
    text: str,
    *,
    source: str,
    until: str = "",
    now: Optional[datetime] = None,
) -> tuple[Optional[StandingEntry], str]:
    """Add a directive, or refresh an existing near-duplicate in place.

    Returns ``(entry, error)``. ``error`` is non-empty when the write was
    refused (cap reached, empty text, store failure); the caller decides how
    to surface it.
    """
    now = now or _now()
    clean_text = _clean(text)
    if not clean_text:
        return None, "empty text"
    if len(clean_text) > MAX_ENTRY_CHARS:
        clean_text = clean_text[: MAX_ENTRY_CHARS - 1].rstrip() + "…"
    clean_until = _clean(until)
    clean_source = _clean(source) or "self"
    if " " in clean_source:
        clean_source = clean_source.replace(" ", "-")

    doc = load_standing(doc_store)

    for entry in doc.entries:
        if _near_duplicate(entry.text, clean_text):
            entry.text = clean_text
            if clean_until:
                entry.until = clean_until
            entry.source = clean_source or entry.source
            if not save_standing(doc_store, doc):
                return None, "failed to write STANDING"
            return entry, ""

    if len(doc.entries) >= MAX_ENTRIES:
        return None, (
            f"STANDING is at its cap of {MAX_ENTRIES} entries. Clear one that no "
            "longer applies (standing_clear) before adding another."
        )

    entry = StandingEntry(
        id=_new_id({e.id for e in doc.entries}),
        text=clean_text,
        since=_fmt_ts(now),
        source=clean_source,
        until=clean_until,
    )
    doc.entries.append(entry)
    if not save_standing(doc_store, doc):
        return None, "failed to write STANDING"
    return entry, ""


def clear_standing(doc_store, entry_id: str) -> tuple[Optional[StandingEntry], str]:
    """Remove one entry by id. Returns ``(removed, error)``."""
    doc = load_standing(doc_store)
    entry = doc.get(entry_id)
    if entry is None:
        return None, f"no STANDING entry with id {entry_id!r}"
    doc.entries = [e for e in doc.entries if e.id != entry.id]
    if not save_standing(doc_store, doc):
        return None, "failed to write STANDING"
    return entry, ""


def expire_standing(
    doc_store, *, now: Optional[datetime] = None, dry_run: bool = False
) -> list[StandingEntry]:
    """Drop entries whose dated ``until`` has passed.

    Undated entries are *not* removed here — only flagged as overdue in the
    prompt — because "until the controller says otherwise" is a real
    condition that a clock cannot settle. Dream hygiene turns those into a
    question for the controller instead.
    """
    now = now or _now()
    doc = load_standing(doc_store)
    expired = [e for e in doc.entries if e.until_at is not None and now >= e.until_at]
    if expired and not dry_run:
        doc.entries = [e for e in doc.entries if e not in expired]
        save_standing(doc_store, doc)
    return expired


PROMPT_RULES = (
    "These directives are currently binding across every team and mode. "
    "Unlike ordinary memory they are authoritative until their `until` "
    "condition is met — do not treat them as stale blockers to re-verify, and "
    "do not spend attempts, budgets, or verdict posts on failures they already "
    "explain. Check this list before executing routes, deploying, or firing a "
    "scheduled trigger. When a condition is satisfied (for example the "
    "controller gives the all-clear), call standing_clear with the entry id; "
    "when you learn a new cross-team constraint, call standing_set."
)


def format_standing_for_prompt(doc_store, *, now: Optional[datetime] = None) -> str:
    """Render the ``## STANDING`` body, or ``""`` when there is nothing binding."""
    doc = load_standing(doc_store)
    if not doc.entries:
        return ""
    now = now or _now()
    lines = [PROMPT_RULES, ""]
    for entry in doc.entries:
        rendered = entry.render()
        if entry.is_overdue(now):
            note = (
                "past its `until` date"
                if entry.until_at is not None
                else f"open for more than {DEFAULT_TTL_DAYS} days"
            )
            rendered += (
                f"\n  (overdue: {note} — confirm it still applies, then either "
                "clear it or refresh its `until`.)"
            )
        lines.append(rendered)
    return "\n".join(lines)
