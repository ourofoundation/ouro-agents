"""Agent-authored routes: draft handlers as tools, publish as Ouro services."""

from .candidates import RouteCandidate, mine_route_candidates, write_route_candidates_skill
from .executor import execute_agent_route
from .manifest import RouteManifest, load_route_manifest, load_route_manifests
from .openapi import build_openapi_spec
from .registry import (
    PublishedRouteEntry,
    PublishedRoutesRegistry,
    load_published_registry,
    save_published_registry,
)

__all__ = [
    "PublishedRouteEntry",
    "PublishedRoutesRegistry",
    "RouteCandidate",
    "RouteManifest",
    "build_openapi_spec",
    "execute_agent_route",
    "load_published_registry",
    "load_route_manifest",
    "load_route_manifests",
    "mine_route_candidates",
    "save_published_registry",
    "write_route_candidates_skill",
]
