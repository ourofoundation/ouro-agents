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
| `remember(memories)` | Any mode with `Capability.MEMORY_WRITE`. | Add one or more curated semantic memories (array of specs). |
| `update_memory(memory_id, text, reason)` | Any mode with `Capability.MEMORY_WRITE`. | Rewrite a memory's text in place (supersede a stale fact). |
| `forget(items)` | Any mode with `Capability.MEMORY_WRITE`. | Permanently delete one or more memories by id (array of `{memory_id, reason}`). |
| `recall_team_memories(team_id, ...)` | When the team is available. | Convenience wrapper for `memory_recall` scoped to a team. |
| `read_doc / write_doc / append_doc` | When the doc store has the doc. | Doc-store CRUD on `MEMORY.md`, daily logs, entity files. |
| `read_user_model / write_user_model` | When `user_id` is set. | Read/write the per-user `USER:<id>` doc. |
| `comment_on_doc` | When the doc store supports it. | Post a comment on a doc-backed Ouro post. |

`ModeProfile.memory_tool_filter` can restrict the visible set
(e.g. `plan` keeps only `memory_recall`).

## Scheduler tools

Available in unrestricted modes (chat, autonomous). They wrap
`AgentScheduler` with team scoping injected:

- `list_scheduled_tasks()`
- `create_scheduled_task(name, prompt, schedule, timezone, team_id?)`
- `update_scheduled_task(task_id, ...)`
- `enable_scheduled_task(task_id)` / `disable_scheduled_task(task_id)`
- `delete_scheduled_task(task_id)` (system tasks are protected)
- `record_task_learning(task_id, learning)` — append a short bullet to
  the task's `learnings` list.

See [Scheduler](./scheduler.md) for schedule syntax.

## Run history (self-recall) tools

Available when `run_log.expose_to_agent` is true. They let the agent query its
**own** past runs (episodic memory) from `runs.db`, scoped to the current
context (see [Run logging](./run-logging.md#agent-self-recall-episodic-memory)):

- `recall_runs(query?, mode?, status?, scope?, limit?)` — compact summaries of
  matching past runs (newest first), excluding the current run.
- `get_run_detail(run_id)` — the full step trace of one past run, within the
  configured scope ceiling.

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
`max_output_chars` (stdout/stderr/result). Use it for short CLI commands and
tests; use `run_python` for persistent Python state or workflows that need
`get_ouro_client()`.

Large tool results (including shell output) are also governed by the top-level
`observations` policy: payloads over `max_inline_chars` are spilled to
`scratch/tool-outputs/<run_id>/` with a head/tail stub left in memory. The stub
and system prompt advertise that inline limit so follow-up reads can stay under
it (`head`/`tail`, bounded `sed`/`rg`) instead of re-`cat`ting the whole file.
Some tools are exempt (default: `load_skill`) because their payload *is* the
context the agent requested. History stays append-only until a rare one-shot
compact at `run_compact_ceiling` so prompt cache stays stable. See
[configuration.md](configuration.md#observations).

## MCP tools

MCP servers are connected at startup. Each tool is registered under a
qualified name `<server>:<tool>` (e.g. `ouro:get_asset`). Two ways tools
become callable inside the loop:

1. **Preloads** — context extras (event payload, inbox, planning) plus the
   mode profile list qualified names that get attached eagerly. The system
   prompt notes the bare names so the agent can call them directly.
   Role/surface capability envelopes only subtract. See
   `ouro_agents/tool_preloads.py`.
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
