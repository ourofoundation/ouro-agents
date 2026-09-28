"""Build real ouro-py models for tests that fake the SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, uuid5

from ouro.models import Asset

ModelT = TypeVar("ModelT", bound=Asset)


def uid(name: str) -> str:
    """A stable UUID for a readable test name."""
    return str(uuid5(NAMESPACE_URL, name))


def make_asset(
    model: type[ModelT] = Asset,
    *,
    id: str,
    asset_type: str = "quest",
    created_at: datetime | str | None = None,
    **fields: Any,
) -> ModelT:
    """An asset with the required fields filled in; ``id`` is a test name."""
    created = created_at or datetime(2026, 7, 1, tzinfo=timezone.utc)
    return model.model_validate(
        {
            "id": uid(id),
            "user_id": uid("owner"),
            "org_id": uid("org"),
            "team_id": uid("team"),
            "visibility": "public",
            "asset_type": asset_type,
            "created_at": created,
            "last_updated": created,
            **fields,
        }
    )
