"""Ouro-backed document store for shared agent memory.

Resolves post names (e.g. ``SOUL:research-agent``) to UUIDs via a local
JSON registry, falling back to search when needed.  The registry persists
across restarts so fuzzy-search flakiness can never create duplicate posts.

Provides read/write/append/comment primitives.  Uses ouro-py directly
for typed responses and proper Content-level append support.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ouro import Ouro
    from ouro.resources.content import Content

logger = logging.getLogger(__name__)


_TEAM_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ReadResult:
    """Content + metadata from an Ouro post read."""

    content: str
    last_updated: Optional[datetime] = None
    post_id: Optional[str] = None


def _build_client(api_key: str | None = None, base_url: str | None = None) -> "Ouro":
    """Create an Ouro client from explicit creds or environment."""
    import os

    from ouro import Ouro

    key = api_key or os.getenv("OURO_API_KEY")
    url = base_url or os.getenv("OURO_BASE_URL")
    if not key:
        raise RuntimeError("OURO_API_KEY required for OuroDocStore")
    return Ouro(api_key=key, base_url=url)


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
    """Resolve the canonical team qualifier used in MEMORY/DAILY names."""
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


def daily_doc_name(
    agent_name: str,
    day: str,
    *,
    team_slug: str | None = None,
    team_name: str | None = None,
    team_id: str | None = None,
) -> str:
    """Build the canonical daily-log doc name."""
    qualifier = team_doc_key(team_slug=team_slug, team_name=team_name, team_id=team_id)
    if qualifier:
        return f"DAILY:{agent_name}:{qualifier}:{day}"
    return f"DAILY:{agent_name}:{day}"


class OuroDocStore:
    """Thin wrapper over ouro-py for reading/writing named posts."""

    _SINGLETON_PREFIXES = {"SOUL", "NOTES", "HEARTBEAT", "MEMORY", "DAILY", "USER"}
    _IDENTITY_PREFIXES = {"SOUL", "HEARTBEAT", "NOTES"}

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
        workspace: Optional[Path] = None,
        team_slug: str | None = None,
        team_name: str | None = None,
    ):
        self.agent_name = agent_name
        self.org_id = org_id
        self.team_id = team_id
        self.team_name = team_name or ""
        self.team_slug = team_doc_key(team_slug=team_slug, team_id=team_id)
        self._client = client or _build_client(api_key, base_url)
        self._owner_cache: dict[str, bool] = {}
        self._write_lock = threading.RLock()

        self._registry_path = registry_path
        self._legacy_registry_path = self._derive_legacy_registry_path(registry_path)
        self._uuid_cache: dict[str, str] = self._load_registry()

        self._local: Optional[LocalDocStore] = None
        if workspace:
            self._local = LocalDocStore(
                workspace,
                agent_name=agent_name,
                team_id=team_id,
                team_slug=self.team_slug,
            )

    def memory_name(self, agent_name: str | None = None) -> str:
        """Canonical MEMORY name for this store's scope."""
        return memory_doc_name(
            agent_name or self.agent_name,
            team_slug=self.team_slug,
            team_id=self.team_id,
        )

    def daily_name(self, agent_name: str | None, day: str) -> str:
        """Canonical DAILY name for this store's scope."""
        return daily_doc_name(
            agent_name or self.agent_name,
            day,
            team_slug=self.team_slug,
            team_id=self.team_id,
        )

    def _canonicalize_name(self, name: str) -> str:
        """Map legacy team-scoped names to canonical team-qualified names."""
        parts = name.split(":")
        prefix = parts[0]
        if prefix == "MEMORY" and len(parts) == 2:
            return self.memory_name(parts[1])
        if prefix == "DAILY" and len(parts) == 3:
            return self.daily_name(parts[1], parts[2])
        return name

    def _legacy_aliases(self, canonical_name: str) -> list[str]:
        """Legacy aliases that may still exist for canonical team-scoped docs."""
        parts = canonical_name.split(":")
        prefix = parts[0]
        if prefix == "DAILY" and len(parts) == 4:
            return [f"DAILY:{parts[1]}:{parts[3]}"]
        return []

    def _candidate_names(self, name: str) -> tuple[str, list[str]]:
        canonical = self._canonicalize_name(name)
        candidates = [canonical]
        for alias in self._legacy_aliases(canonical):
            if alias not in candidates:
                candidates.append(alias)
        return canonical, candidates

    def _is_identity_name(self, name: str) -> bool:
        """True for docs that must stay local (never written to Ouro)."""
        return name.split(":", 1)[0] in self._IDENTITY_PREFIXES

    @staticmethod
    def _derive_legacy_registry_path(registry_path: Path | None) -> Path | None:
        """Return the old registry filename for backward compatibility."""
        if not registry_path:
            return None
        if registry_path.name == "state.json":
            return registry_path.with_name("doc_registry.json")
        return None

    def _registry_read_path(self) -> Path | None:
        """Prefer the new state file, but fall back to the legacy registry file."""
        if self._registry_path and self._registry_path.exists():
            return self._registry_path
        if self._legacy_registry_path and self._legacy_registry_path.exists():
            return self._legacy_registry_path
        return self._registry_path

    def _load_registry(self) -> dict[str, str]:
        """Load the name→UUID registry from disk (or return empty)."""
        load_path = self._registry_read_path()
        if not load_path or not load_path.exists():
            return {}
        try:
            data = json.loads(load_path.read_text())
            if isinstance(data, dict) and isinstance(data.get("docs"), dict):
                docs = data.get("docs", {})
                if all(isinstance(k, str) and isinstance(v, str) for k, v in docs.items()):
                    team = data.get("team")
                    if isinstance(team, dict):
                        self.team_name = str(team.get("name") or self.team_name or "")
                        self.team_slug = team_doc_key(
                            team_slug=str(team.get("slug") or ""),
                            team_name=self.team_name,
                            team_id=str(team.get("id") or self.team_id),
                        )
                    logger.debug("Loaded doc registry with %d entries", len(docs))
                    return docs
            if isinstance(data, dict):
                docs = {
                    key: value
                    for key, value in data.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                logger.debug("Loaded legacy doc registry with %d entries", len(docs))
                return docs
        except Exception as e:
            logger.warning("Failed to load doc registry: %s", e)
        return {}

    def _save_registry(self) -> None:
        """Persist the name→UUID cache to disk."""
        if not self._registry_path:
            return
        try:
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "team": {
                    "id": self.team_id,
                    "name": self.team_name,
                    "slug": self.team_slug,
                    "org_id": self.org_id,
                },
                "docs": self._uuid_cache,
            }
            self._registry_path.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            logger.warning("Failed to save doc registry: %s", e)

    def _remember_uuid(self, name: str, uuid: str) -> str:
        """Cache and persist a resolved UUID for future exact lookups."""
        self._uuid_cache[name] = uuid
        self._save_registry()
        return uuid

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
        """Search for posts with an exact matching name."""
        results = self._client.assets.search(
            query=name,
            asset_type="post",
            team_id=self.team_id,
            limit=limit,
        )
        if not isinstance(results, list):
            return []
        return [item for item in results if item.get("name", "") == name]

    @classmethod
    def _is_singleton_name(cls, name: str) -> bool:
        """Return True for registry-first named memory docs."""
        prefix = name.split(":", 1)[0]
        return prefix in cls._SINGLETON_PREFIXES

    def _select_exact_match(self, name: str, matches: list[dict]) -> Optional[str]:
        """Pick the most recent exact match when duplicates already exist."""
        if not matches:
            return None
        if len(matches) == 1:
            return str(matches[0]["id"])

        def sort_key(item: dict) -> tuple[bool, datetime]:
            ts = self._coerce_timestamp(
                item.get("last_updated") or item.get("updated_at") or item.get("created_at")
            )
            return (ts is not None, ts or datetime.min.replace(tzinfo=timezone.utc))

        selected = max(matches, key=sort_key)
        logger.warning(
            "Multiple exact post matches found for %s; using %s",
            name,
            selected.get("id"),
        )
        return str(selected["id"])

    def _resolve_name(self, name: str) -> tuple[Optional[str], bool, str]:
        """Resolve a post name and report whether recovery was ambiguous.

        Checks the file-backed registry first (survives restarts), then
        does a one-time exact-name recovery search when needed. Singleton
        memory docs treat the registry as authoritative and refuse to pick a
        winner from ambiguous exact-name search results.
        """
        canonical, candidates = self._candidate_names(name)

        for candidate in candidates:
            if candidate in self._uuid_cache:
                uuid = self._uuid_cache[candidate]
                self._remember_uuid(canonical, uuid)
                return uuid, False, candidate

        try:
            for candidate in candidates:
                matches = self._search_exact_name_matches(candidate, limit=25)
                if self._is_singleton_name(candidate):
                    if not matches:
                        continue
                    if len(matches) > 1:
                        logger.warning(
                            "Multiple exact singleton post matches found for %s; refusing recovery",
                            candidate,
                        )
                        return None, True, canonical
                    uuid = str(matches[0]["id"])
                else:
                    uuid = self._select_exact_match(candidate, matches)
                if uuid:
                    self._remember_uuid(canonical, uuid)
                    if candidate != canonical:
                        self._uuid_cache.pop(candidate, None)
                        self._save_registry()
                    return uuid, False, candidate
        except Exception as e:
            logger.warning("OuroDocStore._resolve failed for %s: %s", canonical, e)

        return None, False, canonical

    def _resolve(self, name: str) -> Optional[str]:
        """Resolve a post name to its UUID."""
        uuid, _ambiguous, _matched = self._resolve_name(name)
        return uuid

    def _ensure_canonical_remote_name(
        self,
        uuid: str,
        *,
        canonical_name: str,
        matched_name: str,
    ) -> None:
        """Rename legacy team-scoped posts to the canonical team-qualified title."""
        if matched_name == canonical_name:
            return
        try:
            self._client.posts.update(uuid, name=canonical_name)
            self._uuid_cache.pop(matched_name, None)
            self._remember_uuid(canonical_name, uuid)
        except Exception as e:
            logger.warning(
                "Failed to rename legacy doc %s -> %s: %s",
                matched_name,
                canonical_name,
                e,
            )

    def _make_content(self, markdown: str) -> "Content":
        """Build a Content object from markdown using the SDK's server-side parser."""
        content = self._client.posts.Content()
        content.from_markdown(markdown)
        return content

    def _create(self, name: str, content_md: str) -> Optional[str]:
        """Create a new post and return its UUID."""
        name = self._canonicalize_name(name)
        try:
            post = self._client.posts.create(
                name=name,
                content_markdown=content_md,
                org_id=self.org_id,
                team_id=self.team_id,
                visibility="organization",
            )
            uuid = str(post.id)
            self._uuid_cache[name] = uuid
            self._owner_cache[name] = True
            self._save_registry()
            if self._local:
                self._local.write(name, content_md)
            return uuid
        except Exception as e:
            logger.warning("OuroDocStore._create failed for %s: %s", name, e)
            return None

    def read(self, name: str) -> str:
        """Read a post by name. Returns empty string if not found."""
        if self._is_identity_name(name) and self._local:
            return self._local.read(name)
        result = self.read_with_meta(name)
        return result.content

    def read_with_meta(self, name: str) -> ReadResult:
        """Read a post by name, returning content and metadata."""
        if self._is_identity_name(name) and self._local:
            return self._local.read_with_meta(name)
        canonical_name = self._canonicalize_name(name)
        uuid = self._resolve(canonical_name)
        if not uuid:
            return ReadResult(content="")

        try:
            post = self._client.posts.retrieve(uuid)
            content = ""
            if post.content:
                from ouro.resources.content import Content as ContentCls

                c = ContentCls(
                    json=post.content.data,
                    text=post.content.text,
                    _ouro=self._client,
                )
                content = c.to_markdown().strip()
            return ReadResult(
                content=content,
                last_updated=post.last_updated,
                post_id=str(post.id),
            )
        except Exception as e:
            logger.warning("OuroDocStore.read_with_meta failed for %s: %s", name, e)
            return ReadResult(content="")

    def write(self, name: str, content_md: str) -> bool:
        """Update a post this agent owns. Creates it if it doesn't exist."""
        name = self._canonicalize_name(name)
        if self._is_identity_name(name):
            if self._local:
                return self._local.write(name, content_md)
            return False
        uuid, ambiguous, matched_name = self._resolve_name(name)

        if uuid is None and ambiguous:
            logger.warning(
                "Refusing to create %s because recovery found multiple exact matches",
                name,
            )
            return False

        if uuid is None:
            with self._write_lock:
                uuid, ambiguous, matched_name = self._resolve_name(name)
                if uuid is None:
                    if ambiguous:
                        logger.warning(
                            "Refusing to create %s because recovery found multiple exact matches",
                            name,
                        )
                        return False
                    return self._create(name, content_md) is not None

        try:
            self._ensure_canonical_remote_name(
                uuid,
                canonical_name=name,
                matched_name=matched_name,
            )
            content = self._make_content(content_md)
            self._client.posts.update(id=uuid, content=content)
            if self._local:
                self._local.write(name, content_md)
            return True
        except Exception as e:
            logger.warning("OuroDocStore.write failed for %s: %s", name, e)
            return False

    def append(self, name: str, markdown: str) -> bool:
        """Append markdown to an existing post (or create it).

        Works at the Content/TipTap level so rich formatting is preserved
        — no read→concat→rewrite lossy round-trip.
        """
        name = self._canonicalize_name(name)
        if self._is_identity_name(name):
            if self._local:
                return self._local.append(name, markdown)
            return False
        uuid, ambiguous, matched_name = self._resolve_name(name)

        if uuid is None and ambiguous:
            logger.warning(
                "Refusing to create %s because recovery found multiple exact matches",
                name,
            )
            return False

        if uuid is None:
            with self._write_lock:
                uuid, ambiguous, matched_name = self._resolve_name(name)
                if uuid is None:
                    if ambiguous:
                        logger.warning(
                            "Refusing to create %s because recovery found multiple exact matches",
                            name,
                        )
                        return False
                    return self._create(name, markdown) is not None

        try:
            self._ensure_canonical_remote_name(
                uuid,
                canonical_name=name,
                matched_name=matched_name,
            )
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

            new_block = self._make_content(markdown)
            existing.append(new_block)
            self._client.posts.update(id=uuid, content=existing)
            if self._local:
                self._local.append(name, markdown)
            return True
        except Exception as e:
            logger.warning("OuroDocStore.append failed for %s: %s", name, e)
            return False

    def comment(self, name: str, content_md: str) -> bool:
        """Add a comment to a post (typically one this agent does NOT own)."""
        uuid = self._resolve(self._canonicalize_name(name))
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
        """Read comments on a post (for consolidation)."""
        uuid = self._resolve(self._canonicalize_name(name))
        if not uuid:
            return []

        try:
            comments = self._client.comments.list_by_parent(uuid)
            return [c.model_dump(mode="json") for c in comments]
        except Exception as e:
            logger.warning("OuroDocStore.read_comments failed for %s: %s", name, e)
            return []

    def is_owner(self, name: str) -> bool:
        """Check if this agent created the named post."""
        if name in self._owner_cache:
            return self._owner_cache[name]
        return False

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
        if self._is_identity_name(name):
            return self._local.exists(name) if self._local else False
        return self._resolve(self._canonicalize_name(name)) is not None


class LocalDocStore:
    """File-backed document store mapping post names to local workspace files.

    Provides the same interface as ``OuroDocStore`` so consumers never need
    to branch on which backend is active.  Used when no Ouro org/team is
    configured.

    When *team_id* is set, MEMORY, DAILY, HEARTBEAT, and NOTES route to
    ``teams/{team_id}/`` instead of shared workspace paths, isolating
    working state per team while ``SOUL`` stays global.
    """

    def __init__(
        self,
        workspace: Path,
        agent_name: str = "",
        team_id: str | None = None,
        team_slug: str | None = None,
    ):
        self._workspace = workspace
        self.agent_name = agent_name
        self.team_id = team_id
        self.team_slug = team_doc_key(team_slug=team_slug, team_id=team_id)

    def memory_name(self, agent_name: str | None = None) -> str:
        """Canonical MEMORY name for this store's scope."""
        return memory_doc_name(
            agent_name or self.agent_name,
            team_slug=self.team_slug if self.team_id else None,
            team_id=self.team_id if self.team_id else None,
        )

    def daily_name(self, agent_name: str | None, day: str) -> str:
        """Canonical DAILY name for this store's scope."""
        return daily_doc_name(
            agent_name or self.agent_name,
            day,
            team_slug=self.team_slug if self.team_id else None,
            team_id=self.team_id if self.team_id else None,
        )

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        from .workspace_sync import strip_frontmatter
        return strip_frontmatter(text)

    def _name_to_path(self, name: str) -> Path:
        """Map a post name like ``MEMORY:agent`` to a local file path.

        Routing depends on whether a team_id is set:

        **Identity (always workspace root):** SOUL

        **Team-scoped (under teams/{team_id}/ when team_id set):**
        MEMORY, DAILY, HEARTBEAT, NOTES

        **Shared (under shared/ when no team_id):**
        DAILY → shared/daily/, MEMORY → shared/memory/, USER → shared/users/
        """
        parts = name.split(":")
        prefix = parts[0]

        if prefix == "SOUL":
            return self._workspace / "SOUL.md"

        if self.team_id:
            team_dir = self._workspace / "teams" / self.team_id
            if prefix == "MEMORY":
                return team_dir / "MEMORY.md"
            if prefix == "DAILY" and len(parts) >= 3:
                return team_dir / "daily" / f"{parts[-1]}.md"
            if prefix in ("HEARTBEAT", "NOTES"):
                return team_dir / f"{prefix}.md"

        if prefix in ("NOTES", "HEARTBEAT"):
            return self._workspace / f"{prefix}.md"

        if prefix == "MEMORY":
            legacy = self._workspace / "MEMORY.md"
            if legacy.exists():
                return legacy
            return self._workspace / "shared" / "memory" / "MEMORY.md"
        if prefix == "DAILY" and len(parts) >= 3:
            legacy = self._workspace / "memory" / "daily" / f"{parts[-1]}.md"
            shared = self._workspace / "shared" / "daily" / f"{parts[-1]}.md"
            if legacy.exists() and not shared.exists():
                return legacy
            return shared
        if prefix == "USER" and len(parts) >= 2:
            legacy = self._workspace / "memory" / "users" / f"{parts[1]}.md"
            shared = self._workspace / "shared" / "users" / f"{parts[1]}.md"
            if legacy.exists() and not shared.exists():
                return legacy
            return shared

        safe = name.replace(":", "_").replace("/", "_")
        return self._workspace / "data" / "docs" / f"{safe}.md"

    def read(self, name: str) -> str:
        path = self._name_to_path(name)
        if not path.exists():
            return ""
        try:
            return self._strip_frontmatter(path.read_text()).strip()
        except Exception:
            return ""

    def read_with_meta(self, name: str) -> ReadResult:
        path = self._name_to_path(name)
        if not path.exists():
            return ReadResult(content="")
        try:
            raw = path.read_text()
            content = self._strip_frontmatter(raw).strip()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return ReadResult(content=content, last_updated=mtime)
        except Exception:
            return ReadResult(content="")

    def write(self, name: str, content_md: str) -> bool:
        path = self._name_to_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content_md)
            return True
        except Exception as e:
            logger.warning("LocalDocStore.write failed for %s: %s", name, e)
            return False

    def append(self, name: str, markdown: str) -> bool:
        path = self._name_to_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(markdown)
            return True
        except Exception as e:
            logger.warning("LocalDocStore.append failed for %s: %s", name, e)
            return False

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


# Re-export the DocStore Protocol from the package root for backward compatibility.
from . import DocStore as DocStore  # noqa: F811
