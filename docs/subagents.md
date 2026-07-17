# Subagents

Subagents are smaller `ToolCallingAgent` runs spawned by the main agent (or
by other subagents via chaining) to handle focused work. They keep the
main context clean, can be tuned independently per-profile, and run in
parallel automatically when multiple are dispatched at once.

## The `SubAgentProfile` model

Defined in `ouro_agents/subagents/profiles.py`. Key fields:

| Field | Purpose |
|-------|---------|
| `name` | Unique identifier; used by `delegate` and config overrides. |
| `description` | Shown to the main agent in its subagent directory. |
| `system_prompt` | Static prompt template. |
| `allowed_tools` | Internal tool names (e.g. `memory_recall`). |
| `allowed_servers` | MCP server whitelist (e.g. `["search"]`). |
| `can_load_mcp_tools` | If True, exposes `load_tool` so the subagent can pull MCP tools on demand. |
| `preload_tools` | MCP tools preloaded for this subagent. |
| `delegatable` | If True, the main agent can target this profile from `delegate`. |
| `max_steps` | Step budget for the subagent loop. |
| `model_override` | Hard-coded model id; usually omitted in favor of config. |
| `default_return_mode` | `summary_only` (default), `full_text`, or `auto`. |
| `can_delegate_to` | Names of other delegatable profiles this subagent may chain to. |
| `memory_scopes` | Restrict vector memory hits to these category tags. |
| `needs_python_tool` | Inject `run_python` (with the Ouro SDK client). |
| `skills` | Names of markdown skill files to inline into the task. |
| `subagent_log_level` | OuroLogger level for the inner loop. |

## Built-in profiles

| Name | Delegatable | What it does |
|------|-------------|--------------|
| `preflight` | no | Step 0 of a normal run: classify intent, fetch memory, optionally sketch a plan. |
| `heartbeat_preflight` | no | Decides what the agent should focus on this heartbeat tick. |
| `reflector` | no | Curates long-term memory after a run or every N turns in chat. |
| `research` | yes | Investigates a topic via `tavily_search`, posts the writeup to Ouro. |
| `planner` | yes | Returns a short numbered execution plan. |
| `executor` | yes | Runs a focused sub-task with MCP tools. |
| `writer` | yes | Drafts polished posts; saves them to Ouro. |
| `developer` | yes | Uses the Ouro Python SDK directly through `run_python`. |

## Custom profiles

Drop a `*.json` or `*.yaml` file into `workspace/subagents/` (or the
directory set by `subagents.custom_profiles_dir`) and it will be loaded at
startup. Custom profiles override built-ins of the same name.

Minimal example (`workspace/subagents/copywriter.yaml`):

```yaml
name: copywriter
description: Writes short marketing-style summaries with a punchy hook.
delegatable: true
system_prompt: |
  You are a senior copywriter. Always produce a single tight paragraph
  with a strong opening sentence...
allowed_tools: []
preload_tools:
  - ouro:create_post
max_steps: 4
skills:
  - ouro
  - ouro_markdown
```

Filenames map to the `name` field if you omit it. Use `delegatable: true`
to make the profile callable from the main agent.

## Configuring per-profile behavior

The `subagents` block in `config.json` carries non-prompt overrides:

```json
"subagents": {
  "writer": { "model": "anthropic/claude-sonnet-4" },
  "research": { "max_steps": 30 },
  "preflight": {
    "max_steps": 4
  }
}
```

With a top-level `models` block, preflight/research/reflector default to
`light` and writer/executor/developer to `strong`, so most profiles only
need `max_steps`. An explicit `model` (as on `writer` above) still wins.

Resolution cascade for the model used by a subagent run:

1. `profile.model_override` (rarely set in code).
2. `subagents.<name>.model` from config.
3. `subagents.default_model`.
4. Role→tier from top-level `models` (e.g. research → `light`).
5. `agent.model` (last-resort fallback).

`max_steps` and `reasoning` use the same precedence inside their respective
domains. With `models` set, prefer omitting per-profile `model` /
`reasoning` and let the tier map choose.

## The `delegate` tool

When a profile has `allow_delegation=True` (true for `chat` and
`autonomous` by default), the main agent has a `delegate` tool that
takes a list of task specs:

```json
[
  {"subagent": "research", "task": "Find recent papers on X", "asset_refs": ["..."]},
  {"subagent": "writer",   "task": "Draft an intro section based on the research"}
]
```

Notable parameters:

- `subagent` — must match a delegatable profile name.
- `task` — a self-contained brief; subagents do not see the main thread.
- `asset_refs` — Ouro asset UUIDs passed as input context.
- `return_mode` — `summary_only` (default), `full_text`, or `auto`. The
  subagent always saves its full output as an Ouro asset; the return mode
  controls how much of that comes back to the main agent's context.

A list with one task runs sequentially. A list with multiple tasks runs in
parallel via a thread pool (max 4 workers). Each output is a JSON dict
with `status`, `summary`, `asset_id` (when applicable), etc.

## Subagent execution lifecycle

The main agent's `_run_subagent` does:

1. Apply config overrides to the profile (`max_steps`, `model`, …).
2. Resolve a model with a `MirroredUsageTracker` so token usage rolls up
   into the parent's run usage report.
3. Build a `SubAgentContext` with the shared prompt context (soul, notes,
   working memory, plans index, doc store) plus profile-specific bits.
4. Dispatch through `subagents.runner.run_subagent`, which reuses
   `SanitizedToolCallingAgent` and the same step callbacks.
5. Return a `SubAgentResult` (`text`, `success`, `error`, `usage`,
   `asset_id`, `asset_description`).

Parallel dispatch (`_run_subagents_parallel`) builds independent contexts
and runs them concurrently. The same path backs both the `delegate` tool
and any internal multi-subagent flows.

## Chaining

A subagent can delegate to others listed in its `can_delegate_to`. The
runner injects a `delegate`-equivalent tool into the inner loop with the
allowed names. This lets, for example, `research` hand a finished
investigation to `writer` for a polished publication, without bouncing
back through the main agent.
