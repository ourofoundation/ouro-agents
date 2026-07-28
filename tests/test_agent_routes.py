"""Tests for agent-authored routes."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ouro_agents.agent_routes.candidates import (
    EXCLUDED_TOOLS,
    filter_new_candidates,
    mark_candidates_suggested,
    mine_route_candidates,
)
from ouro_agents.agent_routes.executor import execute_agent_route
from ouro_agents.agent_routes.http import (
    AgentRoutesServer,
    build_agent_routes_router,
    mount_prefix_from_public_base,
)
from ouro_agents.agent_routes.manifest import load_route_manifests
from ouro_agents.agent_routes.openapi import (
    build_openapi_spec,
    display_name_from_slug,
)
from ouro_agents.agent_routes.registry import (
    load_published_registry,
    next_version,
    save_published_registry,
    snapshot_route,
)
from ouro_agents.config import AgentRoutesConfig, OuroAgentsConfig
from ouro_agents.tools.agent_route_tools import (
    _validate_handler_source,
    make_publish_route_tools,
    make_run_route_tool,
)
from ouro_agents.tools.workspace_layout import check_workspace_write


def _write_draft(
    workspace: Path,
    name: str,
    *,
    description: str = "demo",
    title: str | None = None,
    inputs: dict | None = None,
) -> None:
    route_dir = workspace / "routes" / name
    route_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "description": description,
        "timeout_seconds": 30,
        "inputs": inputs
        or {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["asset_id"],
        },
        "input_assets": {
            "file": {
                "asset_type": "file",
                "primary": True,
                "file_extensions": ["cif"],
            }
        },
    }
    if title is not None:
        payload["title"] = title
    (route_dir / "route.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (route_dir / "handler.py").write_text(
        "def handler(params, context):\n"
        "    return {'asset_id': params.get('asset_id'), 'ok': True}\n",
        encoding="utf-8",
    )


class FakeSession:
    def __init__(self, output="{\"ok\": true}"):
        self.output_value = output
        self.calls: list[str] = []

    def execute(self, code: str):
        self.calls.append(code)
        result = MagicMock()
        result.output = self.output_value
        result.logs = ""
        result.stderr = ""
        return result

    def __call__(self, code: str):
        return self.execute(code)

    def close(self):
        pass


class TestManifestLoading(unittest.TestCase):
    def test_loads_valid_and_skips_invalid(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            bad = workspace / "routes" / "bad-name"
            bad.mkdir(parents=True)
            (bad / "route.json").write_text(
                json.dumps({"name": "other-name", "inputs": {"type": "object"}}),
                encoding="utf-8",
            )
            (workspace / "routes" / "broken").mkdir()
            (workspace / "routes" / "broken" / "route.json").write_text(
                "{not json", encoding="utf-8"
            )
            manifests = load_route_manifests(workspace)
            self.assertIn("load-context", manifests)
            self.assertNotIn("bad-name", manifests)
            self.assertNotIn("broken", manifests)

    def test_name_dir_mismatch_skipped(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            route_dir = workspace / "routes" / "alpha"
            route_dir.mkdir(parents=True)
            (route_dir / "route.json").write_text(
                json.dumps(
                    {
                        "name": "beta",
                        "inputs": {"type": "object", "properties": {}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_route_manifests(workspace), {})

    def test_title_and_invalid_schema_skipped(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context", title="Load thread context")
            bad_schema = workspace / "routes" / "bad-schema"
            bad_schema.mkdir(parents=True)
            (bad_schema / "route.json").write_text(
                json.dumps(
                    {
                        "name": "bad-schema",
                        "inputs": {
                            "type": "object",
                            "properties": {"x": {"type": "not-a-type"}},
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifests = load_route_manifests(workspace)
            self.assertEqual(manifests["load-context"].title, "Load thread context")
            self.assertNotIn("bad-schema", manifests)

    def test_validate_params(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            manifest = load_route_manifests(workspace)["load-context"]
            self.assertTrue(manifest.validate_params({}))
            self.assertEqual(manifest.validate_params({"asset_id": "abc"}), [])
            self.assertTrue(
                any("limit" in v for v in manifest.validate_params({"asset_id": "a", "limit": "nope"}))
            )
    def test_routes_writable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            check_workspace_write(root / "routes" / "x" / "handler.py", root)
            with self.assertRaises(PermissionError):
                check_workspace_write(root / "foo.py", root)
            with self.assertRaises(PermissionError):
                check_workspace_write(root / "protected" / "x.py", root)


class TestOpenAPI(unittest.TestCase):
    def test_builds_spec_with_ouro_extensions(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context", description="Load context")
            manifests = load_route_manifests(workspace)
            spec = build_openapi_spec(
                "apollo",
                "https://agents.ouro.foundation/apollo",
                "/routes",
                manifests,
                version=2,
            )
            self.assertEqual(spec["info"]["title"], "apollo-routes")
            self.assertEqual(spec["info"]["version"], "2")
            self.assertEqual(
                spec["servers"][0]["url"],
                "https://agents.ouro.foundation/apollo/routes",
            )
            op = spec["paths"]["/load-context"]["post"]
            self.assertEqual(op["operationId"], "load_context")
            # summary becomes the Ouro route name — short action title from slug
            self.assertEqual(op["summary"], "Load context")
            self.assertEqual(op["description"], "Load context")
            self.assertIn("x-ouro-input-assets", op)
            self.assertEqual(
                op["x-ouro-input-assets"]["file"]["asset_type"], "file"
            )

    def test_title_overrides_slug_humanize(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(
                workspace,
                "load-context",
                description="Longer docs text.",
                title="Fetch comments + asset",
            )
            manifests = load_route_manifests(workspace)
            spec = build_openapi_spec(
                "apollo",
                "https://agents.ouro.foundation/apollo",
                "/routes",
                manifests,
            )
            op = spec["paths"]["/load-context"]["post"]
            self.assertEqual(op["summary"], "Fetch comments + asset")
            self.assertEqual(op["description"], "Longer docs text.")

    def test_display_name_from_slug(self):
        self.assertEqual(
            display_name_from_slug("load-asset-comments"),
            "Load asset comments",
        )
        self.assertEqual(display_name_from_slug("score"), "Score")
        self.assertEqual(
            display_name_from_slug("fetch-team-feed"),
            "Fetch team feed",
        )


class TestExecutor(unittest.TestCase):
    def test_parses_json_result(self):
        session = FakeSession('{"hello": "world"}')
        out = execute_agent_route(
            session,
            handler_path="routes/demo/handler.py",
            params={"a": 1},
            context={"source": "tool"},
        )
        self.assertEqual(out, {"hello": "world"})
        self.assertTrue(session.calls)
        # Driver must JSON-normalize ouro-py models (UUID/datetime) before dumps.
        self.assertIn("def _jsonable", session.calls[0])
        self.assertIn('model_dump(mode="json")', session.calls[0])
        self.assertIn("json.dumps(_jsonable(_out), default=str)", session.calls[0])

    def test_timeout_as_error_dict(self):
        session = MagicMock()
        session.execute.side_effect = TimeoutError("boom")
        out = execute_agent_route(session, handler_path="routes/x/handler.py")
        self.assertEqual(out["error"]["type"], "TimeoutError")


class TestPublishSnapshot(unittest.TestCase):
    def test_versioning_and_registry(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            manifests = load_route_manifests(workspace)
            manifest = manifests["load-context"]
            registry = load_published_registry(workspace)
            self.assertEqual(next_version(registry, "load-context"), 1)
            entry = snapshot_route(workspace, manifest, version=1)
            registry.routes["load-context"] = entry
            registry.service_id = "svc-1"
            save_published_registry(workspace, registry)

            snap = workspace / "protected" / "published_routes" / "load-context" / "v1"
            self.assertTrue((snap / "handler.py").is_file())
            self.assertTrue((snap / "route.json").is_file())

            registry2 = load_published_registry(workspace)
            self.assertEqual(registry2.service_id, "svc-1")
            self.assertEqual(next_version(registry2, "load-context"), 2)

            entry2 = snapshot_route(workspace, manifest, version=2)
            registry2.routes["load-context"] = entry2
            save_published_registry(workspace, registry2)
            self.assertTrue(
                (
                    workspace
                    / "protected"
                    / "published_routes"
                    / "load-context"
                    / "v2"
                    / "handler.py"
                ).is_file()
            )

    def test_publish_tool_registers_service(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            ouro = MagicMock()
            created = MagicMock()
            created.id = "service-123"
            ouro.services.create.return_value = created
            routes_config = AgentRoutesConfig(
                enabled=True,
                path_prefix="/routes",
            )
            tools = make_publish_route_tools(
                workspace,
                routes_config=routes_config,
                agent_name="apollo",
                ouro_client=ouro,
                allow_publish=True,
                public_base_url="https://agents.ouro.foundation/apollo",
            )
            publish = next(t for t in tools if t.name == "publish_route")
            # First publish without org/team should fail clearly.
            missing = publish(name="load-context")
            self.assertIn("org_id and team_id", missing)
            result = publish(
                name="load-context", org_id="org-1", team_id="team-1"
            )
            self.assertIn("Published load-context v", result)
            self.assertIn("service-123", result)
            ouro.services.create.assert_called_once()
            kwargs = ouro.services.create.call_args.kwargs
            self.assertEqual(kwargs["authentication"], "Ouro")
            self.assertTrue(kwargs["spec_url"].endswith("/openapi.json"))
            self.assertEqual(kwargs["org_id"], "org-1")
            self.assertEqual(kwargs["team_id"], "team-1")
            self.assertEqual(
                kwargs["attribution"], {"originality": "original"}
            )

            registry = load_published_registry(workspace)
            self.assertEqual(registry.service_id, "service-123")

    def test_publish_adopts_existing_service_without_org_team(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            ouro = MagicMock()
            existing = MagicMock()
            existing.id = "service-existing"
            existing.name = "apollo-routes"
            ouro.assets.search.return_value = MagicMock(
                data=[existing]
            )
            ouro.services.update.return_value = existing
            ouro.services.create.side_effect = AssertionError("should not create")
            tools = make_publish_route_tools(
                workspace,
                routes_config=AgentRoutesConfig(enabled=True, path_prefix="/routes"),
                agent_name="apollo",
                ouro_client=ouro,
                allow_publish=True,
                public_base_url="https://agents.ouro.foundation/apollo",
            )
            publish = next(t for t in tools if t.name == "publish_route")
            result = publish(name="load-context")
            self.assertIn("adopted existing", result)
            self.assertIn("service-existing", result)
            ouro.services.create.assert_not_called()
            ouro.services.update.assert_called_once()
            registry = load_published_registry(workspace)
            self.assertEqual(registry.service_id, "service-existing")
            self.assertEqual(registry.routes["load-context"].version, 1)

    def test_publish_rolls_back_registry_on_register_failure(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            ouro = MagicMock()
            ouro.assets.search.return_value = MagicMock(data=[])
            ouro.services.create.side_effect = RuntimeError("boom")
            tools = make_publish_route_tools(
                workspace,
                routes_config=AgentRoutesConfig(enabled=True, path_prefix="/routes"),
                agent_name="apollo",
                ouro_client=ouro,
                allow_publish=True,
                public_base_url="https://agents.ouro.foundation/apollo",
            )
            publish = next(t for t in tools if t.name == "publish_route")
            result = publish(
                name="load-context", org_id="org-1", team_id="team-1"
            )
            self.assertIn("registration failed", result)
            self.assertIn("rolled back", result)
            registry = load_published_registry(workspace)
            self.assertNotIn("load-context", registry.routes)
            self.assertIsNone(registry.service_id)
            # Retry should still be v1, not v2
            ouro.services.create.side_effect = None
            created = MagicMock()
            created.id = "service-retry"
            ouro.services.create.return_value = created
            result2 = publish(
                name="load-context", org_id="org-1", team_id="team-1"
            )
            self.assertIn("Published load-context v1", result2)

    def test_publish_syncs_authentication(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            ouro = MagicMock()
            created = MagicMock()
            created.id = "service-auth"
            ouro.services.create.return_value = created
            ouro.services.set_authentication.return_value = {
                "id": "auth-1",
                "secret_id": "sec-1",
                "method": "Ouro",
                "rotated": False,
            }
            tools = make_publish_route_tools(
                workspace,
                routes_config=AgentRoutesConfig(
                    enabled=True,
                    path_prefix="/routes",
                    serve_token_env="AGENT_ROUTES_SERVE_TOKEN_TEST",
                ),
                agent_name="apollo",
                ouro_client=ouro,
                allow_publish=True,
                public_base_url="https://agents.ouro.foundation/apollo",
            )
            publish = next(t for t in tools if t.name == "publish_route")
            os.environ["AGENT_ROUTES_SERVE_TOKEN_TEST"] = "token-abc"
            try:
                result = publish(
                    name="load-context", org_id="org-1", team_id="team-1"
                )
                self.assertIn("Ouro auth already in sync", result)
                ouro.services.set_authentication.assert_called_once_with(
                    "service-auth", "token-abc", method="Ouro"
                )
            finally:
                os.environ.pop("AGENT_ROUTES_SERVE_TOKEN_TEST", None)

    def test_publish_warns_when_serve_token_unset(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            ouro = MagicMock()
            created = MagicMock()
            created.id = "service-warn"
            ouro.services.create.return_value = created
            os.environ.pop("AGENT_ROUTES_SERVE_TOKEN", None)
            tools = make_publish_route_tools(
                workspace,
                routes_config=AgentRoutesConfig(
                    enabled=True,
                    path_prefix="/routes",
                    serve_token_env="AGENT_ROUTES_SERVE_TOKEN",
                ),
                agent_name="apollo",
                ouro_client=ouro,
                allow_publish=True,
                public_base_url="https://agents.ouro.foundation/apollo",
            )
            publish = next(t for t in tools if t.name == "publish_route")
            result = publish(
                name="load-context", org_id="org-1", team_id="team-1"
            )
            self.assertIn("AGENT_ROUTES_SERVE_TOKEN is unset", result)
            ouro.services.set_authentication.assert_not_called()


class TestRunRouteTool(unittest.TestCase):
    def test_missing_required_inputs(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            session = FakeSession('{"ok": true}')
            tool = make_run_route_tool(workspace, session)
            out = tool(name="load-context", params={})
            self.assertIn("JSON Schema validation", out)
            self.assertIn("asset_id", out)
            self.assertFalse(session.calls)

    def test_type_mismatch_rejected(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            session = FakeSession('{"ok": true}')
            tool = make_run_route_tool(workspace, session)
            out = tool(name="load-context", params={"asset_id": "abc", "limit": "nope"})
            self.assertIn("JSON Schema validation", out)
            self.assertIn("limit", out)
            self.assertFalse(session.calls)

    def test_runs_handler(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            session = FakeSession('{"ok": true, "asset_id": "abc"}')
            tool = make_run_route_tool(workspace, session)
            out = tool(name="load-context", params={"asset_id": "abc"})
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertTrue(session.calls)


class TestHandlerValidation(unittest.TestCase):
    def test_requires_handler_fn(self):
        self.assertIsNotNone(_validate_handler_source("x = 1\n"))
        self.assertIsNone(
            _validate_handler_source("def handler(params, context):\n    return {}\n")
        )


class TestHttpRouter(unittest.TestCase):
    def test_mount_prefix(self):
        self.assertEqual(
            mount_prefix_from_public_base(
                "https://agents.ouro.foundation/apollo", "/routes"
            ),
            "/apollo/routes",
        )
        self.assertEqual(mount_prefix_from_public_base(None, "/routes"), "/routes")

    def _app(self, workspace: Path, *, token: str | None):
        config = MagicMock()
        config.agent.name = "apollo"
        config.agent.sandbox = MagicMock()
        config.server.public_base_url = "https://agents.ouro.foundation/apollo"
        config.agent_routes = AgentRoutesConfig(
            enabled=True,
            path_prefix="/routes",
            serve_token_env="AGENT_ROUTES_SERVE_TOKEN_TEST",
        )
        if token is None:
            os.environ.pop("AGENT_ROUTES_SERVE_TOKEN_TEST", None)
        else:
            os.environ["AGENT_ROUTES_SERVE_TOKEN_TEST"] = token

        session = FakeSession('{"served": true}')
        server = AgentRoutesServer(
            config=config,
            workspace=workspace,
            sandbox_factory=lambda: session,
        )
        router, _ = build_agent_routes_router(config, workspace, server=server)
        app = FastAPI()
        app.include_router(router, prefix=router.ouro_mount_prefix)
        return app, server

    def test_auth_and_invoke(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            manifests = load_route_manifests(workspace)
            registry = load_published_registry(workspace)
            entry = snapshot_route(workspace, manifests["load-context"], version=1)
            registry.routes["load-context"] = entry
            save_published_registry(workspace, registry)

            app, server = self._app(workspace, token="secret-token")
            try:
                client = TestClient(app)
                # no auth
                r = client.post(
                    "/apollo/routes/load-context", json={"asset_id": "x"}
                )
                self.assertEqual(r.status_code, 401)
                # wrong auth
                r = client.post(
                    "/apollo/routes/load-context",
                    json={"asset_id": "x"},
                    headers={"Authorization": "Basic nope"},
                )
                self.assertEqual(r.status_code, 401)
                # ok
                r = client.post(
                    "/apollo/routes/load-context",
                    json={"asset_id": "x"},
                    headers={"Authorization": "Basic secret-token"},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["served"], True)
                # schema violation
                r = client.post(
                    "/apollo/routes/load-context",
                    json={"limit": "nope"},
                    headers={"Authorization": "Basic secret-token"},
                )
                self.assertEqual(r.status_code, 422)
                # malformed JSON
                r = client.post(
                    "/apollo/routes/load-context",
                    content="{not-json",
                    headers={
                        "Authorization": "Basic secret-token",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(r.status_code, 400)
                # unknown
                r = client.post(
                    "/apollo/routes/missing",
                    json={},
                    headers={"Authorization": "Basic secret-token"},
                )
                self.assertEqual(r.status_code, 404)
                # openapi
                r = client.get("/apollo/routes/openapi.json")
                self.assertEqual(r.status_code, 200)
                self.assertIn("/load-context", r.json()["paths"])
            finally:
                server.close()
                os.environ.pop("AGENT_ROUTES_SERVE_TOKEN_TEST", None)

    def test_missing_token_env_503(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            _write_draft(workspace, "load-context")
            manifests = load_route_manifests(workspace)
            registry = load_published_registry(workspace)
            entry = snapshot_route(workspace, manifests["load-context"], version=1)
            registry.routes["load-context"] = entry
            save_published_registry(workspace, registry)

            app, server = self._app(workspace, token=None)
            try:
                client = TestClient(app)
                r = client.post(
                    "/apollo/routes/load-context",
                    json={"asset_id": "x"},
                    headers={"Authorization": "Basic anything"},
                )
                self.assertEqual(r.status_code, 503)
            finally:
                server.close()


class FakeRunLog:
    enabled = True

    def __init__(self, runs: list[dict], steps: dict[str, list[dict]]):
        self._runs = runs
        self._steps = steps

    def query_runs(self, **kwargs):
        status = kwargs.get("status")
        out = self._runs
        if status:
            out = [r for r in out if r.get("status") == status]
        return out

    def get_run_steps(self, run_id: str):
        return self._steps.get(run_id, [])


class TestCandidateMining(unittest.TestCase):
    def _steps(self, calls: list[list[tuple[str, dict]]]) -> list[dict]:
        steps = []
        for idx, step_calls in enumerate(calls):
            steps.append(
                {
                    "step_index": idx,
                    "tool_calls_json": json.dumps(
                        [{"name": n, "args": a} for n, a in step_calls]
                    ),
                }
            )
        return steps

    def test_detects_pattern_across_runs(self):
        seq = [
            [("search_assets", {"q": "a"})],
            [("get_asset", {"id": "1"})],
            [("get_comments", {"id": "1"})],
        ]
        runs = [
            {"run_id": "r1", "status": "success", "started_at": "2026-01-01"},
            {"run_id": "r2", "status": "success", "started_at": "2026-01-02"},
            {"run_id": "r3", "status": "success", "started_at": "2026-01-03"},
        ]
        steps = {
            "r1": self._steps(seq),
            "r2": self._steps(seq),
            "r3": self._steps(seq),
        }
        cands = mine_route_candidates(FakeRunLog(runs, steps), min_runs=3)
        keys = [c.key for c in cands]
        self.assertIn("search_assets -> get_asset -> get_comments", keys)

    def test_dedupes_within_run_and_excludes_framework(self):
        # Same 3-gram twice in one run, plus framework tools — still one run.
        calls = [
            [("load_skill", {})],
            [("search_assets", {})],
            [("get_asset", {})],
            [("get_comments", {})],
            [("memory_recall", {})],
            [("search_assets", {})],
            [("get_asset", {})],
            [("get_comments", {})],
        ]
        runs = [{"run_id": "r1", "status": "success", "started_at": "2026-01-01"}]
        cands = mine_route_candidates(
            FakeRunLog(runs, {"r1": self._steps(calls)}),
            min_runs=1,
            max_len=3,
        )
        match = [c for c in cands if c.key == "search_assets -> get_asset -> get_comments"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].run_count, 1)
        for tool in EXCLUDED_TOOLS:
            self.assertTrue(all(tool not in c.signature for c in cands))

    def test_suppresses_previously_suggested(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            seq = ["search_assets", "get_asset", "get_comments"]
            from ouro_agents.agent_routes.candidates import RouteCandidate

            cand = RouteCandidate(signature=seq, run_count=3)
            mark_candidates_suggested(workspace, [cand])
            fresh, known = filter_new_candidates(workspace, [cand])
            self.assertEqual(fresh, [])
            self.assertEqual(len(known), 1)


class TestConfigDefault(unittest.TestCase):
    def test_agent_routes_default_disabled(self):
        cfg = AgentRoutesConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.path_prefix, "/routes")


if __name__ == "__main__":
    unittest.main()
