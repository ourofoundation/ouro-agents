"""Agent tools for running and publishing workspace routes."""

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
    load_route_manifest,
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


def _format_route_directory(manifests: dict) -> str:
    if not manifests:
        return "(none yet — author one under routes/<name>/; see the agent-routes skill)"
    lines = []
    for name, manifest in sorted(manifests.items()):
        required = ", ".join(manifest.required_inputs) or "(none)"
        desc = manifest.description or "(no description)"
        lines.append(f"- {name}: {desc} (required inputs: {required})")
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


def make_run_route_tool(workspace: Path, executor: Any):
    """Create the ``run_route`` tool bound to a sandbox executor."""
    workspace = Path(workspace)
    manifests = load_route_manifests(workspace)
    directory = _format_route_directory(manifests)

    @tool
    def run_route(name: str, params: dict) -> str:
        """Execute one of your saved routes (workspace/routes/<name>/) in the sandbox.

        Use this to compress repeated multi-step Ouro work into a single call.
        Handlers run with get_ouro_client() available (ouro-py), same as run_python.

        Args:
            name: Route directory name under routes/.
            params: JSON object matching the route's inputs schema.
        """
        name = str(name or "").strip()
        if not name:
            return "Error: route name is required."
        if not isinstance(params, dict):
            return "Error: params must be a JSON object."

        manifest_path = workspace / "routes" / name / "route.json"
        manifest = load_route_manifest(manifest_path, expected_name=name)
        if manifest is None:
            available = ", ".join(sorted(load_route_manifests(workspace))) or "(none)"
            return (
                f"Error: unknown or invalid route {name!r}. "
                f"Available drafts: {available}"
            )

        missing = manifest.missing_required(params)
        if missing:
            return (
                f"Error: missing required inputs for {name}: {', '.join(missing)}. "
                f"Required: {', '.join(manifest.required_inputs) or '(none)'}"
            )

        handler_rel = manifest.relative_handler_path()
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

    run_route.description += f"""

Available draft routes:
{directory}

Authoring:
- Put files in routes/<name>/route.json and routes/<name>/handler.py
- handler.py must define: def handler(params: dict, context: dict) -> dict
- Use get_ouro_client() inside the handler (ouro-py, not MCP)
- Load the `agent-routes` skill for templates and the MCP→SDK mapping table
"""
    return run_route


def _service_base_url(public_base_url: str | None, path_prefix: str) -> str:
    base = (public_base_url or "").rstrip("/")
    prefix = path_prefix
    if not prefix.startswith("/"):
        prefix = f"/{prefix}"
    return f"{base}{prefix}"


def _provision_ouro_auth(
    ouro_client: Any,
    *,
    service_id: str,
    serve_token: str,
) -> str:
    """Best-effort vault + authentications insert; return operator instructions otherwise."""
    user_id = None
    try:
        user = getattr(ouro_client, "user", None)
        user_id = getattr(user, "id", None) if user is not None else None
    except Exception:  # noqa: BLE001
        user_id = None

    # ouro-py does not expose vault RPCs; try raw supabase if present.
    supabase = getattr(ouro_client, "supabase", None) or getattr(
        ouro_client, "db", None
    )
    if supabase is not None and user_id:
        try:
            secret_resp = supabase.rpc("insert_secret", {"secret": serve_token}).execute()
            secret_id = secret_resp.data
            if isinstance(secret_id, list) and secret_id:
                secret_id = secret_id[0]
            if isinstance(secret_id, dict):
                secret_id = secret_id.get("insert_secret") or secret_id.get("id")
            supabase.table("authentications").insert(
                {
                    "user_id": str(user_id),
                    "service_id": str(service_id),
                    "secret_id": str(secret_id),
                    "method": "Ouro",
                }
            ).execute()
            return (
                f"Provisioned Ouro auth for service {service_id} "
                f"(secret_id={secret_id})."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Automatic Ouro auth provisioning failed: %s", exc)

    sql = (
        "-- Run as the agent user (or via service role) after first publish:\n"
        "WITH s AS (\n"
        "  INSERT INTO vault.secrets (secret)\n"
        f"  VALUES ('{serve_token}')\n"
        "  RETURNING id\n"
        ")\n"
        "INSERT INTO public.authentications (user_id, service_id, secret_id, method)\n"
        "SELECT '<AGENT_USER_ID>', "
        f"'{service_id}', id, 'Ouro' FROM s\n"
        "ON CONFLICT DO NOTHING;\n"
        "-- Also ensure AGENT_ROUTES_SERVE_TOKEN in the agent env matches this secret.\n"
    )
    return (
        "Could not auto-provision vault auth. Set AGENT_ROUTES_SERVE_TOKEN in the "
        "agent env to the service Basic token, then insert the authentications row. "
        f"Suggested SQL:\n{sql}"
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
            """Publish a draft route so others can call it as an Ouro service route.

            Snapshots routes/<name>/ into protected/published_routes/<name>/vN/,
            updates the local registry, and syncs the agent's <agent>-routes
            service on Ouro from the live OpenAPI spec.

            On the first publish (no service yet), pass org_id and team_id — pick
            them with get_organizations / get_teams the same way you would for
            create_post. Later publishes re-sync the existing service and ignore
            org/team.

            Args:
                name: Draft route name under routes/.
                org_id: Organization for the service (required on first publish).
                team_id: Team for the service (required on first publish).
            """
            name = str(name or "").strip()
            if not name:
                return "Error: route name is required."
            if not public_base_url:
                return (
                    "Error: server.public_base_url is not configured; "
                    "cannot publish."
                )
            if ouro_client is None:
                return "Error: Ouro client is not available; cannot register the service."

            manifest_path = workspace / "routes" / name / "route.json"
            manifest = load_route_manifest(manifest_path, expected_name=name)
            if manifest is None:
                return f"Error: unknown or invalid draft route {name!r}."
            if manifest.timeout_seconds > routes_config.request_timeout_seconds:
                return (
                    f"Error: route timeout_seconds={manifest.timeout_seconds} exceeds "
                    f"agent_routes.request_timeout_seconds="
                    f"{routes_config.request_timeout_seconds}."
                )

            handler_path = workspace / "routes" / name / HANDLER_FILENAME
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
            needs_auth = False
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
                        registry.service_id = str(
                            getattr(service, "id", service["id"])
                        )
                        save_published_registry(workspace, registry)
                        needs_auth = True
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
                        registry.service_id = str(
                            getattr(service, "id", service["id"])
                        )
                        save_published_registry(workspace, registry)
                        location_note = f"\n- Location: org={org} team={team}"
                        needs_auth = True
            except Exception as exc:  # noqa: BLE001
                _rollback_registry()
                return (
                    f"Ouro service registration failed: {type(exc).__name__}: {exc}. "
                    "Registry was rolled back (snapshot files may remain under "
                    f"protected/published_routes/{name}/v{version}/). "
                    "Fix the error and call publish_route again."
                )

            service_id = registry.service_id or str(getattr(service, "id", ""))
            serve_token = os.environ.get(routes_config.serve_token_env) or ""
            auth_note = ""
            if needs_auth:
                if not serve_token:
                    auth_note = (
                        "\nWARNING: AGENT_ROUTES_SERVE_TOKEN is unset. Generate one "
                        "(`openssl rand -hex 32`), put it in the agent env, and "
                        "insert an authentications row (method='Ouro') for this "
                        f"service_id={service_id}."
                    )
                else:
                    auth_note = "\n" + _provision_ouro_auth(
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
