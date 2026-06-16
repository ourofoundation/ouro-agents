# CLI reference

`ouro-agents` exposes a single entry point that dispatches to subcommands
(`ouro_agents/runner.py`).

```
ouro-agents [--config PATH] [--env-file PATH] [-v|-q] <command> [args]
```

Global options:

| Flag | Default | Notes |
|------|---------|-------|
| `--config PATH` | `config.json` | Path to the config file. |
| `--env-file PATH` | `.env` | `.env` file passed to `python-dotenv`. Sets `ENV_FILE` for the rest of the process. |
| `-v` / `--verbose` | off | Verbose display (debug-level info). |
| `-q` / `--quiet` | off | Errors only. |

## `serve`

```bash
ouro-agents serve --config config.json
```

Starts the FastAPI server (uvicorn) on `server.host:server.port`. Launches
the heartbeat scheduler, registers the webhook receiver at
`server.webhook_path`, and connects to all configured MCP servers.

`PYTHON_ENV=production` disables uvicorn's autoreload. Otherwise the
server runs with `reload=True`, watching only the `ouro_agents` package
(not the agent workspace or Chroma memory store).

See the [HTTP API doc](./http-api.md) for the routes the server exposes.

## `run`

```bash
ouro-agents run "Summarize the latest activity in team X."
```

Runs a single autonomous task and prints the result. Flags:

| Flag | Notes |
|------|-------|
| `--debug-md [PATH]` | Write the full system prompt and every step to a markdown file. With no argument, the path defaults to `<workspace>/debug-runs/run-<UTC timestamp>.md`. |

The run uses `RunMode.AUTONOMOUS`. Token usage and per-subagent
breakdowns are printed to the display once the run completes.

## `chat`

```bash
ouro-agents chat
ouro-agents chat --conversation-id 11111111-2222-3333-4444-555555555555
```

Starts an interactive REPL. New conversations get a fresh UUID; pass
`--conversation-id` to resume one. Inside the REPL:

| Input | Behavior |
|-------|----------|
| `<message>` | Run the agent in `RunMode.CHAT` and stream the answer. |
| `/new` | Start a new conversation (new UUID). |
| `/conversation <id>` | Switch to an existing conversation. |
| `/exit` or `/quit` (or empty input) | Exit. |

Conversation history is appended to `workspace/conversations/<id>/turns.jsonl`.
Mid-session reflection runs automatically per the configured interval.

## `heartbeat`

```bash
ouro-agents heartbeat
```

Triggers a single heartbeat tick (manual equivalent of one scheduler
firing). Useful for testing planning, proactive logic, or dryrunning what
the next scheduled run would do.

The heartbeat respects `heartbeat.active_hours` — outside the window it
returns a status string without running the agent.

## `plan`

```bash
ouro-agents plan
ouro-agents plan "Spend the day exploring rare-earth alternatives"
ouro-agents plan --team-id 11111111-... "..."
```

Forces a planning heartbeat. If `--team-id` is omitted and the agent
belongs to multiple teams, a TUI picker (`tui/team_picker.py`) prompts you
to choose. The optional positional `prompt` becomes the goal/directive
the planner builds around.

The planner emits an Ouro **quest**, a numbered plan, and a `PlanCycle`
under `workspace/teams/<team_id>/plans/`. See [Planning](./planning.md).

## `review`

```bash
ouro-agents review
```

Forces a review heartbeat. If multiple plans across teams are reviewable,
a TUI picker (`tui/review_picker.py`) lets you pick which one. The review
heartbeat re-checks comments on the quest, applies feedback, and either
promotes the plan to `active` or keeps it in `pending_review`.

## `runs`

```bash
ouro-agents runs list                       # recent runs, newest first
ouro-agents runs list --mode heartbeat --status error --since 7d
ouro-agents runs list --grep quest --json
ouro-agents runs show <run_id> [--full] [--json]
ouro-agents runs stats --since 24h
```

Read-only views over the SQLite run log (`<workspace>/runs.db`). These open the
database directly and never start an agent. `--since` takes a relative window
(`30m`, `24h`, `7d`, `2w`) or an ISO date; `runs show` accepts a unique id
prefix. See [Run logging](./run-logging.md).

## Verbosity levels

`OuroDisplay` (`ouro_agents/display.py`) reads:

- `Verbosity.QUIET` — only errors and final answers.
- `Verbosity.NORMAL` — step headers, token summaries, run summaries.
- `Verbosity.VERBOSE` — adds debug-level reasoning streams.

`config.display.usage_table.show_reasoning` toggles whether the run
summary table includes reasoning tokens.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success. |
| 1 | No subcommand provided / planning aborted with no team selected. |
