# Configuration reference

`config.json` is the single source of truth for an agent. It is loaded by
`OuroAgentsConfig.load_from_file(path)` (`ouro_agents/config.py`), which
also expands `${ENV_VAR}` references and migrates a few legacy field
shapes.

This page documents every field. See
[`config.example.json`](../config.example.json) for a working starting
point.

## Top-level shape

```json
{
  "agent":         { ... },   // includes optional ``reasoning``
  "prompt_caching":{ ... },
  "heartbeat":     { ... },     // populated from modes.heartbeat
  "planning":      { ... },     // populated from modes.planning
  "modes":         { ... },
  "subagents":     { ... },
  "memory":        { ... },
  "mcp_servers":   [ ... ],
  "server":        { ... },
  "event_pooling": { ... },
  "controller":    { ... },
  "display":       { ... },
  "refinement":    { ... },
  "env_file":      "..."
}
```

You typically write `heartbeat` and `planning` *inside* the `modes` block
(see [§ modes](#modes)); the loader hoists them into top-level
`HeartbeatConfig` / `PlanningConfig` objects automatically.

## `agent`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | str | required | Display name, also used in doc-store keys. |
| `model` | str | required | Default OpenRouter model id. |
| `workspace` | path | `./workspace` | Workspace directory. |
| `org_id` | str | none | Ouro organization the agent operates in. Required when using teams. |
| `python_packages` | list[str] | `[]` | Packages exposed inside the `run_python` sandbox. Use `package.*` to allow submodules such as `pymatgen.symmetry.analyzer`; entries are validated at startup. |
| `reasoning` | ReasoningConfig | none | Default OpenRouter reasoning for the main agent model (see below). |

`agent.team_id` is **not** supported anymore — teams are discovered at
runtime. The loader raises if it sees one.

A legacy top-level `reasoning` block is migrated into `agent.reasoning` on
load.

### `agent.reasoning`

Maps to OpenRouter's top-level `reasoning` request field on chat completions.

| Field | Type | Notes |
|-------|------|-------|
| `effort` | `xhigh`/`high`/`medium`/`low`/`minimal`/`none` | Used by reasoning-capable models. |
| `max_tokens` | int | Cap reasoning tokens; mutually exclusive with `effort` per provider rules. |
| `exclude` | bool | Don't return reasoning to the client. |
| `enabled` | bool | Some providers gate reasoning behind this flag. |

Layered overrides:

- `heartbeat.reasoning` — overlays on top of `agent.reasoning` whenever the
  heartbeat model is built.
- `subagents.<name>.reasoning` — per-profile override.

`merge_reasoning(*layers)` merges them last-wins per non-None field.

## `prompt_caching`

Anthropic prompt caching via OpenRouter.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Set `cache_control` on requests when the model id starts with `anthropic/`. |
| `ttl` | `5m` \| `1h` | `5m` | Cache lifetime. |

## `heartbeat`

Top-level scheduler/model fields for the heartbeat tick. Usually written
inside `modes.heartbeat` and hoisted out at load time.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `true` | Disable to stop scheduling heartbeats. |
| `every` | str | `30m` | Interval (`30s`, `5m`, `1h`, `1d`) or 5-field cron. |
| `model` | str | required | Heartbeat model id (also used by the cheap "compactor" model). |
| `active_hours` | `{start, end, timezone}` | none | Skip ticks outside this window. |
| `proactive.enabled` | bool | `false` | Allow heartbeats to use additional MCP servers. |
| `proactive.servers` | list[str] | `["ouro"]` | Extra servers when proactive. |
| `reasoning` | ReasoningConfig | none | Overlay on top of `agent.reasoning`. |

## `planning`

Hoisted from `modes.planning`.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Master switch for the planning cycle. |
| `model` | str | none | Planner model; falls back to `agent.model`. |
| `cadence` | str | `1d` | How often a new plan cycle is generated. |
| `min_heartbeats` | int | 4 | Minimum heartbeats between plan cycles. |
| `review_window` | str | `2h` | How long a plan stays in `pending_review` before auto-approval. |
| `auto_approve` | bool | `true` | Skip controller review entirely. |

The plan-loop max steps live in `modes.planning.max_steps` (which becomes
the `plan` mode profile's step budget).

## `modes`

User-facing per-mode overrides keyed by mode name (or alias):

```json
"modes": {
  "run":      { "max_steps": 60 },
  "chat":     { "max_steps": 40 },
  "planning": { "enabled": true, "cadence": "4h", "max_steps": 6 },
  "heartbeat": {
    "every": "1h",
    "model": "openai/gpt-4.1-mini",
    "active_hours": { "start": "09:00", "end": "17:00", "timezone": "America/Chicago" },
    "max_steps": 20
  }
}
```

Keys accepted (with aliases):

- `run` → `autonomous`
- `planning` → `plan`
- `reply` → `chat-reply`
- `chat`, `chat-reply`, `heartbeat` are accepted as-is.

Each block accepts a `ModeOverride`:

| Field | Type | Notes |
|-------|------|-------|
| `max_steps` | int | Override the profile's step budget. |
| `preload_tools` | list[str] | Replace the profile's preloads. |

For `heartbeat` and `planning` blocks, the loader extracts the
scheduler/model fields described above into top-level config sections, so
both forms work side by side.

## `subagents`

```json
"subagents": {
  "default_model": "google/gemini-2.5-flash",
  "parallel_dispatch": true,
  "custom_profiles_dir": "subagents",
  "writer":   { "model": "anthropic/claude-sonnet-4" },
  "research": { "max_steps": 30, "reasoning": { "effort": "low" } }
}
```

Top-level fields:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `true` | Reserved; the `delegate` tool is also gated by `ModeProfile.allow_delegation`. |
| `default_model` | str | none | Fallback model for any subagent profile without an explicit override. |
| `parallel_dispatch` | bool | `true` | Whether `delegate` can fan out tasks in parallel. |
| `custom_profiles_dir` | str | none | Directory of custom profiles. Resolved relative to the workspace if not absolute. Defaults to `workspace/subagents/` when present. |

Each named child block is a `SubAgentOverride`:

| Field | Notes |
|-------|-------|
| `model` | OpenRouter model id. |
| `max_steps` | Override profile budget. |
| `reasoning` | Reasoning overlay specific to this subagent. |

See [Subagents](./subagents.md) for profile authoring.

## `memory`

Vector memory backend + dream (consolidation) policy.

| Field | Default | Notes |
|-------|---------|-------|
| `provider` | `mem0` | Only `mem0` is shipped. |
| `path` | `./workspace/memory` | Local store directory. |
| `extraction_model` | required | Cheap model used by mem0 to extract facts. |
| `embedder` | required | Embedding model id (e.g. `openai/text-embedding-3-small`). |
| `search_limit` | 10 | Default top-K for `memory_recall`. |
| `retrieval_queries` | 3 | Multi-query expansion in preflight. |
| `max_retrieval_tokens` | 4000 | Soft cap on injected memory bytes. |
| `rhythm` | `daily` | Log/dream cadence: `daily`, `weekly`, or `biweekly`. Sets the window a single log doc covers **and** how often the dream cycle runs. |
| `dream_enabled` | `true` | Run the dream (memory consolidation) cycle. |
| `dream_time` | `03:00` | Time of day (`HH:MM`, UTC) for the dream tick. The tick only does work when a new `rhythm` period has begun since the last run. |
| `memory_md_max_tokens` | 4000 | Cap on `MEMORY.md` size after consolidation. |
| `mid_session_reflection_interval` | 10 | Chat turns between mid-session reflections. |
| `decay_after_days` | 30 | Global decay fallback. |
| `decay_rules` | (see below) | Per-category decay policy. |
| `graph.enabled` | `false` | Optional mem0 graph backend. |
| `graph.provider` | none | Provider name when graph is enabled. |
| `graph.config` | none | Provider-specific config dict. |

Default `decay_rules`:

```json
{
  "direction":   { "after_days": null, "factor": 1.0 },
  "decision":    { "after_days": null, "factor": 1.0 },
  "fact":        { "after_days": 180,  "factor": 0.7 },
  "preference":  { "after_days": 365,  "factor": 0.8 },
  "learning":    { "after_days": 180,  "factor": 0.8 },
  "observation": { "after_days": 30,   "factor": 0.5 }
}
```

## `mcp_servers`

A list of `MCPServerConfig`. Each entry connects on startup.

| Field | Notes |
|-------|-------|
| `name` | Logical name, used as the server prefix in qualified tool names (e.g. `ouro:create_post`). |
| `transport` | `stdio` (only one currently implemented) or `streamable-http` (raises NotImplementedError). |
| `command` | Executable path for `stdio`. |
| `args` | Argv list. |
| `env` | Env dict; supports `${VAR}` expansion. |
| `url` | Reserved for `streamable-http`. |

`${WORKSPACE_ROOT}` is auto-injected for child processes. For the `ouro`
server, `OURO_MCP_TIMEZONE` is also injected based on
`heartbeat.active_hours.timezone` so platform timestamps render in the
agent's local time.

## `server`

| Field | Default | Notes |
|-------|---------|-------|
| `host` | `0.0.0.0` | uvicorn bind. |
| `port` | `8000` | uvicorn port. |
| `webhook_path` | `/events` | URL path Ouro should POST events to. |

## `event_pooling`

```json
"event_pooling": {
  "enabled": true,
  "events": {
    "new-message": { "settle_seconds": 2.0,  "jitter_seconds": 3.0,  "max_wait_seconds": 8.0  },
    "comment":     { "settle_seconds": 20.0, "jitter_seconds": 20.0, "max_wait_seconds": 90.0 },
    "mention":     { "settle_seconds": 20.0, "jitter_seconds": 20.0, "max_wait_seconds": 90.0 }
  }
}
```

Per-event timing config (`EventPoolTimingConfig`):

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `true` | Pool this event type at all. |
| `settle_seconds` | 10 | Wait at least this long after the latest event before dispatching. |
| `jitter_seconds` | 0 | Random extra wait, applied per batch. |
| `max_wait_seconds` | 45 | Hard ceiling per batch from the first event. |

Defaults are merged with the user's overrides per-event so missing fields
keep sensible values.

## `controller`

| Field | Notes |
|-------|-------|
| `username` | Ouro username to mention as `{@username}` when a plan enters review. Tells the controller a quest is ready. |

## `display`

| Field | Default | Notes |
|-------|---------|-------|
| `usage_table.show_reasoning` | `false` | Include reasoning tokens in the per-run usage table. |

## `refinement`

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `true` | Run the refinement scheduler job. |
| `schedule` | `0 */6 * * *` | Cron for refinement passes. |
| `min_batch_size` | 5 | Minimum queued changes before a pass runs. |
| `max_changes_per_pass` | 25 | Hard cap on changes processed in one pass. |
| `max_docs_per_pass` | 15 | Hard cap on docs rewritten per pass. |
| `window_lines` | 20 | Context lines around each match in the LLM payload. |
| `model` | none | Cheap model used for refinement; falls back to the heartbeat model. |

See [Refinement](./refinement.md).

## `env_file`

Optional path to a `.env` file (relative paths resolve against the config
directory). When set, the loader passes it to `python-dotenv`. The CLI
`--env-file` flag and the `ENV_FILE` env var both override this.

## Environment variable expansion

Any string value can reference an env var with `${VAR}`. The loader
replaces it after `.env` is loaded, so you can keep secrets out of
`config.json`:

```json
"env": {
  "OURO_API_KEY":  "${OURO_API_KEY}",
  "OURO_BASE_URL": "${OURO_BASE_URL}"
}
```

## Migrations the loader applies

The loader silently normalizes a few legacy shapes:

- `agent.preload_tools` and `agent.max_steps` → `modes.<name>` entries.
- `subagents.overrides.*` and bare keys at the top of `subagents` →
  `subagents.profiles`.
- `modes.overrides.*` → `modes.profiles`, with alias normalization.
- `memory.org_id` and `planning.org_id` → `agent.org_id`.
- `agent.team_id` → raises a clear error (teams are discovered at runtime).

You can keep using the old shapes; the eventual goal is to migrate
example configs over time.
