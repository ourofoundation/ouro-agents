"""Coil manifests: draft coil.json parsing and validation.

Coils are the tier-1 concept: private workspace handlers under
``workspace/coils/<name>/``. Publishing a coil turns it into a real Ouro
service route (tier 2), so the published side keeps route naming.
Legacy workspaces used ``routes/<name>/route.json``; both are still read.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
DRAFT_COILS_DIR = "coils"
LEGACY_DRAFT_DIR = "routes"
HANDLER_FILENAME = "handler.py"
MANIFEST_FILENAME = "coil.json"
LEGACY_MANIFEST_FILENAME = "route.json"
TITLE_MAX_LEN = 80


def find_draft_dir(workspace: Path | str, name: str) -> Path:
    """Directory for a draft coil, honoring the legacy ``routes/`` location.

    Prefers ``coils/<name>`` when it exists; falls back to ``routes/<name>``
    for pre-rename workspaces; defaults to the coils path for new drafts.
    """
    workspace = Path(workspace)
    preferred = workspace / DRAFT_COILS_DIR / name
    if preferred.is_dir():
        return preferred
    legacy = workspace / LEGACY_DRAFT_DIR / name
    if legacy.is_dir():
        return legacy
    return preferred


def find_manifest_path(draft_dir: Path) -> Path:
    """Manifest file inside a draft dir (coil.json, else legacy route.json)."""
    preferred = draft_dir / MANIFEST_FILENAME
    if preferred.is_file():
        return preferred
    legacy = draft_dir / LEGACY_MANIFEST_FILENAME
    if legacy.is_file():
        return legacy
    return preferred


def _format_jsonschema_error(error: Any) -> str:
    path = ".".join(str(part) for part in error.absolute_path)
    if path:
        return f"{path}: {error.message}"
    return str(error.message)


class RouteManifest(BaseModel):
    """Validated route.json contents."""

    name: str
    title: Optional[str] = None
    description: str = ""
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    inputs: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    input_assets: Optional[dict[str, Any]] = None
    output_assets: Optional[dict[str, Any]] = None
    mined_from: Optional[list[str]] = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not ROUTE_NAME_RE.match(value):
            raise ValueError(
                f"Route name must match {ROUTE_NAME_RE.pattern!r}, got {value!r}"
            )
        return value

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        title = value.strip()
        if not title:
            return None
        if len(title) > TITLE_MAX_LEN:
            raise ValueError(
                f"title must be at most {TITLE_MAX_LEN} characters, got {len(title)}"
            )
        return title

    @field_validator("inputs")
    @classmethod
    def _validate_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("inputs must be a JSON Schema object")
        if value.get("type") != "object":
            raise ValueError('inputs.type must be "object"')
        props = value.get("properties", {})
        if props is not None and not isinstance(props, dict):
            raise ValueError("inputs.properties must be an object")
        required = value.get("required")
        if required is not None and not isinstance(required, list):
            raise ValueError("inputs.required must be a list")
        try:
            from jsonschema.validators import Draft202012Validator

            Draft202012Validator.check_schema(value)
        except Exception as exc:  # noqa: BLE001 — surface as ValidationError
            raise ValueError(f"inputs is not a valid JSON Schema: {exc}") from exc
        return value

    @property
    def required_inputs(self) -> list[str]:
        required = self.inputs.get("required") or []
        return [str(item) for item in required]

    def missing_required(self, params: dict[str, Any] | None) -> list[str]:
        params = params or {}
        return [key for key in self.required_inputs if key not in params]

    def validate_params(self, params: dict[str, Any] | None) -> list[str]:
        """Return human-readable JSON Schema violations for *params* (empty if ok)."""
        from jsonschema import Draft202012Validator

        instance = params if isinstance(params, dict) else {}
        validator = Draft202012Validator(self.inputs)
        return [_format_jsonschema_error(err) for err in validator.iter_errors(instance)]

    def draft_dir(self, workspace: Path) -> Path:
        return find_draft_dir(workspace, self.name)

    def draft_handler_path(self, workspace: Path) -> Path:
        return self.draft_dir(workspace) / HANDLER_FILENAME

    def draft_manifest_path(self, workspace: Path) -> Path:
        return find_manifest_path(self.draft_dir(workspace))

    def relative_handler_path(self, workspace: Path | None = None) -> str:
        if workspace is not None:
            draft = find_draft_dir(workspace, self.name)
            rel = draft.relative_to(Path(workspace))
            return f"{rel.as_posix()}/{HANDLER_FILENAME}"
        return f"{DRAFT_COILS_DIR}/{self.name}/{HANDLER_FILENAME}"


def _parse_manifest_dict(
    data: dict[str, Any], *, expected_name: str | None = None
) -> RouteManifest:
    manifest = RouteManifest.model_validate(data)
    if expected_name is not None and manifest.name != expected_name:
        raise ValueError(
            f"Route name {manifest.name!r} does not match directory {expected_name!r}"
        )
    return manifest


def load_route_manifest(
    path: Path, *, expected_name: str | None = None
) -> RouteManifest | None:
    """Load one coil manifest; return None on any parse/validation failure."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("coil manifest must be a JSON object")
        return _parse_manifest_dict(data, expected_name=expected_name)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        logger.warning("Skipping invalid coil manifest %s: %s", path, exc)
        return None


def load_coil_manifest(
    workspace: Path | str, name: str
) -> RouteManifest | None:
    """Load a draft coil by name, honoring legacy locations and filenames."""
    draft = find_draft_dir(Path(workspace), name)
    return load_route_manifest(find_manifest_path(draft), expected_name=name)


def load_route_manifests(
    workspace: Path, *, published_only: bool = False
) -> dict[str, RouteManifest]:
    """Load draft or published route manifests.

    Invalid files are skipped with a warning so a broken draft cannot take down
    tool building or the HTTP server.
    """
    workspace = Path(workspace)
    if published_only:
        from .registry import load_published_registry

        registry = load_published_registry(workspace)
        manifests: dict[str, RouteManifest] = {}
        for name, entry in registry.routes.items():
            try:
                manifests[name] = RouteManifest.model_validate(entry.manifest)
            except ValidationError as exc:
                logger.warning(
                    "Skipping invalid published route %s: %s", name, exc
                )
        return manifests

    manifests = {}
    for dirname in (DRAFT_COILS_DIR, LEGACY_DRAFT_DIR):
        drafts_root = workspace / dirname
        if not drafts_root.is_dir():
            continue
        for child in sorted(drafts_root.iterdir()):
            if not child.is_dir() or child.name in manifests:
                continue
            manifest_path = find_manifest_path(child)
            if not manifest_path.is_file():
                continue
            manifest = load_route_manifest(manifest_path, expected_name=child.name)
            if manifest is None:
                continue
            manifests[manifest.name] = manifest
    return manifests
