"""Published-route registry under ``protected/published_routes/``."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..tools.workspace_paths import ensure_protected_dir, protected_root
from .manifest import (
    HANDLER_FILENAME,
    MANIFEST_FILENAME,
    RouteManifest,
    find_manifest_path,
)

logger = logging.getLogger(__name__)

PUBLISHED_DIRNAME = "published_routes"
REGISTRY_FILENAME = "registry.json"


def published_routes_root(workspace: Path | str) -> Path:
    return protected_root(workspace) / PUBLISHED_DIRNAME


def registry_path(workspace: Path | str) -> Path:
    return published_routes_root(workspace) / REGISTRY_FILENAME


class PublishedRouteEntry(BaseModel):
    version: int = Field(ge=1)
    published_at: str
    handler_path: str
    manifest: dict[str, Any]


class PublishedRoutesRegistry(BaseModel):
    service_id: Optional[str] = None
    routes: dict[str, PublishedRouteEntry] = Field(default_factory=dict)

    def get(self, name: str) -> PublishedRouteEntry | None:
        return self.routes.get(name)

    def manifests(self) -> dict[str, RouteManifest]:
        out: dict[str, RouteManifest] = {}
        for name, entry in self.routes.items():
            try:
                out[name] = RouteManifest.model_validate(entry.manifest)
            except Exception as exc:  # noqa: BLE001 — skip broken entries
                logger.warning("Invalid published manifest for %s: %s", name, exc)
        return out

    def max_version(self) -> int:
        if not self.routes:
            return 1
        return max(entry.version for entry in self.routes.values())


def load_published_registry(workspace: Path | str) -> PublishedRoutesRegistry:
    path = registry_path(workspace)
    if not path.is_file():
        return PublishedRoutesRegistry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("registry.json must be an object")
        return PublishedRoutesRegistry.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load published routes registry %s: %s", path, exc)
        return PublishedRoutesRegistry()


def save_published_registry(
    workspace: Path | str, registry: PublishedRoutesRegistry
) -> Path:
    ensure_protected_dir(workspace)
    root = published_routes_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    path = registry_path(workspace)
    path.write_text(
        json.dumps(registry.model_dump(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def snapshot_route(
    workspace: Path,
    manifest: RouteManifest,
    *,
    version: int,
) -> PublishedRouteEntry:
    """Copy draft handler+manifest into an immutable published snapshot."""
    ensure_protected_dir(workspace)
    draft_dir = manifest.draft_dir(workspace)
    handler_src = draft_dir / HANDLER_FILENAME
    manifest_src = find_manifest_path(draft_dir)
    if not handler_src.is_file():
        raise FileNotFoundError(f"Missing handler: {handler_src}")
    if not manifest_src.is_file():
        raise FileNotFoundError(f"Missing manifest: {manifest_src}")

    snap_dir = published_routes_root(workspace) / manifest.name / f"v{version}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    handler_dest = snap_dir / HANDLER_FILENAME
    manifest_dest = snap_dir / MANIFEST_FILENAME
    handler_dest.write_text(handler_src.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_dest.write_text(
        json.dumps(manifest.model_dump(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rel_handler = (
        f"protected/{PUBLISHED_DIRNAME}/{manifest.name}/v{version}/{HANDLER_FILENAME}"
    )
    return PublishedRouteEntry(
        version=version,
        published_at=datetime.now(timezone.utc).isoformat(),
        handler_path=rel_handler,
        manifest=manifest.model_dump(),
    )


def next_version(registry: PublishedRoutesRegistry, name: str) -> int:
    existing = registry.get(name)
    return 1 if existing is None else existing.version + 1
