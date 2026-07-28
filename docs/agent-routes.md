# Agent-authored routes

Agents can author small Python handlers under `workspace/routes/<name>/` and
call them with `run_route` (tier 1), or publish them as a real Ouro service
served by the agent FastAPI process (tier 2). Modal remains for heavy compute
(tier 3). Handlers always use **ouro-py** via `get_ouro_client()` — never MCP.

## Enabling

In the agent JSON config:

```json
"server": {
  "host": "0.0.0.0",
  "port": 8020,
  "webhook_path": "/apollo/events",
  "public_base_url": "https://agents.ouro.foundation/apollo"
},
"agent_routes": {
  "enabled": true,
  "path_prefix": "/routes"
}
```

Enabled agents today:
- Apollo: `https://agents.ouro.foundation/apollo` → mounts `/apollo/routes`
- Hermes: `https://hermes.ouro.foundation/ouro-agents` → mounts `/ouro-agents/routes`

`server.public_base_url` is the agent's public HTTPS origin (nginx front door).
Agent routes use it for OpenAPI `servers` and service `base_url`; other features
can reuse it too. Org/team are **not** configured here — on first
`publish_route`, the agent passes `org_id` and `team_id`.

In the agent env file (e.g. `.env.apollo` / `.env.hermes`):

```bash
AGENT_ROUTES_SERVE_TOKEN=$(openssl rand -hex 32)
```

The HTTP router mounts at `{urlparse(server.public_base_url).path}/routes` on the
agent server (no nginx changes). The backend must reach that public URL when
proxying `execute_route`.

## Ouro auth provisioning

Published services use `authentication: "Ouro"`. The backend loads the
service owner's `authentications` row (`method='Ouro'`) and sends
`Authorization: Basic <vault secret>` on outbound calls.

1. Set `AGENT_ROUTES_SERVE_TOKEN` in the agent env once and restart the agent
   so the HTTP handler can validate inbound Basic auth.
2. Call `publish_route` — it syncs that token into the service via
   `PUT /services/:id/authentication` (owner-scoped, idempotent). Re-publishing
   rotates the vault secret only when the env token changed.

No manual vault SQL is required. The agent never uses a service-role key;
provisioning runs as the agent user through the normal API.

## Files

| Path | Role |
| --- | --- |
| `routes/<name>/route.json` | Draft manifest (agent-writable) |
| `routes/<name>/handler.py` | Draft handler (agent-writable) |
| `protected/published_routes/registry.json` | Published registry (host-writable) |
| `protected/published_routes/<name>/vN/` | Immutable snapshots |
| `protected/published_routes/candidates.json` | Dream miner state |
| `skills/route-candidates.md` | Dream-written suggestions |

## Dream mining

When `agent_routes` and `run_log` are enabled, the dream cycle mines repeated
tool-call n-grams and writes `skills/route-candidates.md`. See the
`agent-routes` skill for authoring.
