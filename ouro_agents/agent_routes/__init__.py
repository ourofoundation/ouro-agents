"""Agent-authored routes: draft handlers as tools, publish as Ouro services."""

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
    "RouteManifest",
    "build_openapi_spec",
    "execute_agent_route",
    "load_published_registry",
    "load_route_manifest",
    "load_route_manifests",
    "save_published_registry",
]
