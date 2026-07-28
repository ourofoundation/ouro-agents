"""Hand-built OpenAPI 3.1 specs for published agent routes."""

from __future__ import annotations

from typing import Any

from .manifest import RouteManifest


def _snake_case(name: str) -> str:
    return name.replace("-", "_")


def display_name_from_slug(name: str) -> str:
    """Humanize a kebab slug into a short Ouro route name.

    OpenAPI ``summary`` becomes the Ouro asset name on spec sync, so keep it
    short and action-oriented — sentence case, never the long description.
    Example: ``load-asset-comments`` → ``Load asset comments``.
    """
    parts = [p for p in str(name or "").split("-") if p]
    if not parts:
        return str(name or "")
    words = [p.lower() for p in parts]
    words[0] = words[0].capitalize()
    return " ".join(words)


def build_openapi_spec(
    agent_name: str,
    public_base_url: str,
    path_prefix: str,
    manifests: dict[str, RouteManifest],
    *,
    version: str | int | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI 3.1 document for published agent routes.

    ``public_base_url`` is the agent public root (e.g.
    ``https://agents.ouro.foundation/apollo``). ``path_prefix`` is typically
    ``/routes``. The server URL in the spec is ``public_base_url + path_prefix``.
    """
    base = public_base_url.rstrip("/")
    prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    server_url = f"{base}{prefix}"
    if version is None:
        version = "1"
    version_str = str(version)

    paths: dict[str, Any] = {}
    for name in sorted(manifests):
        manifest = manifests[name]
        # summary → Ouro route asset name (must stay short)
        # description → longer docs text
        title = display_name_from_slug(name)
        operation: dict[str, Any] = {
            "operationId": _snake_case(name),
            "summary": title,
            "description": (manifest.description or title).strip(),
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": manifest.inputs,
                    }
                },
            },
            "responses": {
                "200": {
                    "description": "Successful response",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object"},
                        }
                    },
                }
            },
        }
        if manifest.input_assets:
            operation["x-ouro-input-assets"] = manifest.input_assets
        if manifest.output_assets:
            operation["x-ouro-output-assets"] = manifest.output_assets
        paths[f"/{name}"] = {"post": operation}

    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{agent_name}-routes",
            "version": version_str,
            "description": (
                f"Agent-authored routes served by {agent_name}. "
                "Handlers run in the agent's Docker sandbox."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": paths,
    }
