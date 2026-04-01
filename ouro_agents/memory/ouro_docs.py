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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from ouro import Ouro
    from ouro.resources.content import Content

logger = logging.getLogger(__name__)


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
    ):
        self.agent_name = agent_name
        self.org_id = org_id
        self.team_id = team_id
        self._client = client or _build_client(api_key, base_url)
        self._owner_cache: dict[str, bool] = {}

        self._registry_path = registry_path
        self._uuid_cache: dict[str, str] = self._load_registry()

    def _load_registry(self) -> dict[str, str]:
        """Load the name→UUID registry from disk (or return empty)."""
        if not self._registry_path or not self._registry_path.exists():
            return {}
        try:
            data = json.loads(self._registry_path.read_text())
            if isinstance(data, dict):
                logger.debug("Loaded doc registry with %d entries", len(data))
                return data
        except Exception as e:
            logger.warning("Failed to load doc registry: %s", e)
        return {}

    def _save_registry(self) -> None:
        """Persist the name→UUID cache to disk."""
        if not self._registry_path:
            return
        try:
            self._registry_path.parent.mkdir(parents=True, exist_ok=True)
            self._registry_path.write_text(json.dumps(self._uuid_cache, indent=2))
        except Exception as e:
            logger.warning("Failed to save doc registry: %s", e)

    def _resolve(self, name: str) -> Optional[str]:
        """Resolve a post name to its UUID.

        Checks the file-backed registry first (survives restarts), then
        falls back to search.  Any newly discovered mapping is persisted
        immediately so duplicates are never created.
        """
        if name in self._uuid_cache:
            return self._uuid_cache[name]

        try:
            results = self._client.assets.search(
                query=name,
                asset_type="post",
                team_id=self.team_id,
                limit=5,
            )
            if isinstance(results, list):
                for item in results:
                    item_name = item.get("name", "")
                    if item_name == name:
                        uuid = item["id"]
                        self._uuid_cache[name] = uuid
                        self._save_registry()
                        return uuid
        except Exception as e:
            logger.warning("OuroDocStore._resolve failed for %s: %s", name, e)

        return None

    def _make_content(self, markdown: str) -> "Content":
        """Build a Content object from markdown using the SDK's server-side parser."""
        content = self._client.posts.Content()
        content.from_markdown(markdown)
        return content

    def _create(self, name: str, content_md: str) -> Optional[str]:
        """Create a new post and return its UUID."""
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
            return uuid
        except Exception as e:
            logger.warning("OuroDocStore._create failed for %s: %s", name, e)
            return None

    def read(self, name: str) -> str:
        """Read a post by name. Returns empty string if not found."""
        result = self.read_with_meta(name)
        return result.content

    def read_with_meta(self, name: str) -> ReadResult:
        """Read a post by name, returning content and metadata."""
        uuid = self._resolve(name)
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
        uuid = self._resolve(name)

        if uuid is None:
            return self._create(name, content_md) is not None

        try:
            content = self._make_content(content_md)
            self._client.posts.update(id=uuid, content=content)
            return True
        except Exception as e:
            logger.warning("OuroDocStore.write failed for %s: %s", name, e)
            return False

    def append(self, name: str, markdown: str) -> bool:
        """Append markdown to an existing post (or create it).

        Works at the Content/TipTap level so rich formatting is preserved
        — no read→concat→rewrite lossy round-trip.
        """
        uuid = self._resolve(name)

        if uuid is None:
            return self._create(name, markdown) is not None

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

            new_block = self._make_content(markdown)
            existing.append(new_block)
            self._client.posts.update(id=uuid, content=existing)
            return True
        except Exception as e:
            logger.warning("OuroDocStore.append failed for %s: %s", name, e)
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
        """Read comments on a post (for consolidation)."""
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
        return self._resolve(name) is not None


class LocalDocStore:
    """File-backed document store mapping post names to local workspace files.

    Provides the same interface as ``OuroDocStore`` so consumers never need
    to branch on which backend is active.  Used when no Ouro org/team is
    configured.
    """

    def __init__(self, workspace: Path, agent_name: str = ""):
        self._workspace = workspace
        self.agent_name = agent_name

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if not text.startswith("---"):
            return text
        end = text.find("---", 3)
        if end == -1:
            return text
        return text[end + 3 :].lstrip("\n")

    def _name_to_path(self, name: str) -> Path:
        """Map a post name like ``MEMORY:agent`` to a local file path."""
        parts = name.split(":", 2)
        prefix = parts[0]
        if prefix in ("SOUL", "NOTES", "HEARTBEAT", "MEMORY"):
            return self._workspace / f"{prefix}.md"
        if prefix == "DAILY" and len(parts) >= 3:
            return self._workspace / "memory" / "daily" / f"{parts[2]}.md"
        if prefix == "USER" and len(parts) >= 2:
            return self._workspace / "memory" / "users" / f"{parts[1]}.md"
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


DocStore = Union[OuroDocStore, LocalDocStore]
