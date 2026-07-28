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

## One-time Ouro auth provisioning

Published services use `authentication: "Ouro"`. The backend loads the
service owner's `authentications` row (`method='Ouro'`) and sends
`Authorization: Basic <vault secret>` on outbound calls.

After the first successful `publish_route`:

1. Ensure `AGENT_ROUTES_SERVE_TOKEN` is set in the agent env (same value the
   agent server validates).
2. Insert the vault secret + auth row as the agent user:

```sql
WITH s AS (
  INSERT INTO vault.secrets (secret)
  VALUES ('<same value as AGENT_ROUTES_SERVE_TOKEN>')
  RETURNING id
)
INSERT INTO public.authentications (user_id, service_id, secret_id, method)
SELECT '<AGENT_USER_ID>', '<SERVICE_ID>', id, 'Ouro' FROM s
ON CONFLICT DO NOTHING;
```

`publish_route` attempts this automatically when a Supabase session is
available; otherwise it prints the SQL above. Restart the agent server after
setting the env token so the HTTP handler picks it up.

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
