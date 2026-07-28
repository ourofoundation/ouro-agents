"""HTTP serving of published agent routes from the agent FastAPI server."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import AgentRoutesConfig, OuroAgentsConfig, SandboxConfig
from .executor import execute_agent_route
from .openapi import build_openapi_spec
from .registry import load_published_registry

logger = logging.getLogger(__name__)


class AgentRoutesServer:
    """Owns the dedicated sandbox session used for inbound route calls."""

    def __init__(
        self,
        *,
        config: OuroAgentsConfig,
        workspace,
        sandbox_factory=None,
    ) -> None:
        self.config = config
        self.routes_config: AgentRoutesConfig = config.agent_routes
        self.workspace = workspace
        self._sandbox_factory = sandbox_factory
        self._session: Any | None = None
        self._semaphore = asyncio.Semaphore(
            self.routes_config.max_concurrent_requests
        )
        self._lock = asyncio.Lock()

    def _serve_token(self) -> str | None:
        env_name = self.routes_config.serve_token_env
        token = os.environ.get(env_name)
        if not token:
            return None
        return token

    def _check_auth(self, authorization: str | None) -> None:
        token = self._serve_token()
        if not token:
            logger.error(
                "Agent routes serving refused: env %s is unset",
                self.routes_config.serve_token_env,
            )
            raise HTTPException(
                status_code=503,
                detail="Agent routes serving is not configured (missing serve token)",
            )
        expected = f"Basic {token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        if self._sandbox_factory is not None:
            self._session = self._sandbox_factory()
            return self._session

        from ..tools.docker_sandbox import DockerSandboxSession

        base = self.config.agent.sandbox
        serve_cfg: SandboxConfig = base.model_copy(
            update={"timeout_seconds": self.routes_config.request_timeout_seconds}
        )
        self._session = DockerSandboxSession(
            config=serve_cfg,
            workspace=self.workspace,
            agent_name=self.config.agent.name,
            run_id="routes-serve",
        )
        return self._session

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None and hasattr(session, "close"):
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to close agent-routes sandbox: %s", exc)

    def openapi_document(self) -> dict:
        registry = load_published_registry(self.workspace)
        manifests = registry.manifests()
        public_base = self.config.server.public_base_url or ""
        return build_openapi_spec(
            self.config.agent.name,
            public_base,
            self.routes_config.path_prefix,
            manifests,
            version=registry.max_version(),
        )

    async def invoke(
        self,
        route_name: str,
        body: dict,
        *,
        authorization: str | None,
        headers: dict[str, str],
    ) -> JSONResponse:
        self._check_auth(authorization)
        registry = load_published_registry(self.workspace)
        entry = registry.get(route_name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown route: {route_name}")

        context = {
            "route_name": route_name,
            "source": "http",
            "user_id": headers.get("ouro-user-id") or None,
            "action_id": headers.get("ouro-action-id") or None,
            "route_id": headers.get("ouro-route-id") or None,
            "org_id": headers.get("ouro-route-org-id") or None,
            "team_id": headers.get("ouro-route-team-id") or None,
        }
        params = body if isinstance(body, dict) else {}

        async with self._semaphore:
            async with self._lock:
                session = self._ensure_session()

            def _run() -> dict:
                return execute_agent_route(
                    session,
                    handler_path=entry.handler_path,
                    params=params,
                    context=context,
                )

            try:
                result = await asyncio.to_thread(_run)
            except TimeoutError as exc:
                return JSONResponse(
                    status_code=504,
                    content={
                        "error": {
                            "type": "TimeoutError",
                            "message": str(exc) or "Route timed out",
                        }
                    },
                )

        if isinstance(result, dict) and "error" in result:
            return JSONResponse(status_code=500, content=result)
        return JSONResponse(status_code=200, content=result)


def mount_prefix_from_public_base(public_base_url: str | None, path_prefix: str) -> str:
    """Derive the FastAPI mount path from the public URL + path_prefix.

    Example: ``https://agents.ouro.foundation/apollo`` + ``/routes``
    → ``/apollo/routes``.
    """
    prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
    if not public_base_url:
        return prefix
    path = urlparse(public_base_url).path.rstrip("/")
    if not path:
        return prefix
    return f"{path}{prefix}"


def build_agent_routes_router(
    config: OuroAgentsConfig,
    workspace,
    *,
    server: AgentRoutesServer | None = None,
) -> tuple[APIRouter, AgentRoutesServer]:
    """Return ``(router, server)``. Caller must ``close()`` the server on shutdown."""
    routes_server = server or AgentRoutesServer(config=config, workspace=workspace)
    mount = mount_prefix_from_public_base(
        config.server.public_base_url, config.agent_routes.path_prefix
    )
    # Router is mounted at ``mount``; paths below are relative to that.
    router = APIRouter(tags=["agent-routes"])

    @router.get("/openapi.json")
    async def openapi_json():
        return routes_server.openapi_document()

    @router.post("/{route_name}")
    async def invoke_route(
        route_name: str,
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        header_map = {k.lower(): v for k, v in request.headers.items()}
        return await routes_server.invoke(
            route_name,
            body,
            authorization=authorization,
            headers=header_map,
        )

    # Store mount path on the router for lifespan to include_router(..., prefix=)
    router.ouro_mount_prefix = mount  # type: ignore[attr-defined]
    return router, routes_server
