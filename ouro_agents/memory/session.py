from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from . import MemoryResult
from .model import MemoryItem, memory_item_from_raw, to_metadata, utc_now

logger = logging.getLogger(__name__)


class SessionMemoryStore:
    """Small persisted working set keyed by conversation_id."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._cache: dict[str, list[MemoryItem]] = {}

    def _path(self, conversation_id: str) -> Path:
        return self.workspace / "conversations" / f"{conversation_id}.session.json"

    def load(self, conversation_id: str) -> list[MemoryItem]:
        if conversation_id in self._cache:
            return self._cache[conversation_id]
        path = self._path(conversation_id)
        if not path.exists():
            self._cache[conversation_id] = []
            return []
        try:
            raw_items = json.loads(path.read_text())
            items = [
                memory_item_from_raw(
                    str(raw.get("text") or ""),
                    raw.get("metadata") or {},
                )
                for raw in raw_items
                if isinstance(raw, dict)
            ]
        except Exception as e:
            logger.warning("Failed to load session memory for %s: %s", conversation_id, e)
            items = []
        self._cache[conversation_id] = items
        return items

    def save(self, conversation_id: str, items: list[MemoryItem]) -> None:
        path = self._path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"text": item.text, "metadata": to_metadata(item)} for item in items]
        path.write_text(json.dumps(payload, indent=2))
        self._cache[conversation_id] = items

    def append(self, conversation_id: str, item: MemoryItem, *, max_items: int = 80) -> None:
        items = self.load(conversation_id)
        items.append(item)
        if len(items) > max_items:
            items = items[-max_items:]
        self.save(conversation_id, items)

    def append_turn(
        self,
        conversation_id: str,
        *,
        role: str,
        text: str,
        agent_id: str = "",
        user_id: str = "",
        run_id: str = "",
    ) -> None:
        text = text.strip()
        if not text:
            return
        now = utc_now()
        item = MemoryItem(
            text=f"{role}: {text[:1200]}",
            subject_type="conversation",
            subject_id=conversation_id,
            category="observation",
            conversation_id=conversation_id,
            user_id=user_id,
            run_id=run_id,
            mode="chat",
            source=f"session:{agent_id}" if agent_id else "session",
            importance=0.3,
            confidence=1.0,
            created_at=now,
        )
        self.append(conversation_id, item)

    def search(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 5,
        category: Optional[str] = None,
    ) -> list[MemoryResult]:
        query_terms = {part.lower() for part in query.split() if len(part) > 2}
        results: list[MemoryResult] = []
        for item in reversed(self.load(conversation_id)):
            if category and item.category != category:
                continue
            text_terms = set(item.text.lower().split())
            overlap = len(query_terms & text_terms) if query_terms else 1
            if query_terms and overlap == 0:
                continue
            score = min(1.0, 0.2 + overlap / max(1, len(query_terms)))
            results.append(
                MemoryResult(
                    text=item.text,
                    score=score,
                    category=item.category,
                    importance=item.importance,
                    created_at=item.created_at.isoformat(),
                    source=item.source,
                    subject_type=item.subject_type,
                    subject_id=item.subject_id,
                    team_ids=item.team_ids,
                    asset_ids=item.asset_ids,
                    user_id=item.user_id,
                    mode=item.mode,
                    confidence=item.confidence,
                    content_hash=item.content_hash,
                    schema_version=item.schema_version,
                    metadata=to_metadata(item),
                )
            )
            if len(results) >= limit:
                break
        return results
