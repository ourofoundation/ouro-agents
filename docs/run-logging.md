# Run logging

Every agent run — in **every mode** (chat, autonomous, heartbeat,
plan, review, dream) — writes one rich, structured record to a SQLite database at
`<workspace>/runs.db`, plus the full step trace (when applicable). Records are written on
success, error, **and** cancellation, so failed and interrupted runs are just
as visible as successful ones.

The implementation lives in [`ouro_agents/run_log.py`](../ouro_agents/run_log.py).
Normal smolagents modes are wrapped by `OuroAgent._run_blocking`. Dream is not a
smolagents loop, so `OuroAgent._run_dream_scope` writes the same schema (one row
per memory scope, grouped by `tick_id` within a cycle).

## Why SQLite

One queryable file, no external service, and easy to slice by mode, team,
conversation, cost, or status. WAL mode keeps reads non-blocking while runs
write.

## Schema

### `runs` — one row per run

| Column | Notes |
|--------|-------|
| `run_id` | Primary key. A fresh time-ordered UUIDv7 per run. |
| `parent_run_id` | Set for nested runs and subagent child runs. |
| `tick_id` | Shared by all runs in one heartbeat tick (planning + review + action). |
| `agent_name` | The agent that produced the run. |
| `mode` | `chat`, `autonomous`, `heartbeat`, `plan`, `review`, `dream`, … or `subagent:<name>`. |
| `event_type` | Webhook event that triggered the run, if any. |
| `status` | `success` \| `error` \| `cancelled`. |
| `started_at`, `ended_at`, `duration_s` | Lifecycle timing (UTC ISO-8601). |
| `conversation_id`, `team_id`, `user_id`, `trigger_turn_id` | Routing/context. |
| `capability_role`, `capability_surface` | Security envelope the run executed under. |
| `model` | Resolved model id. |
| `task`, `result` | Full request and final answer (not truncated). |
| `error_type`, `error_message`, `error_traceback` | Populated on failure. |
| `preflight_intent`, `preflight_complexity`, `worth_remembering` | From the strategist step (`act`/`pass` and `priority:N`). |
| `num_steps`, `num_tool_calls` | Rollups. |
| `input_tokens`, `output_tokens`, `cached_input_tokens`, `reasoning_tokens`, `total_tokens`, `num_api_calls`, `cost_usd` | Flattened usage for cheap queries. |
| `usage_json`, `subagent_ledger_json`, `memory_ledger_json` | Full usage detail as JSON. |
| `created_at` | Row insert time. |

### `run_steps` — one row per smolagents memory step

| Column | Notes |
|--------|-------|
| `run_id` | Foreign key into `runs` (indexed). |
| `step_index` | Order within the run. |
| `step_number` | smolagents step number (action/final steps). |
| `step_type` | `task` \| `planning` \| `action` \| `final` \| `other`. |
| `model_output` | The model's text output for the step. |
| `reasoning` | Provider reasoning, when present. |
| `tool_calls_json` | `[{"name", "args"}, …]`. |
| `observations` | Tool results (untruncated by default). |
| `error` | Step error, if any. |
| `is_final_answer` | Marks the final step. |
| `duration_s` | Step wall time. |

Indexes: `runs(started_at, mode, conversation_id, team_id, parent_run_id,
tick_id)` and `run_steps(run_id)`.

## Configuration

Under `run_log` in `config.json` (all optional — defaults shown):

```json
"run_log": {
  "enabled": true,
  "path": null,                 // default: <workspace>/runs.db
  "capture_steps": true,
  "capture_reasoning": true,
  "capture_observations": true,
  "max_observation_chars": 0,   // 0 = keep full observations
  "capture_subagent_runs": true,

  "expose_to_agent": true,           // give the agent recall tools
  "agent_default_scope": "team",     // team | conversation | all
  "agent_max_results": 10,
  "agent_max_detail_chars": 6000
}
```

Set `enabled: false` for a no-op store (no DB file is created). Writes are
best-effort: a logging failure never breaks a run.

## Browsing runs from the CLI

`ouro-agents runs` opens `runs.db` **read-only** (no agent is started):

```bash
ouro-agents runs list                      # recent runs (newest first)
ouro-agents runs list --mode heartbeat --status error --since 7d
ouro-agents runs list --grep "quest" --json
ouro-agents runs show <run_id>             # full record + step trace
ouro-agents runs show <prefix> --full      # untruncated observations
ouro-agents runs stats --since 24h         # cost/tokens/failures by mode
```

`--since` accepts relative windows (`30m`, `24h`, `7d`, `2w`) or an ISO date.
`runs show` accepts a unique id prefix.

## Agent self-recall (episodic memory)

When `expose_to_agent` is true, the agent gets two read-only tools so it can
query its **own** history — distinct from curated vector memory (facts) and
conversation state (continuity):

- **`recall_runs(query, mode, status, scope, limit)`** — search past runs and
  return compact summaries (task/result previews, status, tokens, tools used).
- **`get_run_detail(run_id)`** — the full step trace of one past run
  (model output, reasoning, tool calls, observations), truncated to
  `agent_max_detail_chars`.

**Scope / privacy.** `runs.db` spans every conversation, team, and user the
agent has served. `agent_default_scope` is the *maximum* breadth the agent may
see:

| Scope | The agent can recall… |
|-------|------------------------|
| `conversation` | only runs in the current conversation thread. |
| `team` (default) | runs in the current team **plus** shared/no-team runs. |
| `all` | every run, across all teams and conversations. |

The agent may *narrow* the scope per call but can never *widen* it beyond the
configured ceiling, and `get_run_detail` refuses runs outside that ceiling — so
one team's content can't leak into another. The current in-progress run is
always excluded from recall.

## Example queries

Recent runs:

```sql
SELECT started_at, mode, status, duration_s, total_tokens, cost_usd
FROM runs ORDER BY started_at DESC LIMIT 20;
```

One run's full trace:

```sql
SELECT step_index, step_type, substr(model_output, 1, 80) AS output,
       tool_calls_json, substr(observations, 1, 80) AS obs
FROM run_steps WHERE run_id = ? ORDER BY step_index;
```

Cost by mode:

```sql
SELECT mode, COUNT(*) AS runs, ROUND(SUM(cost_usd), 4) AS cost
FROM runs GROUP BY mode ORDER BY cost DESC;
```

Failed runs with their error:

```sql
SELECT started_at, mode, error_type, error_message
FROM runs WHERE status != 'success' ORDER BY started_at DESC;
```

Everything in one heartbeat tick:

```sql
SELECT run_id, mode, status, duration_s
FROM runs WHERE tick_id = ? ORDER BY started_at;
```

A run and its subagent children:

```sql
SELECT run_id, mode, status FROM runs
WHERE run_id = ? OR parent_run_id = ?;
```
