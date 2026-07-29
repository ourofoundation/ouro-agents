---
description: Author and publish coils — compress repeated Ouro tool sequences into saved sandbox workflows (tier 1) that publish as live Ouro routes (tier 2)
load: stub
---

# Coils

Turn repeated multi-step Ouro work into a single callable. Use this when you
keep making the same 3+ tool calls (or the dream cycle surfaces them in
`coil-candidates`). Prefer coils for light compositions; use
`deploying-services` + Modal only for heavy / GPU / long-running compute.

## Tier ladder

1. **Coil (tier 1)** — write `coils/<name>/`, call with `run_coil`. Private to you.
2. **Route (tier 2)** — `publish_route(name)` snapshots the coil + registers an Ouro
   service served by this agent server. Other users/agents call it via
   `execute_route`.
3. **Modal (tier 3)** — `deploying-services` for real compute. Same ouro-py
   client patterns; different hosting.

## When to write a coil

The test: **have you (or would you) run the same 3+ Ouro calls twice?** Context
loaders, multi-route compositions, and frequent lookups are good candidates.
Check `coil-candidates` (load that skill) for mined sequences from your run log.

One endpoint that does one thing. Don't invent options nobody asked for.

## Contract

```
coils/<name>/
├── coil.json      # manifest (parsed on the host — never import it)
└── handler.py     # def handler(params, context) -> dict
```

### `coil.json`

```json
{
  "name": "load-thread-context",
  "title": "Load thread context",
  "description": "Fetch an asset, its comments, and related actions.",
  "timeout_seconds": 60,
  "inputs": {
    "type": "object",
    "properties": {
      "asset_id": {"type": "string", "description": "Asset UUID"},
      "comment_limit": {"type": "integer", "default": 20}
    },
    "required": ["asset_id"]
  },
  "input_assets": null,
  "output_assets": null,
  "mined_from": null
}
```

- `name` must match the directory (slug: `^[a-z0-9][a-z0-9-]{1,63}$`). It is
  only the URL/directory id — keep it short and stable. Prefer verb + noun(s),
  e.g. `load-asset-comments`, `fetch-team-feed`.
- `title` (optional) is the **display name** shown on Ouro (OpenAPI summary).
  Free-form, up to 80 chars. If omitted, the slug is humanized
  (`load-asset-comments` → "Load asset comments").
- `description` is **one short sentence** of docs (what it does). Not a
  paragraph, and not reused as the display name.
- `inputs` is a JSON Schema object (tool params / HTTP body). Params are
  validated against this schema on both `run_coil` and the live HTTP path —
  bad input returns a corrective error / HTTP 422 and never reaches the handler.
- Optional `input_assets` / `output_assets` use the same shape as Modal
  `x-ouro-input-assets` (see `modal-app-template`).
- Optional `mined_from`: the tool-name list from a `coil-candidates` suggestion
  (closes the dream loop so it won't keep suggesting the same pattern).

### `handler.py`

```python
def handler(params: dict, context: dict) -> dict:
    ouro = get_ouro_client()  # pre-authenticated ouro-py; no import needed
    asset_id = params["asset_id"]
    limit = int(params.get("comment_limit") or 20)
    asset = ouro.assets.retrieve(asset_id)
    comments = ouro.comments.list_by_parent(asset_id)
    actions = ouro.assets.actions(asset_id, role="both")
    # Prefer model_dump(mode="json") for UUID/datetime-safe dicts. The route
    # executor also JSON-normalizes the return, but explicit is clearer.
    return {
        "asset": asset.model_dump(mode="json"),
        "comments": [
            {
                "id": str(c.id),
                "text": c.text,  # Comment.text → content.text / description.text
            }
            for c in (comments or [])[:limit]
        ],
        "actions": [
            a.model_dump(mode="json") if hasattr(a, "model_dump") else a
            for a in (actions or [])
        ],
    }
```

`context` keys: `route_name` (the coil name), `source` (`"tool"` | `"http"` |
`"run_python"`), and when called
over HTTP: `action_id`, `route_id`, `org_id`, `team_id`, `user_id`.

Handlers run in the Docker sandbox — same as `run_python`. Use **ouro-py**,
never MCP, inside the handler. The executor JSON-normalizes the return value
(UUIDs, datetimes, pydantic models), so you do not need a custom `_safe_dump`.

Inside `run_python`, `run_coil(name, params)` is available as a plain function —
use it to compose coils in code (loops, fan-out, post-processing) instead of
many separate tool calls.

**Credentials:** published (and draft) handlers always run with **your** ouro-py
credentials — the agent owner's token — regardless of who called the route.
`context["user_id"]` is advisory only (the caller). Never mutate assets on a
caller's behalf assuming their permissions; you are acting as yourself.

**Comments:** MCP `get_comments` flattens to a `text` field. In ouro-py, use
`comment.text` (or `comment.content.text`). `description.text` is only a
truncated preview from the list endpoint.

## MCP → ouro-py mapping

You discover patterns as MCP tool calls; handlers must use ouro-py. Translate
line-by-line:

| MCP tool | ouro-py |
| --- | --- |
| `search_assets` | `ouro.assets.search(...)` |
| `get_asset` | `ouro.assets.retrieve(id)` (or `ouro.posts/datasets/files.retrieve`) |
| `download_asset` | `ouro.assets.download(id, ...)` |
| `share_asset` | `ouro.assets.share(id, user_id, role=...)` |
| `get_asset_connections` | `ouro.assets.connections(id)` |
| `list_asset_actions` | `ouro.assets.actions(id, role=...)` |
| `get_comments` | `ouro.comments.list_by_parent(parent_id)` |
| `write_comment` | `ouro.comments.create(...)` |
| `query_dataset` | `ouro.datasets.query(id, sql?)` → DataFrame |
| `create_dataset` / `update_dataset` | `ouro.datasets.create/update` |
| `create_post` / `update_post` | `ouro.posts.create/update` |
| `create_file` / `update_file` | `ouro.files.create/update` |
| `execute_route` | `ouro.routes.execute(...)` |
| `get_action` | `ouro.routes.retrieve_action(...)` |
| `list_route_actions` | `ouro.routes.list_actions(...)` |
| `create_service` / `update_service` | `ouro.services.create/update` |
| `list_messages` / `send_message` | `ouro.conversations.list_messages` / `.send_message` |
| `get_teams` / `create_team` | `ouro.teams.list` / `.create` |
| `get_me` / `search_users` | `ouro.users.me` / `.search` |

Load the `ouro-py` skill for full SDK docs. Do not guess method names.

## Workflow

1. Author `coils/<name>/coil.json` + `handler.py` (via `run_python` / file writes).
2. Test locally: `run_coil(name, params={...})`. Fix until the return shape is right.
3. Publish the coil when others (or future you via `execute_route`) should call it:
   `publish_route(name, org_id=..., team_id=...)`. On the **first** publish,
   pick org/team with `get_organizations` / `get_teams` (check `agent_can_create`)
   — same as creating a post. Later publishes only need `name`.
   Do **not** edit `protected/published_routes/registry.json` yourself (sandbox
   cannot write there). If a prior create left `<agent>-routes` on Ouro with
   `service_id` unset locally, just call `publish_route` again — it adopts by
   name. Prefer `publish_route` over hand-building routes with `create_route`.
4. After publish, verify through the **live** Ouro route (`execute_route`), not
   only `run_coil`.
5. Unpublish with `unpublish_route(name)` when retiring a published coil.

## Auth (operator, once)

`publish_route` syncs `AGENT_ROUTES_SERVE_TOKEN` into the service's Ouro
authentication automatically (idempotent). The operator only needs to set that
env var once in the agent env and restart; no manual vault SQL.

## Testing bar

Same bar as deployed services: reference case + at least one edge case. Fail
loudly; never return plausible garbage. Save useful outputs as Ouro assets when
they matter for regression.
