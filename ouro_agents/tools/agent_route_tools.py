"""Agent tools for running coils and publishing them as Ouro routes."""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from smolagents import tool

from ..agent_routes.executor import execute_agent_route
from ..agent_routes.manifest import (
    HANDLER_FILENAME,
    find_draft_dir,
    load_coil_manifest,
    load_route_manifests,
)
from ..agent_routes.registry import (
    load_published_registry,
    next_version,
    save_published_registry,
    snapshot_route,
)

if TYPE_CHECKING:
    from ..config import AgentRoutesConfig

logger = logging.getLogger(__name__)


def build_coil_directory(workspace: Path | str) -> str:
    """Render the coil index for the system prompt.

    Lists draft coils with their published status so the agent checks here
    before re-deriving repeated multi-step work by hand.
    """
    workspace = Path(workspace)
    manifests = load_route_manifests(workspace)
    if not manifests:
        return (
            "(none yet — author one under coils/<name>/; "
            "load the `coils` skill for the contract)"
        )
    registry = load_published_registry(workspace)
    lines = []
    for name, manifest in sorted(manifests.items()):
        required = ", ".join(manifest.required_inputs) or "(none)"
        desc = manifest.description or "(no description)"
        entry = registry.get(name)
        status = f"published v{entry.version}" if entry else "draft"
        lines.append(f"- {name} [{status}]: {desc} (required inputs: {required})")
    return "\n".join(lines)


def _validate_handler_source(source: str) -> str | None:
    """Return an error string if handler.py is not valid; else None."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"handler.py has a syntax error: {exc}"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "handler":
            args = node.args
            positional = len(args.posonlyargs) + len(args.args)
            # Allow defaults; require at least 2 positional/keyword args.
            if positional < 2 and args.vararg is None:
                return (
                    "handler(params, context) must accept at least two positional "
                    "arguments"
                )
            return None
    return "handler.py must define a top-level function handler(params, context)"


def make_run_coil_tool(workspace: Path, executor: Any):
    """Create the ``run_coil`` tool bound to a sandbox executor."""
    workspace = Path(workspace)

    @tool
    def run_coil(name: str, params: dict) -> str:
        """Execute one of your saved coils (workspace/coils/<name>/) in the sandbox.

        Coils compress repeated multi-step Ouro work into a single call.
        Your COILS index (in context) lists what exists. Handlers run with
        get_ouro_client() available (ouro-py), same as run_python.

        Args:
            name: Coil directory name under coils/.
            params: JSON object matching the coil's inputs schema.
        """
        name = str(name or "").strip()
        if not name:
            return "Error: coil name is required."
        if not isinstance(params, dict):
            return "Error: params must be a JSON object."

        manifest = load_coil_manifest(workspace, name)
        if manifest is None:
            available = ", ".join(sorted(load_route_manifests(workspace))) or "(none)"
            return (
                f"Error: unknown or invalid coil {name!r}. "
                f"Available drafts: {available}"
            )

        violations = manifest.validate_params(params)
        if violations:
            joined = "; ".join(violations[:8])
            more = f" (+{len(violations) - 8} more)" if len(violations) > 8 else ""
            return (
                f"Error: params failed JSON Schema validation for {name}: "
                f"{joined}{more}"
            )

        handler_rel = manifest.relative_handler_path(workspace)
        handler_abs = workspace / handler_rel
        if not handler_abs.is_file():
            return f"Error: missing handler at {handler_rel}"

        context = {
            "route_name": name,
            "source": "tool",
            "user_id": None,
            "action_id": None,
            "route_id": None,
            "org_id": None,
            "team_id": None,
        }
        result = execute_agent_route(
            executor,
            handler_path=handler_rel,
            params=params,
            context=context,
        )
        return json.dumps(result, default=str, indent=2)

    run_coil.description += """

Authoring:
- Put files in coils/<name>/coil.json and coils/<name>/handler.py
- handler.py must define: def handler(params: dict, context: dict) -> dict
- Use get_ouro_client() inside the handler (ouro-py, not MCP)
- Load the `coils` skill for templates and the MCP→SDK mapping table
- Publish a coil as a live Ouro route with publish_route(name)
"""
    return run_coil


def _service_base_url(public_base_url: str | None, path_prefix: str) -> str:
    base = (public_base_url or "").rstrip("/")
    prefix = path_prefix
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    return f"{base}{prefix}"


def _resolve_service_id(service: Any) -> str:
    """Extract id from a Service model or dict without eager subscripting.

    ``getattr(obj, "id", obj["id"])`` evaluates the default before the call, so
    a real Service (not subscriptable) raises TypeError even when ``.id`` exists.
    """
    sid = getattr(service, "id", None)
    if sid is None and isinstance(service, dict):
        sid = service.get("id")
    if not sid:
        raise ValueError("service response missing id")
    return str(sid)


def _sync_ouro_auth(
    ouro_client: Any,
    *,
    service_id: str,
    serve_token: str,
) -> str:
    """Sync AGENT_ROUTES_SERVE_TOKEN into the service's Ouro auth via the API."""
    set_auth = getattr(
        getattr(ouro_client, "services", None), "set_authentication", None
    )
    if not callable(set_auth):
        return (
            "WARNING: ouro-py client has no services.set_authentication; "
            "upgrade ouro-py and retry publish_route to provision Ouro auth."
        )
    try:
        result = set_auth(service_id, serve_token, method="Ouro")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ouro auth sync failed for service %s: %s", service_id, exc)
        return (
            f"WARNING: could not sync Ouro auth for service {service_id}: "
            f"{type(exc).__name__}: {exc}. Ensure AGENT_ROUTES_SERVE_TOKEN is set "
            "and retry publish_route."
        )

    payload = result
    if hasattr(result, "model_dump") and callable(result.model_dump):
        try:
            payload = result.model_dump()
        except Exception:  # noqa: BLE001
            payload = result
    if not isinstance(payload, dict):
        return f"Synced Ouro auth for service {service_id}."

    rotated = payload.get("rotated")
    secret_id = payload.get("secret_id")
    if rotated is False:
        return (
            f"Ouro auth already in sync for service {service_id}"
            + (f" (secret_id={secret_id})." if secret_id else ".")
        )
    if rotated is True:
        return (
            f"Rotated Ouro auth for service {service_id}"
            + (f" (secret_id={secret_id})." if secret_id else ".")
        )
    return (
        f"Provisioned Ouro auth for service {service_id}"
        + (f" (secret_id={secret_id})." if secret_id else ".")
    )


def make_publish_route_tools(
    workspace: Path,
    *,
    routes_config: "AgentRoutesConfig",
    agent_name: str,
    ouro_client: Any | None,
    allow_publish: bool = True,
    public_base_url: str | None = None,
):
    """Create publish_route / unpublish_route tools."""
    workspace = Path(workspace)

    tools: list = []

    if allow_publish:

        @tool
        def publish_route(
            name: str,
            org_id: Optional[str] = None,
            team_id: Optional[str] = None,
        ) -> str:
            """Publish a draft coil so others can call it as an Ouro service route.

            Snapshots coils/<name>/ into protected/published_routes/<name>/vN/,
            updates the local registry, and syncs the agent's <agent>-routes
            service on Ouro from the live OpenAPI spec.

            On the first publish (no service yet), pass org_id and team_id — pick
            them with get_organizations / get_teams the same way you would for
            create_post. Later publishes re-sync the existing service and ignore
            org/team.

            Args:
                name: Draft coil name under coils/.
                org_id: Organization for the service (required on first publish).
                team_id: Team for the service (required on first publish).
            """
            name = str(name or "").strip()
            if not name:
                return "Error: coil name is required."
            if not public_base_url:
                return (
                    "Error: server.public_base_url is not configured; "
                    "cannot publish."
                )
            if ouro_client is None:
                return "Error: Ouro client is not available; cannot register the service."

            manifest = load_coil_manifest(workspace, name)
            if manifest is None:
                return f"Error: unknown or invalid draft coil {name!r}."
            if manifest.timeout_seconds > routes_config.request_timeout_seconds:
                return (
                    f"Error: coil timeout_seconds={manifest.timeout_seconds} exceeds "
                    f"agent_routes.request_timeout_seconds="
                    f"{routes_config.request_timeout_seconds}."
                )

            handler_path = find_draft_dir(workspace, name) / HANDLER_FILENAME
            if not handler_path.is_file():
                return f"Error: missing {handler_path.relative_to(workspace)}"
            handler_err = _validate_handler_source(
                handler_path.read_text(encoding="utf-8")
            )
            if handler_err:
                return f"Error: {handler_err}"

            registry = load_published_registry(workspace)
            previous_entry = registry.get(name)
            version = next_version(registry, name)
            # Snapshot before register so OpenAPI includes this route; roll back
            # the registry entry on failure so failed publishes don't burn versions.
            entry = snapshot_route(workspace, manifest, version=version)
            registry.routes[name] = entry
            save_published_registry(workspace, registry)

            base_url = _service_base_url(public_base_url, routes_config.path_prefix)
            spec_url = f"{base_url}/openapi.json"
            service_name = f"{agent_name}-routes"
            org = (org_id or "").strip() or None
            team = (team_id or "").strip() or None

            def _service_payload(**extra: Any) -> dict[str, Any]:
                payload: dict[str, Any] = {
                    "name": service_name,
                    "base_url": base_url,
                    "authentication": "Ouro",
                    "spec_url": spec_url,
                    "description": (
                        f"Native agent-authored routes for {agent_name}."
                    ),
                    "attribution": {"originality": "original"},
                }
                payload.update(extra)
                return payload

            def _adopt_existing_service() -> Any | None:
                """Find an existing <agent>-routes service owned by this agent."""
                search = getattr(getattr(ouro_client, "assets", None), "search", None)
                if not callable(search):
                    return None
                try:
                    found = search(
                        query=service_name,
                        asset_type="service",
                        scope="personal",
                        limit=10,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not search for existing service: %s", exc)
                    return None
                results = getattr(found, "data", None)
                if results is None and isinstance(found, dict):
                    results = found.get("data") or found.get("results") or []
                if results is None and isinstance(found, list):
                    results = found
                for item in results or []:
                    item_name = getattr(item, "name", None)
                    if item_name is None and isinstance(item, dict):
                        item_name = item.get("name")
                    if item_name != service_name:
                        continue
                    sid = getattr(item, "id", None)
                    if sid is None and isinstance(item, dict):
                        sid = item.get("id")
                    if not sid:
                        continue
                    return ouro_client.services.update(
                        str(sid), **_service_payload()
                    )
                return None

            def _rollback_registry() -> None:
                if previous_entry is None:
                    registry.routes.pop(name, None)
                else:
                    registry.routes[name] = previous_entry
                save_published_registry(workspace, registry)

            action = "updated"
            location_note = ""
            try:
                if registry.service_id:
                    service = ouro_client.services.update(
                        registry.service_id,
                        **_service_payload(),
                    )
                    action = "updated"
                else:
                    # Prefer adopting a prior manual create before requiring org/team.
                    adopted = _adopt_existing_service()
                    if adopted is not None:
                        service = adopted
                        action = "updated (adopted existing)"
                        registry.service_id = _resolve_service_id(service)
                        save_published_registry(workspace, registry)
                    else:
                        if not org or not team:
                            _rollback_registry()
                            return (
                                "Error: first publish requires org_id and team_id. "
                                "Call get_organizations / get_teams, pick where this "
                                "service should live, then retry publish_route with "
                                "those ids."
                            )
                        try:
                            service = ouro_client.services.create(
                                **_service_payload(
                                    visibility="public",
                                    org_id=org,
                                    team_id=team,
                                ),
                            )
                            action = "created"
                        except Exception as create_exc:  # noqa: BLE001
                            adopted = _adopt_existing_service()
                            if adopted is None:
                                raise create_exc
                            service = adopted
                            action = "updated (adopted existing)"
                        registry.service_id = _resolve_service_id(service)
                        save_published_registry(workspace, registry)
                        location_note = f"\n- Location: org={org} team={team}"
            except Exception as exc:  # noqa: BLE001
                _rollback_registry()
                return (
                    f"Ouro service registration failed: {type(exc).__name__}: {exc}. "
                    "Registry was rolled back (snapshot files may remain under "
                    f"protected/published_routes/{name}/v{version}/). "
                    "Fix the error and call publish_route again."
                )

            service_id = registry.service_id or _resolve_service_id(service)
            serve_token = os.environ.get(routes_config.serve_token_env) or ""
            auth_note = ""
            # Sync serve token on every successful publish (idempotent upsert).
            if not serve_token:
                auth_note = (
                    f"\nWARNING: {routes_config.serve_token_env} is unset. "
                    "Generate one (`openssl rand -hex 32`), put it in the agent "
                    "env, restart the agent, and call publish_route again so "
                    f"Ouro auth can be synced for service_id={service_id}."
                )
            else:
                auth_note = "\n" + _sync_ouro_auth(
                    ouro_client,
                    service_id=service_id,
                    serve_token=serve_token,
                )

            return (
                f"Published {name} v{version}.\n"
                f"- Live URL: {base_url}/{name}\n"
                f"- OpenAPI: {spec_url}\n"
                f"- Service {action}: {service_id} ({service_name})"
                f"{location_note}\n"
                f"- Snapshot: {entry.handler_path}"
                f"{auth_note}"
            )

        tools.append(publish_route)

        @tool
        def unpublish_route(name: str) -> str:
            """Remove a route from the published registry (snapshots are kept).

            The next publish_route (or an empty re-sync) drops it from the Ouro
            service OpenAPI. Call this when a published route should stop being
            served.

            Args:
                name: Published route name.
            """
            name = str(name or "").strip()
            if not name:
                return "Error: route name is required."
            registry = load_published_registry(workspace)
            if name not in registry.routes:
                return f"Route {name!r} is not published."
            del registry.routes[name]
            save_published_registry(workspace, registry)

            if (
                ouro_client is not None
                and registry.service_id
                and public_base_url
            ):
                base_url = _service_base_url(
                    public_base_url, routes_config.path_prefix
                )
                try:
                    ouro_client.services.update(
                        registry.service_id,
                        name=f"{agent_name}-routes",
                        base_url=base_url,
                        authentication="Ouro",
                        spec_url=f"{base_url}/openapi.json",
                    )
                    sync_note = " Ouro service re-synced from OpenAPI."
                except Exception as exc:  # noqa: BLE001
                    sync_note = (
                        f" Local registry updated, but Ouro re-sync failed: {exc}. "
                        "Call publish_route on another route (or retry) to sync."
                    )
            else:
                sync_note = (
                    " Local registry updated; Ouro will drop the route on the next "
                    "spec sync."
                )
            return f"Unpublished {name}.{sync_note}"

        tools.append(unpublish_route)

    return tools
