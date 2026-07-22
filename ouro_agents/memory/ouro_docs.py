"""Document stores for shared agent memory.

Three classes:

- ``OuroDocStore``: pure Ouro client. Reads/writes/comments on posts.
  Resolves logical names to UUIDs via a file-backed registry; falls back to
  exact-name search once on a miss, then caches.
- ``LocalDocStore``: pure file-backed store with one canonical layout.
  Identity at the workspace root, team docs under ``teams/{team_id}/``,
  shared docs under ``shared/...`` when no team is set.
- ``CompositeDocStore``: routes by name prefix. ``SOUL``/``HEARTBEAT``/``NOTES``
  always go local (per-machine identity); everything else goes to Ouro when
  available, otherwise local.

``workspace_sync`` is the only bridge that copies team ``MEMORY.md`` between
disk and Ouro at startup.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol

from .frontmatter import strip_frontmatter
from .naming import (
    IDENTITY_PREFIXES,
    LOG_PREFIX,
    canonical_log_name,
    is_log_prefix,
    is_singleton_name,
    legacy_log_name,
    log_doc_display_name,
    log_doc_name,
    log_name_lookup_keys,
    memory_doc_name,
    remote_display_name,
    slugify_team_key,
    team_doc_key,
)

if TYPE_CHECKING:
    from ouro import Ouro
    from ouro.resources.content import Content

logger = logging.getLogger(__name__)


_LIST_ITEM_RE = re.compile(r"^\s*[-*] ")


def _append_markdown_list_item(existing: str, addition: str) -> str:
    """Merge a markdown list item into the current trailing list."""
    existing = existing.rstrip()
    addition = addition.strip()
    if not existing:
        return addition
    if not addition:
        return existing

    separator = "\n" if _LIST_ITEM_RE.match(addition) else "\n\n"
    return f"{existing}{separator}{addition}"


def _requires_owned_cache(name: str) -> bool:
    """Return True for docs that must only use agent-owned registry entries."""
    return is_log_prefix(name.split(":", 1)[0])


def _visibility_for_doc(name: str) -> str:
    """Choose the Ouro visibility for a newly created memory document."""
    prefix = name.split(":", 1)[0]
    if prefix == "MEMORY" or is_log_prefix(prefix):
        return "private"
    return "organization"


_STALE_UUID_HINTS = (
    "cannot coerce the result to a single",
    "not found",
    "404",
    "403",
    "permission",
    "not allowed",
    "forbidden",
)


def _looks_like_stale_uuid(exc: Exception) -> bool:
    """Return True when *exc* suggests a registry UUID points to a tombstone.

    Covers the two cases observed against the Ouro backend: PostgREST's
    ``Cannot coerce the result to a single JSON object`` (returned when
    ``.single()`` finds zero rows under RLS, e.g. the post was deleted) and
    explicit permission errors on update of a row whose ACL changed. In both
    cases the cached UUID is unusable and re-resolution via search/create is
    the right next step.
    """
    msg = str(exc).lower()
    return any(hint in msg for hint in _STALE_UUID_HINTS)


@dataclass
class ReadResult:
    """Content + metadata from a doc-store read."""

    content: str
    last_updated: Optional[datetime] = None
    post_id: Optional[str] = None


class DocStore(Protocol):
    """Interface for document stores (Ouro-backed or local filesystem)."""

    rhythm: str

    def read(self, name: str) -> str: ...
    def write(self, name: str, content_md: str) -> bool: ...
    def append(self, name: str, markdown: str) -> bool: ...
    def append_list_item(
        self,
        name: str,
        markdown_item: str,
        *,
        initial_md: str | None = None,
    ) -> bool: ...
    def exists(self, name: str) -> bool: ...
    def comment(self, name: str, content_md: str) -> bool: ...
    def read_comments(self, name: str) -> list[dict]: ...
    def search(self, query: str) -> list[dict]: ...
    def is_owner(self, name: str) -> bool: ...
    def memory_name(self, agent_name: str | None = None) -> str: ...
    def log_name(self, agent_name: str | None, period: str) -> str: ...


def _build_client(api_key: str | None = None, base_url: str | None = None) -> "Ouro":
    """Create an Ouro client from explicit creds or environment."""
    import os

    from ouro import Ouro

    key = api_key or os.getenv("OURO_API_KEY")
    url = base_url or os.getenv("OURO_BASE_URL")
    if not key:
        raise RuntimeError("OURO_API_KEY required for OuroDocStore")
    return Ouro(api_key=key, base_url=url)


class OuroDocStore:
    """Thin wrapper over ouro-py for reading/writing named posts."""

    def __init__(
        self,
        agent_name: str,
        org_id: str,
        team_id: str,
        client: Optional["Ouro"] = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        registry_path: Optional[Path] = None,
        team_slug: str | None = None,
        team_name: str | None = None,
        rhythm: str = "daily",
    ):
        self.agent_name = agent_name
        self.org_id = org_id
        self.team_id = team_id
        self.team_name = team_name or ""
        self.team_slug = team_doc_key(team_slug=team_slug, team_id=team_id)
        self.rhythm = rhythm
        self._client = client or _build_client(api_key, base_url)
        self._owner_cache: set[str] = set()
        self._write_lock = threading.RLock()

        self._registry_path = registry_path
        # Populated by _load_registry as a side effect alongside _uuid_cache.
        self._uuid_cache: dict[str, str] = self._load_registry()

    def memory_name(self, agent_name: str | None = None) -> str:
        """Canonical MEMORY name for this store's scope."""
        return memory_doc_name(
            agent_name or self.agent_name,
            team_slug=self.team_slug,
            team_id=self.team_id,
        )

    def log_name(self, agent_name: str | None, period: str) -> str:
        """Canonical period-log name for this store's scope."""
        return log_doc_name(
            agent_name or self.agent_name,
            period,
            team_slug=self.team_slug,
            team_id=self.team_id,
        )

    # -- Registry persistence -------------------------------------------------

    def _load_registry(self) -> dict[str, str]:
        """Load the name→UUID registry from disk (or return empty).

        Also populates ``self._owner_cache`` and refreshes team metadata as
        side effects so durable ownership survives restarts.

        Each ``docs`` entry is either a bare uuid string (legacy / non-owned)
        or a ``{"uuid": "...", "owned": true}`` object. Reads accept both;
        writes always emit the object form.
        """
        if not self._registry_path or not self._registry_path.exists():
            return {}
        try:
            data = json.loads(self._registry_path.read_text())
            if not isinstance(data, dict):
                return {}
            docs = data.get("docs")
            if not isinstance(docs, dict):
                return {}

            entries: dict[str, str] = {}
            for key, value in docs.items():
                if not isinstance(key, str):
                    continue
                if isinstance(value, str):
                    entries[key] = value
                elif isinstance(value, dict):
                    uuid = value.get("uuid")
                    if isinstance(uuid, str):
                        entries[key] = uuid
                        if value.get("owned") is True:
                            self._owner_cache.add(key)

            team = data.get("team")
            if isinstance(team, dict):
                self.team_name = str(team.get("name") or self.team_name or "")
                self.team_slug = team_doc_key(
                    team_slug=str(team.get("slug") or ""),
                    team_name=self.team_name,
                    team_id=str(team.get("id") or self.team_id),
                )
            logger.debug(
                "Loaded doc registry: %d entries (%d owned)",
                len(entries),
                len(self._owner_cache),
            )
            return entries
        except Exception as e:
            logger.warning("Failed to load doc registry: %s", e)
            return {}

    def _save_registry(self) -> None:
        """Persist the name→UUID cache (and ownership) to disk."""
        if not self._registry_path:
            return
        try:
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            docs_payload: dict[str, dict] = {}
            for name, uuid in self._uuid_cache.items():
                entry: dict = {"uuid": uuid}
                if name in self._owner_cache:
                    entry["owned"] = True
                docs_payload[name] = entry
            payload = {
                "team": {
                    "id": self.team_id,
                    "name": self.team_name,
                    "slug": self.team_slug,
                    "org_id": self.org_id,
                },
                "docs": docs_payload,
            }
            self._registry_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning("Failed to save doc registry: %s", e)

    def _remember_uuid(self, name: str, uuid: str) -> str:
        """Cache and persist a resolved UUID for future exact lookups."""
        canonical = canonical_log_name(name)
        self._uuid_cache[canonical] = uuid
        legacy = legacy_log_name(canonical)
        if legacy and legacy in self._uuid_cache:
            self._uuid_cache.pop(legacy, None)
            self._owner_cache.discard(legacy)
        self._save_registry()
        return uuid

    def _forget_uuid(self, name: str) -> None:
        """Remove a stale registry entry and persist the change."""
        self._uuid_cache.pop(name, None)
        self._owner_cache.discard(name)
        self._save_registry()

    def _cached_uuid(self, name: str, *, require_owned: bool = False) -> Optional[str]:
        """Return a usable cached UUID, optionally requiring ownership."""
        for key in (
            log_name_lookup_keys(name)
            if is_log_prefix(name.split(":", 1)[0])
            else [name]
        ):
            uuid = self._uuid_cache.get(key)
            if not uuid:
                continue
            if not require_owned or key in self._owner_cache:
                return uuid
            logger.warning(
                "Ignoring non-owned cached UUID for %s (%s); will create owned doc",
                key,
                uuid,
            )
            self._forget_uuid(key)
        return None

    def _drop_stale_uuid(
        self,
        name: str,
        uuid: str,
        exc: Exception,
        already_recovered: bool,
        *,
        op: str,
    ) -> bool:
        """Forget *uuid* and signal a single retry when *exc* looks stale.

        Returns True iff the caller should retry the operation. Only retries
        once per call chain (gated by ``already_recovered``) and only when
        the registry still has the same UUID we tried to use, so a concurrent
        recovery elsewhere doesn't double-evict.
        """
        if already_recovered:
            return False
        if not _looks_like_stale_uuid(exc):
            return False
        if self._uuid_cache.get(name) != uuid:
            return False
        logger.warning(
            "OuroDocStore.%s: dropping stale registry entry for %s (%s): %s",
            op,
            name,
            uuid,
            exc,
        )
        self._forget_uuid(name)
        return True

    # -- Search + resolution -------------------------------------------------

    @staticmethod
    def _coerce_timestamp(value) -> Optional[datetime]:
        """Normalize search result timestamps for duplicate resolution."""
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _search_exact_name_matches(self, name: str, *, limit: int = 25) -> list[dict]:
        """Search this agent's posts whose remote title exactly matches *name*'s display."""
        remote_name = remote_display_name(name)
        results = self._client.assets.search(
            query=remote_name,
            asset_type="post",
            scope="personal",
            team_id=self.team_id,
            limit=limit,
        )
        if not isinstance(results, list):
            return []
        return [item for item in results if item.get("name", "") == remote_name]

    def _select_exact_match_item(
        self, name: str, matches: list[dict]
    ) -> Optional[dict]:
        """Pick the most recent exact match item when duplicates already exist."""
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        def sort_key(item: dict) -> tuple[bool, datetime]:
            ts = self._coerce_timestamp(
                item.get("last_updated")
                or item.get("updated_at")
                or item.get("created_at")
            )
            return (ts is not None, ts or datetime.min.replace(tzinfo=timezone.utc))

        selected = max(matches, key=sort_key)
        logger.warning(
            "Multiple exact post matches found for %s; using %s",
            name,
            selected.get("id"),
        )
        return selected

    def _resolve_name(self, name: str) -> tuple[Optional[str], bool]:
        """Resolve a logical doc name to ``(uuid, ambiguous)``.

        Cache hit short-circuits. Otherwise one exact-name search. For
        singleton prefixes (``MEMORY``/``LOG``/``USER``/etc.) refuses to
        pick a winner when multiple exact matches exist; the caller is
        expected to surface or clean up the duplicate.
        """
        owned_only = _requires_owned_cache(name)
        cached = self._cached_uuid(name, require_owned=owned_only)
        if cached:
            return cached, False
        if owned_only:
            return None, False

        lookup_names = (
            log_name_lookup_keys(name)
            if is_log_prefix(name.split(":", 1)[0])
            else [name]
        )
        for lookup_name in lookup_names:
            try:
                matches = self._search_exact_name_matches(lookup_name, limit=25)
            except Exception as e:
                logger.warning(
                    "OuroDocStore._resolve failed for %s: %s", lookup_name, e
                )
                continue

            if not matches:
                continue

            if is_singleton_name(lookup_name) and len(matches) > 1:
                logger.warning(
                    "Multiple exact singleton post matches found for %s; refusing recovery",
                    lookup_name,
                )
                return None, True

            selected = self._select_exact_match_item(lookup_name, matches)
            if not selected:
                continue

            uuid = str(selected["id"])
            self._remember_uuid(lookup_name, uuid)
            return uuid, False

        return None, False

    def _resolve(self, name: str) -> Optional[str]:
        """Resolve a post name to its UUID."""
        uuid, _ambiguous = self._resolve_name(name)
        return uuid

    def _resolve_or_create(
        self, name: str, initial_md: str
    ) -> tuple[Optional[str], bool]:
        """Resolve *name*, creating the post with *initial_md* if missing.

        Returns ``(uuid, created)``. ``uuid`` is None only when recovery hit
        an ambiguous singleton match (multiple exact matches) — the caller
        should treat that as a hard failure rather than retry. The double-
        check under ``_write_lock`` guards against concurrent creation
        within the same process.
        """
        uuid, ambiguous = self._resolve_name(name)
        if uuid is not None:
            return uuid, False
        if ambiguous:
            logger.warning(
                "Refusing to create %s: recovery found multiple exact matches",
                name,
            )
            return None, False

        with self._write_lock:
            uuid, ambiguous = self._resolve_name(name)
            if uuid is not None:
                return uuid, False
            if ambiguous:
                logger.warning(
                    "Refusing to create %s: recovery found multiple exact matches",
                    name,
                )
                return None, False
            uuid = self._create(name, initial_md)
            return uuid, uuid is not None

    # -- Content helpers -----------------------------------------------------

    def _make_content(self, markdown: str) -> "Content":
        """Build a Content object from markdown using the SDK's server-side parser."""
        content = self._client.posts.Content()
        content.from_markdown(markdown)
        return content

    def _content_to_markdown(self, post_content) -> str:
        """Render an Ouro post Content payload back to markdown.

        Handles three shapes the SDK can hand us back: a Content instance
        (use its ``to_markdown``), a payload object with a plain ``.text``
        attribute, or raw ``json``/``text`` fields that need wrapping in a
        new ``Content``.
        """
        if not post_content:
            return ""
        if hasattr(post_content, "to_markdown"):
            return post_content.to_markdown().strip()
        text = getattr(post_content, "text", None)
        if isinstance(text, str):
            return text.strip()
        from ouro.resources.content import Content as ContentCls

        c = ContentCls(
            json=post_content.data,
            text=post_content.text,
            _ouro=self._client,
        )
        return c.to_markdown().strip()

    def _create(self, name: str, content_md: str) -> Optional[str]:
        """Create a new post and return its UUID."""
        try:
            post = self._client.posts.create(
                name=remote_display_name(name),
                content_markdown=content_md,
                org_id=self.org_id,
                team_id=self.team_id,
                visibility=_visibility_for_doc(name),
            )
            uuid = str(post.id)
            self._uuid_cache[name] = uuid
            self._owner_cache.add(name)
            self._save_registry()
            return uuid
        except Exception as e:
            logger.warning("OuroDocStore._create failed for %s: %s", name, e)
            return None

    # -- Public API ----------------------------------------------------------

    def read(self, name: str) -> str:
        """Read a post by name. Returns empty string if not found."""
        return self.read_with_meta(name).content

    def read_with_meta(self, name: str, *, _recovered: bool = False) -> ReadResult:
        """Read a post by name, returning content and metadata.

        If the cached UUID points to a tombstone (post deleted, ACL
        revoked), drops the registry entry and retries once via the
        search path. Guarded by ``_recovered`` to prevent loops.
        """
        uuid = self._resolve(name)
        if not uuid:
            return ReadResult(content="")

        try:
            post = self._client.posts.retrieve(uuid)
            return ReadResult(
                content=self._content_to_markdown(post.content),
                last_updated=post.last_updated,
                post_id=str(post.id),
            )
        except Exception as e:
            if self._drop_stale_uuid(name, uuid, e, _recovered, op="read_with_meta"):
                return self.read_with_meta(name, _recovered=True)
            logger.warning("OuroDocStore.read_with_meta failed for %s: %s", name, e)
            return ReadResult(content="")

    def write(self, name: str, content_md: str, *, _recovered: bool = False) -> bool:
        """Update a post this agent owns. Creates it if it doesn't exist.

        On a stale-UUID error from update (post deleted out from under us,
        or ownership rescinded) the registry entry is dropped and the call
        is retried once — which falls through to ``_resolve_or_create``
        and creates a fresh owned post when no exact-name match exists.
        """
        uuid, created = self._resolve_or_create(name, content_md)
        if uuid is None:
            return False
        if created:
            return True

        try:
            self._client.posts.update(id=uuid, content=self._make_content(content_md))
            return True
        except Exception as e:
            if self._drop_stale_uuid(name, uuid, e, _recovered, op="write"):
                return self.write(name, content_md, _recovered=True)
            logger.warning("OuroDocStore.write failed for %s: %s", name, e)
            return False

    def append(self, name: str, markdown: str, *, _recovered: bool = False) -> bool:
        """Append markdown to an existing post (or create it).

        Works at the Content/TipTap level so rich formatting is preserved
        — no read→concat→rewrite lossy round-trip. Self-heals on stale
        UUIDs the same way ``write`` does.
        """
        uuid, created = self._resolve_or_create(name, markdown)
        if uuid is None:
            return False
        if created:
            return True

        try:
            post = self._client.posts.retrieve(uuid)
            if post.content:
                from ouro.resources.content import Content

                existing = Content(
                    json=post.content.data,
                    text=post.content.text,
                    _ouro=self._client,
                )
            else:
                existing = self._client.posts.Content()

            existing.append(self._make_content(markdown))
            self._client.posts.update(id=uuid, content=existing)
            return True
        except Exception as e:
            if self._drop_stale_uuid(name, uuid, e, _recovered, op="append"):
                return self.append(name, markdown, _recovered=True)
            logger.warning("OuroDocStore.append failed for %s: %s", name, e)
            return False

    def append_list_item(
        self,
        name: str,
        markdown_item: str,
        *,
        initial_md: str | None = None,
        _recovered: bool = False,
    ) -> bool:
        """Append a markdown list item, creating the post when missing.

        When the post already exists, reads its current content and rewrites
        with the item merged into the trailing list. When it doesn't exist:
        creates with *initial_md* if supplied, otherwise with the item alone.

        On Ouro this is owned-cache-first: an owned registry hit goes straight
        to a retrieve+update with no search round-trip. On a miss (or a stale
        non-owned cache entry) we create directly because this process owns
        list-style docs such as daily logs. On a stale-UUID error we drop the
        registry entry and retry once via the create path.
        """
        uuid = self._cached_uuid(name, require_owned=True)
        if not uuid:
            return self._create(name, initial_md or markdown_item) is not None

        try:
            post = self._client.posts.retrieve(uuid)
            current = self._content_to_markdown(post.content)
            new_md = _append_markdown_list_item(current, markdown_item)
            self._client.posts.update(id=uuid, content=self._make_content(new_md))
            return True
        except Exception as e:
            if self._drop_stale_uuid(name, uuid, e, _recovered, op="append_list_item"):
                return self.append_list_item(
                    name,
                    markdown_item,
                    initial_md=initial_md,
                    _recovered=True,
                )
            logger.warning(
                "OuroDocStore.append_list_item failed for %s (%s): %s",
                name,
                uuid,
                e,
            )
            return False

    def comment(self, name: str, content_md: str) -> bool:
        """Add a comment to a post (typically one this agent does NOT own)."""
        uuid = self._resolve(name)
        if not uuid:
            return False

        try:
            content = self._make_content(content_md)
            self._client.comments.create(content=content, parent_id=uuid)
            return True
        except Exception as e:
            logger.warning("OuroDocStore.comment failed for %s: %s", name, e)
            return False

    def read_comments(self, name: str) -> list[dict]:
        """Read comments on a post (for dream cycle consolidation)."""
        uuid = self._resolve(name)
        if not uuid:
            return []

        try:
            comments = self._client.comments.list_by_parent(uuid)
            return [c.model_dump(mode="json") for c in comments]
        except Exception as e:
            logger.warning("OuroDocStore.read_comments failed for %s: %s", name, e)
            return []

    def is_owner(self, name: str) -> bool:
        """Return True only for posts this process created in the current run.

        Used by ``user_model``/``dream`` to choose between writing
        directly and contributing via comment. Process-local by design — on
        restart we intentionally fall back to commenting until the dream cycle
        runs.
        """
        return name in self._owner_cache

    def search(self, query: str) -> list[dict]:
        """Search posts in the team."""
        try:
            results = self._client.assets.search(
                query=query,
                asset_type="post",
                team_id=self.team_id,
                limit=20,
            )
            return results if isinstance(results, list) else []
        except Exception as e:
            logger.warning("OuroDocStore.search failed: %s", e)
            return []

    def exists(self, name: str) -> bool:
        """Check whether a named post exists in the team."""
        return self._resolve(name) is not None


class LocalDocStore:
    """File-backed document store mapping post names to local workspace files.

    Provides the same interface as ``OuroDocStore`` so consumers never need
    to branch on which backend is active.

    Layout:

    - ``SOUL`` → ``{workspace}/SOUL.md`` (always at workspace root)
    - ``SHARED:memory`` → ``{workspace}/MEMORY.md`` (cross-team shared notes,
      always at workspace root regardless of team scope)
    - With ``team_id`` set:
        - ``MEMORY`` → ``teams/{team_id}/MEMORY.md``
        - ``LOG:*:*:{period}`` → ``teams/{team_id}/logs/{period}.md`` (legacy ``daily/`` read fallback)
        - ``HEARTBEAT``/``NOTES`` → ``teams/{team_id}/{prefix}.md``
    - Without ``team_id``:
        - ``HEARTBEAT``/``NOTES`` → workspace root
        - ``MEMORY`` → ``shared/memory/MEMORY.md``
        - ``LOG`` → ``shared/logs/{period}.md`` (legacy ``shared/daily/`` read fallback)
        - ``USER:{user_id}`` → ``shared/users/{user_id}.md``
    - Anything else → ``data/docs/{safe_name}.md``
    """

    def __init__(
        self,
        workspace: Path,
        agent_name: str = "",
        team_id: str | None = None,
        team_slug: str | None = None,
        rhythm: str = "daily",
    ):
        self._workspace = workspace
        self.agent_name = agent_name
        self.team_id = team_id
        self.team_slug = team_doc_key(team_slug=team_slug, team_id=team_id)
        self.rhythm = rhythm

    def memory_name(self, agent_name: str | None = None) -> str:
        """Canonical MEMORY name for this store's scope."""
        return memory_doc_name(
            agent_name or self.agent_name,
            team_slug=self.team_slug if self.team_id else None,
            team_id=self.team_id if self.team_id else None,
        )

    def log_name(self, agent_name: str | None, period: str) -> str:
        """Canonical period-log name for this store's scope."""
        return log_doc_name(
            agent_name or self.agent_name,
            period,
            team_slug=self.team_slug if self.team_id else None,
            team_id=self.team_id if self.team_id else None,
        )

    def _log_storage_dirs(self) -> list[Path]:
        """Canonical ``logs/`` dir first, then legacy ``daily/`` for reads."""
        if self.team_id:
            team_dir = self._workspace / "teams" / self.team_id
            return [team_dir / "logs", team_dir / "daily"]
        return [
            self._workspace / "shared" / "logs",
            self._workspace / "shared" / "daily",
        ]

    def _log_period_path(self, parts: list[str]) -> Path:
        period_file = f"{parts[-1]}.md"
        for directory in self._log_storage_dirs():
            candidate = directory / period_file
            if candidate.exists():
                return candidate
        return self._log_storage_dirs()[0] / period_file

    def _name_to_path(self, name: str) -> Path:
        """Map a post name like ``MEMORY:agent`` to a local file path."""
        if is_log_prefix(name.split(":", 1)[0]):
            name = canonical_log_name(name)
        parts = name.split(":")
        prefix = parts[0]

        if prefix == "SOUL":
            return self._workspace / "SOUL.md"

        if prefix == "SHARED":
            # Cross-team shared notes always live at workspace root, ignoring
            # any team scope on this store.
            if len(parts) >= 2 and parts[1] == "memory":
                return self._workspace / "MEMORY.md"
            safe = parts[1] if len(parts) >= 2 else "shared"
            return self._workspace / f"SHARED_{safe}.md"

        if self.team_id:
            team_dir = self._workspace / "teams" / self.team_id
            if prefix == "MEMORY":
                return team_dir / "MEMORY.md"
            if prefix == LOG_PREFIX and len(parts) >= 3:
                return self._log_period_path(parts)
            if prefix in ("HEARTBEAT", "NOTES"):
                return team_dir / f"{prefix}.md"

        if prefix in ("NOTES", "HEARTBEAT"):
            return self._workspace / f"{prefix}.md"
        if prefix == "MEMORY":
            return self._workspace / "shared" / "memory" / "MEMORY.md"
        if prefix == LOG_PREFIX and len(parts) >= 3:
            return self._log_period_path(parts)
        if prefix == "USER" and len(parts) >= 2:
            return self._workspace / "shared" / "users" / f"{parts[1]}.md"

        safe = name.replace(":", "_").replace("/", "_")
        return self._workspace / "data" / "docs" / f"{safe}.md"

    def read(self, name: str) -> str:
        path = self._name_to_path(name)
        if not path.exists():
            return ""
        try:
            return strip_frontmatter(path.read_text()).strip()
        except Exception:
            return ""

    def read_with_meta(self, name: str) -> ReadResult:
        path = self._name_to_path(name)
        if not path.exists():
            return ReadResult(content="")
        try:
            raw = path.read_text()
            content = strip_frontmatter(raw).strip()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return ReadResult(content=content, last_updated=mtime)
        except Exception:
            return ReadResult(content="")

    def write(self, name: str, content_md: str) -> bool:
        from ..memory_lock import memory_write_lock

        with memory_write_lock():
            path = self._name_to_path(name)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content_md)
                return True
            except Exception as e:
                logger.warning("LocalDocStore.write failed for %s: %s", name, e)
                return False

    def append(self, name: str, markdown: str) -> bool:
        from ..memory_lock import memory_write_lock

        with memory_write_lock():
            path = self._name_to_path(name)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as f:
                    f.write(markdown)
                return True
            except Exception as e:
                logger.warning("LocalDocStore.append failed for %s: %s", name, e)
                return False

    def append_list_item(
        self,
        name: str,
        markdown_item: str,
        *,
        initial_md: str | None = None,
    ) -> bool:
        """Append a list item to an existing file, or create it.

        If the file doesn't exist and *initial_md* is provided, write
        *initial_md* as the seed content. Otherwise merge *markdown_item*
        into the trailing list of the current file body.
        """
        from ..memory_lock import memory_write_lock

        with memory_write_lock():
            if not self.exists(name):
                # write() takes the same lock (RLock) — safe to nest.
                return self.write(
                    name, initial_md if initial_md is not None else markdown_item
                )
            current = self.read(name)
            return self.write(name, _append_markdown_list_item(current, markdown_item))

    def exists(self, name: str) -> bool:
        return self._name_to_path(name).exists()

    def comment(self, name: str, content_md: str) -> bool:
        return False

    def read_comments(self, name: str) -> list[dict]:
        return []

    def search(self, query: str) -> list[dict]:
        return []

    def is_owner(self, name: str) -> bool:
        return True


class CompositeDocStore:
    """Routes doc operations to the right backend by name prefix.

    Identity prefixes (``SOUL``/``HEARTBEAT``/``NOTES``) always go to the
    local store — these are per-machine identity files, never persisted to
    Ouro. Everything else goes to the Ouro store when one is provided, and
    falls back to local when ``ouro=None`` (no team configured, or the team
    is not writable by agents).
    """

    def __init__(
        self,
        local: LocalDocStore,
        ouro: Optional[OuroDocStore] = None,
    ):
        self._local = local
        self._ouro = ouro

    @property
    def ouro(self) -> Optional[OuroDocStore]:
        """Expose the underlying Ouro store (used by workspace_sync)."""
        return self._ouro

    @property
    def local(self) -> LocalDocStore:
        """Expose the underlying local store."""
        return self._local

    @property
    def rhythm(self) -> str:
        """Memory rhythm, taken from whichever backend owns the name scope."""
        return getattr(self._scoped(), "rhythm", "daily")

    def _backend(self, name: str):
        prefix = name.split(":", 1)[0]
        if prefix in IDENTITY_PREFIXES or self._ouro is None:
            return self._local
        return self._ouro

    def _scoped(self):
        """Return whichever backend defines the canonical name scope."""
        return self._ouro or self._local

    def memory_name(self, agent_name: str | None = None) -> str:
        return self._scoped().memory_name(agent_name)

    def log_name(self, agent_name: str | None, period: str) -> str:
        return self._scoped().log_name(agent_name, period)

    def read(self, name: str) -> str:
        return self._backend(name).read(name)

    def read_with_meta(self, name: str) -> ReadResult:
        return self._backend(name).read_with_meta(name)

    def write(self, name: str, content_md: str) -> bool:
        return self._backend(name).write(name, content_md)

    def append(self, name: str, markdown: str) -> bool:
        return self._backend(name).append(name, markdown)

    def append_list_item(
        self,
        name: str,
        markdown_item: str,
        *,
        initial_md: str | None = None,
    ) -> bool:
        return self._backend(name).append_list_item(
            name, markdown_item, initial_md=initial_md
        )

    def exists(self, name: str) -> bool:
        return self._backend(name).exists(name)

    def comment(self, name: str, content_md: str) -> bool:
        return self._backend(name).comment(name, content_md)

    def read_comments(self, name: str) -> list[dict]:
        return self._backend(name).read_comments(name)

    def is_owner(self, name: str) -> bool:
        return self._backend(name).is_owner(name)

    def search(self, query: str) -> list[dict]:
        backend = self._ouro or self._local
        return backend.search(query)
