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
  "agent":         { ... },   // model/reasoning optional when ``models`` is set
  "models":        { ... },   // preferred: strong / light / optional mid
  "prompt_caching":{ ... },
  "observations":  { ... },
  "heartbeat":     { ... },     // populated from modes.heartbeat
  "planning":      { ... },     // populated from modes.planning
  "modes":         { ... },
  "subagents":     { ... },
  "memory":        { ... },
  "mcp_servers":   [ ... ],
  "server":        { ... },
  "event_pooling": { ... },
  "event_delivery":{ ... },
  "security":      { ... },
  "ask_controller":{ ... },
  "display":       { ... },
  "refinement":    { ... },
  "run_log":       { ... },
  "env_file":      "..."
}
```

You typically write `heartbeat` and `planning` *inside* the `modes` block
(see [§ modes](#modes)); the loader hoists them into top-level
`HeartbeatConfig` / `PlanningConfig` objects automatically.

## `models`

Configure two or three model bundles once; the harness picks a tier per role.

```json
"models": {
  "strong": {
    "id": "z-ai/glm-5.2",
    "reasoning": { "effort": "medium" }
  },
  "light": {
    "id": "xiaomi/mimo-v2.5",
    "reasoning": { "effort": "none" }
  },
  "mid": {
    "id": "moonshotai/kimi-k3"
  }
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `strong` | ModelTierSpec | required | Planning, writer, executor, developer; also `agent.model` fallback. |
| `light` | ModelTierSpec | required | Preflight, research, reflector, extraction, utilities (compact / summarize / dream / refinement). |
| `mid` | ModelTierSpec | none | Chat, autonomous, and heartbeat when set; otherwise those use `strong`. |

Each tier spec:

| Field | Type | Notes |
|-------|------|-------|
| `id` | str | OpenRouter model id. |
| `reasoning` | ReasoningConfig | Default reasoning for that tier. |
| `openrouter_provider` | object | Optional per-tier provider routing overlay. |

When `models` is set, the loader fills `agent.model`, `agent.reasoning`,
`modes.heartbeat.model`, `modes.planning.model`, and
`memory.extraction_model` if those fields are omitted. Explicit values
always win. Per-subagent `model` / `reasoning` overrides still win over
the role→tier map.

Role → tier map (code-owned, not configurable):

| Role | Tier |
|------|------|
| agent, planning, writer, executor, developer, planner | strong |
| chat, autonomous, heartbeat | mid → strong |
| search, research, reflector | light |
| utility, extraction, refinement | light |

Legacy configs without `models` keep working: set `agent.model`,
`modes.heartbeat.model`, `memory.extraction_model`, and per-subagent
models as before.

## `agent`

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | str | required | Display name, also used in doc-store keys. |
| `model` | str | required\* | Default OpenRouter model id. \*Filled from `models.strong` when omitted. |
| `workspace` | path | `./workspace` | Workspace directory. |
| `org_id` | str | none | Ouro organization the agent operates in. Required when using teams. |
| `sandbox` | SandboxConfig | see below | Selects the `run_python` execution backend. |
| `reasoning` | ReasoningConfig | none | Default OpenRouter reasoning for the main agent model (see below). Filled from `models.strong.reasoning` when omitted. |

`agent.team_id` is **not** supported anymore — teams are discovered at
runtime. The loader raises if it sees one.

A legacy top-level `reasoning` block is migrated into `agent.reasoning` on
load.

### `agent.sandbox`

Controls how code execution tools run.

```json
"sandbox": {
  "mode": "local",
  "python_packages": [],
  "image": "ouro-agents-sandbox:latest",
  "workspace_mount": "/workspace",
  "network": "bridge",
  "memory": "1g",
  "cpus": 1.0,
  "pids_limit": 256,
  "timeout_seconds": 300,
  "max_output_chars": 50000,
  "enable_shell": false,
  "env_allowlist": ["OURO_API_KEY", "OURO_BASE_URL"]
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `mode` | `local` \| `docker` | `local` | `local` preserves the existing restricted smolagents executor. `docker` runs code in an isolated container. |
| `python_packages` | list[str] | `[]` | Packages advertised to `run_python`. In local mode they are added to the smolagents import allowlist; in Docker mode they are validated inside `image`. Use `package.*` to describe submodule packages such as `pymatgen.symmetry.analyzer`. |
| `image` | str | `ouro-agents-sandbox:latest` | Docker image used when `mode` is `docker`. |
| `workspace_mount` | str | `/workspace` | Container path where the agent workspace is bind-mounted. |
| `network` | str | `bridge` | Docker network mode. Use `none` for offline sandboxes. |
| `memory` | str \| null | `1g` | Docker memory limit. |
| `cpus` | float \| null | `1.0` | Docker CPU quota. |
| `pids_limit` | int \| null | `256` | Docker process limit. |
| `timeout_seconds` | int | `300` | Per-call timeout for Docker sandbox tools. On timeout the worker is reset and the next `run_python`/`run_shell` call starts a fresh session (workspace files persist; in-memory state does not). Timed-out shell commands that finish inside the worker return a timeout result without resetting. |
| `max_output_chars` | int | `50000` | Captured stdout/stderr/result truncation limit. |
| `enable_shell` | bool | `false` | When `mode` is `docker`, expose a `run_shell(command)` tool that executes non-interactive shell commands inside the same sandbox container. Ignored in local mode. |
| `env_allowlist` | list[str] | `["OURO_API_KEY", "OURO_BASE_URL"]` | Host environment variables passed through to the container. |
| `user` | str \| null | current uid/gid | Docker `--user` override. |
| `no_new_privileges` | bool | `true` | Adds Docker's `no-new-privileges` security option. |
| `drop_capabilities` | bool | `true` | Drops Linux capabilities in the sandbox container. |

Docker mode is isolated at the container boundary. Code can use normal Python
file and process APIs (`pathlib`, `open`, `zipfile`, `subprocess`, installed
packages) as long as reads and writes stay under `WORKSPACE_ROOT` /
`workspace_mount`.

When `enable_shell` is true, `run_shell` uses the same container, bind mount,
environment allowlist, timeout, and output limit. Keep commands short and
non-interactive; use a custom Docker image when the command needs extra CLI
dependencies.

#### Build the Docker sandbox images

`Dockerfile.sandbox` is the shared base image with the common science stack.
Agents that need extra tooling get their own thin overlay Dockerfile built
`FROM` the base (e.g. `Dockerfile.sandbox.apollo` adds git for service
building; `Dockerfile.sandbox.hermes` adds the Resend SDK for outreach
routes) and point `agent.sandbox.image` at the overlay tag.

Build from the `ouro-agents` directory, base first:

```bash
docker build -f Dockerfile.sandbox -t ouro-agents-sandbox:latest .
docker build -f Dockerfile.sandbox.apollo -t ouro-agents-sandbox-apollo:latest .
docker build -f Dockerfile.sandbox.hermes -t ouro-agents-sandbox-hermes:latest .
```

If you add packages to `agent.sandbox.python_packages`, make sure the agent's
configured image installs them: add them to the agent's overlay Dockerfile (or
the base, if every agent should have them) and rebuild.

### `agent.reasoning`

Maps to OpenRouter's top-level `reasoning` request field on chat completions.

| Field | Type | Notes |
|-------|------|-------|
| `effort` | `xhigh`/`high`/`medium`/`low`/`minimal`/`none`/`max` | Used by reasoning-capable models. |
| `max_tokens` | int | Cap reasoning tokens; mutually exclusive with `effort` per provider rules. |
| `exclude` | bool | Don't return reasoning to the client. |
| `enabled` | bool | Some providers gate reasoning behind this flag. |
| `context` | `auto`/`all_turns`/`current_turn` | GPT-5.6+ only. Which echoed reasoning turns the model may use. When unset on GPT-5.6+, ouro-agents defaults to `all_turns` (tool-loop continuity). |
| `mode` | `standard`/`pro` | GPT-5.6+ only. `pro` routes to the matching `*-pro` listing (deeper multi-pass reasoning). |

Layered overrides:

- `heartbeat.reasoning` — overlays on top of `agent.reasoning` whenever the
  heartbeat model is built.
- `subagents.<name>.reasoning` — per-profile override.

`merge_reasoning(*layers)` merges them last-wins per non-None field.

## `prompt_caching`

Prompt caching via OpenRouter for Anthropic (top-level `cache_control`) and
commercial Qwen (per-message breakpoints). Kimi/GLM use automatic provider
prefix caching without explicit markers — keep the system prompt static and
the task dynamic so prefixes stay stable.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Enables explicit Anthropic/Qwen cache markers. |
| `ttl` | `5m` \| `1h` | `5m` | Cache lifetime for explicit markers. |

Optional per-tier `max_completion_tokens` sets OpenAI `max_tokens` when you
need an explicit ceiling (for example to avoid a provider reserving a huge
default). There are no role-level defaults — leave the field unset to let the
model finish normally.

## `observations`

Spill-and-cap policy for tool results in every mode (chat, heartbeat,
autonomous, plan, subagents). Oversized outputs are written under
`scratch/tool-outputs/<run_id>/` and replaced with a head/tail stub **before**
they enter agent memory. History stays append-only for prompt-cache stability;
older steps are rewritten only on a rare one-shot compact when cumulative
observation size crosses `run_compact_ceiling`.

```json
"observations": {
  "max_inline_chars": 20000,
  "head_chars": 2500,
  "tail_chars": 1500,
  "max_step_chars": 40000,
  "run_compact_ceiling": 160000,
  "keep_recent_steps": 3,
  "excerpt_chars": 1200,
  "exempt_tools": ["load_skill"]
}
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `max_inline_chars` | int | `20000` | Spill + head/tail stub when a single tool result exceeds this. Stubs (and the system prompt) tell the agent this budget so follow-up `head`/`tail`/`sed`/`rg` reads stay inline. |
| `head_chars` | int | `2500` | Leading chars kept in the stub. |
| `tail_chars` | int | `1500` | Trailing chars kept in the stub (catches deploy success / exit codes). |
| `max_step_chars` | int | `40000` | Cap after parallel tools concatenate into one step observation. |
| `run_compact_ceiling` | int | `160000` | One-shot fold of older steps only above this cumulative size (one cache break). |
| `keep_recent_steps` | int | `3` | Recent action steps left untouched on one-shot compact. |
| `excerpt_chars` | int | `1200` | Remnant per compacted older step (spill paths are preserved). |
| `exempt_tools` | list[str] | `["load_skill"]` | Tool names whose results never spill — use for intentional context payloads (skill bodies). Pass `[]` to disable. |

Sandbox `max_output_chars` stays high (default `50000`) so spill files still
receive useful full tool output; the agent-layer stub is what protects the
prompt.

## `heartbeat`

Top-level scheduler/model fields for the heartbeat tick. Usually written
inside `modes.heartbeat` and hoisted out at load time.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `true` | Disable to stop scheduling heartbeats. |
| `every` | str | `30m` | Interval (`30s`, `5m`, `1h`, `1d`) or 5-field cron. |
| `model` | str | required | Heartbeat executor model id. |
| `active_hours` | `{start, end, timezone}` | none | Skip ticks outside this window. |
| `servers` | list[str] | `["ouro"]` | MCP servers the main heartbeat may load. Search belongs to `search`/`research` subagents. |
| `reasoning` | ReasoningConfig | none | Overlay on top of `agent.reasoning`. |

Legacy `proactive.enabled` / `proactive.servers` configs are migrated to
`servers` at load time (always `["ouro"]` for the main heartbeat when search
was previously listed — search is delegated).

Heartbeat flow: one mid-model run decides and executes (strong when mid is
unset). Delegated workers are capped to mid/light for that tick.

## `planning`

Hoisted from `modes.planning`.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `false` | Master switch for the planning cycle. |
| `model` | str | none | Planner model; falls back to `models` planning tier, then the heartbeat model. |
| `cadence` | str | `1d` | How often a new plan quest may be published (per team; fires only once the work inbox drains). |
| `review_window` | str | `2h` | How long a plan quest stays in `draft` before auto-approval. |
| `auto_approve` | bool | `true` | Auto-open drafts after the review window with no feedback. |

The retired `min_heartbeats` knob is still accepted from legacy configs and
ignored — inbox depth now paces replanning naturally.

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
- `reply`, `chat-reply` → `chat` (the two chat modes were merged)
- `chat`, `heartbeat` are accepted as-is.

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
| `path` | `./workspace/protected/memory` | Local store directory. |
| `extraction_model` | required\* | Cheap model used by mem0 to extract facts. \*Filled from `models.light` when omitted. |
| `embedder` | required | Embedding model id (e.g. `openai/text-embedding-3-small`). |
| `search_limit` | 10 | Default top-K per `memory_recall` query (per-query `limit` overrides). |
| `max_retrieval_tokens` | 4000 | Global soft cap (tokens) on a single `memory_recall`'s combined output. |
| `min_signal_score` | 0.35 | Relevance floor for recall results; low-signal hits are dropped unless explicit filters were passed. |
| `rhythm` | `daily` | Log/dream cadence: `daily`, `weekly`, or `biweekly`. Sets the window a single log doc covers **and** how often the dream cycle runs. |
| `dream_enabled` | `true` | Run the dream (memory consolidation) cycle. |
| `dream_time` | `03:00` | Time of day (`HH:MM`, UTC) for the dream tick. The tick only does work when a new `rhythm` period has begun since the last run. |
| `memory_md_max_tokens` | 4000 | Cap on `MEMORY.md` size after consolidation. |
| `decay_after_days` | 30 | Days since last access before strength decay and stale evolving-memory review. |
| `graph.enabled` | `false` | Optional mem0 graph backend. |
| `graph.provider` | none | Provider name when graph is enabled. |
| `graph.config` | none | Provider-specific config dict. |

## `mcp_servers`

A list of `MCPServerConfig`. Each entry connects on startup.

| Field | Notes |
|-------|-------|
| `name` | Logical name, used as the server prefix in qualified tool names (e.g. `ouro:create_post`). |
| `transport` | `stdio` or `streamable-http`. |
| `command` | Required for `stdio`. Optional for `streamable-http` — when set with `url`, the agent spawns and manages the HTTP server process. |
| `args` | Argv list. |
| `env` | Env dict; supports `${VAR}` expansion. |
| `url` | Required for `streamable-http` (e.g. `http://127.0.0.1:8011/mcp`). |

Stdio MCP tool calls are serialized per server so overlapping runs cannot
interleave JSON-RPC on one pipe. Streamable-HTTP servers skip that lock —
concurrent tool calls are safe on the wire.

`${WORKSPACE_ROOT}` is auto-injected for child processes (host workspace path).
In Docker sandbox mode, `${WORKSPACE_MOUNT}` is also injected (e.g.
`/workspace`) so MCP remaps container absolute paths onto the host root. For
the `ouro` server, `OURO_MCP_TIMEZONE` is also injected based on
`heartbeat.active_hours.timezone` so platform timestamps render in the
agent's local time. `OURO_MCP_MAX_RESPONSE_SIZE` defaults to `0` (no
server-side truncation) so observation budgeting stays in the agent; set a
positive character count in `mcp_servers[].env` to opt into MCP's soft
`len(response)` cap.

## `server`

| Field | Default | Notes |
|-------|---------|-------|
| `host` | `0.0.0.0` | uvicorn bind. |
| `port` | `8000` | uvicorn port. |
| `webhook_path` | `/events` | URL path Ouro should POST events to. |
| `public_base_url` | `null` | Public HTTPS origin for this agent (nginx front door), e.g. `https://agents.ouro.foundation/apollo`. Used by agent routes and available to other features that need the externally reachable URL. |

## `event_pooling`

```json
"event_pooling": {
  "enabled": true,
  "events": {
    "new-message": { "enabled": false },
    "comment":     { "settle_seconds": 20.0, "jitter_seconds": 20.0, "max_wait_seconds": 90.0 },
    "mention":     { "settle_seconds": 20.0, "jitter_seconds": 20.0, "max_wait_seconds": 90.0 }
  }
}
```

Chat (`new-message`) pooling is off by default so replies start immediately.
Comment/mention bursts still debounce.

Per-event timing config (`EventPoolTimingConfig`):

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `true` | Pool this event type at all. |
| `settle_seconds` | 10 | Wait at least this long after the latest event before dispatching. |
| `jitter_seconds` | 0 | Random extra wait, applied per batch. |
| `max_wait_seconds` | 45 | Hard ceiling per batch from the first event. |

Defaults are merged with the user's overrides per-event so missing fields
keep sensible values.

## `event_delivery`

Controls whether each webhook event type runs the agent immediately or is
deferred to the next heartbeat's Notification Inbox.

```json
"event_delivery": {
  "events": {
    "comment": "heartbeat",
    "mention": "heartbeat"
  },
  "realtime_for_controllers": true,
  "notification_inbox": {
    "expire_after_hours": 72,
    "max_threads": 15,
    "max_fetch": 100,
    "snippet_chars": 150,
    "categories": ["mentions", "comments", "shares"]
  }
}
```

| Field | Default | Notes |
|-------|---------|-------|
| `events` | `{}` | Map of event type → `"realtime"` \| `"heartbeat"`. Unlisted types default to realtime. |
| `realtime_for_controllers` | `true` | Controllers bypass heartbeat deferral and wake the agent immediately. Trusted users do not. |
| `notification_inbox.expire_after_hours` | `72` | Unread items older than this are auto-marked read before the digest renders. |
| `notification_inbox.max_threads` | `15` | Max thread groups shown in the digest; overflow summarized as a count. |
| `notification_inbox.max_fetch` | `100` | Cap on unread notifications fetched per tick. |
| `notification_inbox.snippet_chars` | `150` | Truncation length for the latest comment text per thread. |
| `notification_inbox.categories` | `mentions,comments,shares` | Backend notification categories included in the digest. |

`interrupt`, `asset.deleted`, and `new-conversation` cannot be set to
`heartbeat`. See [Events & webhooks](events.md#delivery-modes) for triage
semantics (handle / dismiss / defer).

Pooling (`event_pooling`) still applies to events whose delivery mode is
`realtime`. Flipping an event back to `"realtime"` restores pooling with
no other edits.

## `security`

Controls who the agent treats as a privileged actor, plus the shared secret
that guards the `/run` endpoint.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `controllers` | list[str] | `[]` | Ouro usernames **or** user ids (UUIDs), mixed freely. Controllers get the full capability set. The first username-form entry is also the one mentioned as `{@username}` when a plan enters review. |
| `trusted` | list[str] | `[]` | Ouro usernames **or** user ids (UUIDs). Trusted actors get a broader (but not full) capability set. |
| `run_secret` | str \| null | `null` | Shared secret required to authenticate `/run` requests (via the `run_secret` body field or `x-ouro-run-secret` header). When `null`, only loopback callers are trusted. |

`controllers` and `trusted` are static input — the agent never rewrites them.
At startup any usernames are resolved to user ids and cached under
`workspace/protected/data/security_resolved.json`, so later restarts skip the lookup. The
resolved ids are what the runtime authorization checks read.

Legacy keys are still accepted and migrated automatically: the old
`controller.username` block folds into `controllers`, and
`security.controller_user_ids` / `security.trusted_user_ids` /
`security.run_shared_secret` map to `controllers` / `trusted` / `run_secret`.

## `ask_controller`

Adds an internal `ask_controller` tool when at least one controller is configured
and the current capability envelope allows messaging. The question is sent to a
private Ouro conversation containing the agent and controller.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `enabled` | bool | `true` | Expose the controller-question workflow. |
| `fast_wait_seconds` | float | `90` | Keep the current run alive this long for a quick answer (maximum 240 seconds). A later answer resumes in a new run. |
| `gate_mode` | `off` \| `observe` | `observe` | Record money, destructive, scheduling, and externally visible tool calls in `runs.db` without blocking them. |

Questions and answers are persisted in the `controller_questions` table beside
run history. Quick answers bypass normal message pooling so the same agent loop
continues while its model context and prompt-cache prefix are still warm.

## `display`

| Field | Default | Notes |
|-------|---------|-------|
| `usage_table.show_reasoning` | `false` | Include reasoning tokens in the per-run usage table. |

## `refinement`

| Field | Default | Notes |
|-------|---------|-------|
| `max_changes_per_pass` | 25 | Hard cap on changes processed in one pass. |
| `max_docs_per_pass` | 15 | Hard cap on docs rewritten per pass. |
| `window_lines` | 20 | Context lines around each match in the LLM payload. |
| `model` | none | Cheap model used for refinement; falls back to the heartbeat model. |

See [Refinement](./refinement.md).

## `run_log`

Durable SQLite logging of every run to `<workspace>/protected/runs.db`.

| Field | Default | Notes |
|-------|---------|-------|
| `enabled` | `true` | When false, a no-op store (no DB file created). |
| `path` | none | Defaults to `<workspace>/protected/runs.db`. |
| `capture_steps` | `true` | Persist the per-step trace to `run_steps`. |
| `capture_reasoning` | `true` | Include provider reasoning on steps. |
| `capture_observations` | `true` | Persist tool results (observations). |
| `max_observation_chars` | `0` | Truncate observations to this many chars when writing `runs.db`; `0` = keep as stored in memory (typically spill stubs after the observations policy runs). |
| `capture_subagent_runs` | `true` | Log each subagent as a child run (`parent_run_id`). |
| `expose_to_agent` | `true` | Give the agent `recall_runs` / `get_run_detail` tools. |
| `agent_default_scope` | `team` | Max recall breadth: `team` (+ shared), `conversation`, or `all`. |
| `agent_max_results` | `10` | Default result cap for `recall_runs`. |
| `agent_max_detail_chars` | `6000` | Observation budget returned by `get_run_detail`. |

See [Run logging](./run-logging.md) for the schema, the `ouro-agents runs`
CLI, and the agent self-recall tools.

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
