"""Ouro-backed document store for shared agent memory.

Resolves post names (e.g. ``SOUL:research-agent``) to UUIDs via search,
provides read/write/append/comment primitives.  Uses ouro-py directly
for typed responses and proper Content-level append support.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ouro import Ouro
    from ouro.resources.content import Content

logger = logging.getLogger(__name__)


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
    ):
        self.agent_name = agent_name
        self.org_id = org_id
        self.team_id = team_id
        self._client = client or _build_client(api_key, base_url)
        self._uuid_cache: dict[str, str] = {}
        self._owner_cache: dict[str, bool] = {}

    def _resolve(self, name: str) -> Optional[str]:
        """Resolve a post name to its UUID via search. Caches the result."""
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
            return uuid
        except Exception as e:
            logger.warning("OuroDocStore._create failed for %s: %s", name, e)
            return None

    def read(self, name: str) -> str:
        """Read a post by name. Returns empty string if not found."""
        uuid = self._resolve(name)
        if not uuid:
            return ""

        try:
            post = self._client.posts.retrieve(uuid)
            if post.content:
                from ouro.resources.content import Content

                c = Content(json=post.content.data, text=post.content.text, _ouro=self._client)
                return c.to_markdown().strip()
            return ""
        except Exception as e:
            logger.warning("OuroDocStore.read failed for %s: %s", name, e)
            return ""

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
