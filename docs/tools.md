# Tools

This page lists the tools available inside the smolagents loop. Tools are
assembled per-run in `OuroAgent._build_agent_tools` based on the active
mode profile and any explicit preloads. Subagents get their own narrower
tool list per profile.

The four sources of tools:

1. **Memory tools** — built by `make_memory_tools`.
2. **Scheduler tools** — built by `make_scheduler_tools` (skipped when
   `restricted_servers=True`).
3. **Built-in tools** — `delegate`, `load_tool`, `load_skill`,
   `run_python`.
4. **MCP tools** — exposed as deferred entries in a directory; the agent
   pulls specific ones via `load_tool`, or some are preloaded by the mode
   profile so they're immediately callable.

## Memory tools

| Tool | Available when | Effect |
|------|----------------|--------|
| `memory_recall(query, ...)` | Always (subject to `memory_tool_filter`). | Vector search with filters: category, subject, asset, mode, team, time window. |
| `remember(text, category, importance, ...)` | Only `heartbeat`, `plan`, `review` (other modes use the reflector). | Add a curated memory. |
| `recall_team_memories(team_id, ...)` | When the team is available. | Convenience wrapper for `memory_recall` scoped to a team. |
| `read_doc / write_doc / append_doc` | When the doc store has the doc. | Doc-store CRUD on `MEMORY.md`, daily logs, entity files. |
| `read_user_model / write_user_model` | When `user_id` is set. | Read/write the per-user `USER:<id>` doc. |
| `comment_on_doc` | When the doc store supports it. | Post a comment on a doc-backed Ouro post. |

`ModeProfile.memory_tool_filter` can restrict the visible set
(e.g. `plan` keeps only `memory_recall`).

## Scheduler tools

Available in unrestricted modes (chat, autonomous, chat-reply). They wrap
`AgentScheduler` with team scoping injected:

- `list_scheduled_tasks()`
- `create_scheduled_task(name, prompt, schedule, timezone, team_id?)`
- `update_scheduled_task(task_id, ...)`
- `enable_scheduled_task(task_id)` / `disable_scheduled_task(task_id)`
- `delete_scheduled_task(task_id)` (system tasks are protected)
- `record_task_learning(task_id, learning)` — append a short bullet to
  the task's `learnings` list.

See [Scheduler](./scheduler.md) for schedule syntax.

## delegate

The dispatcher for subagents. Takes a list of task specs:

```json
{
  "tasks": [
    {"subagent": "research", "task": "...", "asset_refs": ["..."], "return_mode": "summary_only"},
    {"subagent": "writer",   "task": "..."}
  ]
}
```

A single task runs sequentially; multiple tasks fan out to a thread pool
(max 4 workers). Each result is a JSON object with `status`, `summary`,
`asset_id` (when the subagent created an Ouro asset), etc.

`return_mode`:

- `summary_only` (default) — just the curated summary + asset metadata.
- `full_text` — the subagent's full output text.
- `auto` — `full_text` if no asset was created, `summary_only` otherwise.

See [Subagents](./subagents.md).

## load_tool / load_skill

`load_tool(name)` — promote a deferred MCP tool to actually callable.
The system prompt advertises the deferred directory (qualified name +
short description); the agent calls `load_tool` once and the tool stays
attached for the rest of the run. Names accept either the qualified form
(`ouro:create_post`) or the bare name when unambiguous.

`load_skill(name)` — return the body of a skill markdown file (built-in
or workspace override). The agent typically copies the relevant parts
into its context to satisfy a request.

## run_python

A sandboxed Python tool wrapping smolagents'
`LocalPythonExecutor`. Always available in chat / autonomous modes;
included in subagents that set `needs_python_tool=True` (e.g.
`developer`).

The executor:

- Imports only the packages listed in `agent.python_packages` and
  validates them at agent startup. Use wildcard entries such as
  `pymatgen.*` or `ase.*` when submodule imports should be available.
- Receives the agent's Ouro SDK client (`OURO_API_KEY` required) under
  the global name `ouro`, so subagents can do
  `ouro.posts.create(...)`, `ouro.routes.execute(...)`, etc.
- Resolves paths relative to the workspace, so reads/writes stay scoped.

## MCP tools

MCP servers are connected at startup. Each tool is registered under a
qualified name `<server>:<tool>` (e.g. `ouro:get_asset`). Two ways tools
become callable inside the loop:

1. **Preloads** — the mode profile (or `preload_tools` argument) lists
   qualified names that get attached eagerly. The system prompt notes the
   bare names so the agent can call them directly.
2. **Deferred directory** — every other tool is listed with its short
   description. The agent calls `load_tool(name)` to attach it.

`restricted_servers=True` (heartbeat / plan / review) hides every server
not in `default_servers` (typically `["ouro"]`).

## Step callbacks

Every tool call passes through a step callback that:

- Updates the display ("step 3: ouro:create_post").
- Tracks token usage on the `UsageTracker`.
- Notifies the `AgentObserver` (server uses this to stream activity into
  Ouro chat).

`SanitizedToolCallingAgent` wraps smolagents' base agent to coerce
malformed tool-call JSON the model occasionally emits (truncated args,
shadowed booleans) before the tool runs.
