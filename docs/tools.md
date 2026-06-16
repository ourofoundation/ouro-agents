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
   `run_python`, and optionally `run_shell`.
4. **MCP tools** — exposed as deferred entries in a directory; the agent
   pulls specific ones via `load_tool`, or some are preloaded by the mode
   profile so they're immediately callable.

## Memory tools

| Tool | Available when | Effect |
|------|----------------|--------|
| `memory_recall(query, ...)` | Always (subject to `memory_tool_filter`). | Vector search with filters: category, subject, asset, team, time window. |
| `remember(text, category, basis, stability, strength, ...)` | Any mode with `Capability.MEMORY_WRITE`. | Add a curated semantic memory. |
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

A persistent Python sandbox. Always available in chat / autonomous modes;
included in subagents that set `needs_python_tool=True` (e.g. `developer`).

The backend is selected by `agent.sandbox.mode`:

- `local` preserves the existing restricted smolagents
  `LocalPythonExecutor`. It uses an import allowlist and legacy workspace
  helpers such as `read_file` and `write_file`.
- `docker` runs a Python worker inside `agent.sandbox.image` with the
  workspace bind-mounted at `/workspace` by default. In this mode, the
  container is the sandbox boundary, so code can use normal Python APIs:
  `pathlib.Path`, `open`, `shutil`, `glob`, `zipfile`, installed packages,
  and `subprocess.run(...)`.

In both modes, Python state persists across calls within one run and is
discarded afterwards. Files written under the workspace persist across runs.

`agent.sandbox.python_packages` entries are advertised in the tool description.
In local mode they are added to the import allowlist; in Docker mode they are
validated inside the configured image at startup. Use wildcard entries such as
`pymatgen.*` or `ase.*` when submodule imports should be available.

When Ouro credentials are available, the tool exposes `get_ouro_client()`
for SDK access. In Docker mode, the container receives only the environment
variables listed in `agent.sandbox.env_allowlist` (by default
`OURO_API_KEY` and `OURO_BASE_URL`).

## run_shell

`run_shell(command)` is available only when `agent.sandbox.mode` is `docker`
and `agent.sandbox.enable_shell` is `true`. It executes a non-interactive shell
command in the same Docker sandbox container used by `run_python`, with the
workspace mounted as the working directory.

The command inherits the Docker sandbox limits: configured image, network mode,
environment allowlist, memory/CPU/pid limits, `timeout_seconds`, and
`max_output_chars`. The result includes exit code, stdout, stderr, and a timeout
marker when applicable. Use it for short CLI commands and tests; use
`run_python` for persistent Python state or workflows that need `get_ouro_client()`.

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
