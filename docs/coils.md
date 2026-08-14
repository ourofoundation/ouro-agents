# Coils (agent-authored routes)

Coils are small Python handlers agents author under `workspace/coils/<name>/`
and call with `run_coil` (tier 1). Publishing a coil (`publish_route`) turns it
into a real Ouro service route served by the agent FastAPI process (tier 2).
Modal remains for heavy compute (tier 3). Handlers always use **ouro-py** via
`get_ouro_client()` — never MCP. Legacy workspaces with `routes/<name>/route.json`
are still read; new coils use `coils/<name>/coil.json`.

Agents see a top-level COILS index in the system prompt (drafts + published
status), and can call `run_coil(name, params)` inside `run_python` to compose
coils in code.

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
| `coils/<name>/coil.json` | Draft coil manifest (agent-writable) |
| `coils/<name>/handler.py` | Draft coil handler (agent-writable) |
| `protected/published_routes/registry.json` | Published registry (host-writable) |
| `protected/published_routes/<name>/vN/` | Immutable snapshots |
| `skills/coil-candidates.md` | Agent-maintained list of jobs worth coiling |

Agents jot candidates themselves in `skills/coil-candidates.md` when they
notice a repeatable *job* (not a repeated tool name). See the `coils` skill
for the quality bar and templates. Nothing auto-writes that file.
