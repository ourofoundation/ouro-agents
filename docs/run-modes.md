# Run modes

Every agent run executes inside a **mode profile** (defined in
`ouro_agents/modes/profiles.py`). The profile controls prompt assembly,
allowed tools, step budget, and lifecycle hooks (preflight, reflection,
conversation persistence).

## The `RunMode` enum

```python
class RunMode(str, Enum):
    CHAT = "chat"
    AUTONOMOUS = "autonomous"
    HEARTBEAT = "heartbeat"
    PLAN = "plan"
    REVIEW = "review"
```

Each value maps to a built-in `ModeProfile`.

## What a profile controls

`ModeProfile` is a Pydantic model. The fields most often tuned:

| Field | Effect |
|-------|--------|
| `framing` | High-level "you are doing X" prose injected at the top of the system prompt. |
| `output_format` | Closing instructions about how to respond. |
| `max_steps` | Hard cap on smolagents loop iterations. |
| `preload_tools` | Qualified MCP tool names eagerly attached so the model can call them without `load_tool`. |
| `restricted_servers` | When True, only `default_servers` MCP tools are visible (others can't be loaded at all). |
| `default_servers` | The whitelist used when `restricted_servers=True`. |
| `allow_delegation` | Whether the `delegate` tool is exposed. |
| `memory_tool_filter` | Optional whitelist of memory tool names (e.g. `["memory_recall"]`). |
| `lightweight` | Skips most heavy context-loading branches in `_build_system_prompt`. |
| `skip_preflight` | Don't run the `preflight` subagent before the main loop. |
| `skip_post_reflection` | Don't run the `reflector` subagent after the main loop. |
| `load_conversation_state` | Inject conversation summary into the prompt. |
| `load_scheduled_tasks` | Inject a scheduled-task summary block. |
| `append_conversation_turns` | Persist user/assistant turns to disk after the run. |
| `update_conversation_state` | Re-summarize and save the per-conversation state file. |

## Built-in profiles

### `chat`
Interactive, conversation-aware. Used both by the local `ouro-agents chat`
CLI and by webhook chat events (`new-message`, `new-conversation`). Loads
conversation state, preloads no tools (everything stays one `load_tool`
away — most chat turns need zero tools), uses `tool_choice="auto"` so the
model can reply to casual messages without a forced tool call, and skips
preflight for lower reply latency (the trivial-message regex still
fast-paths greetings; post-run reflection still runs in the background and
never delays the reply). Uses the mid model tier when configured (falls
back to strong). Default `max_steps=20`.

Delivery is the observer's job, not the mode's: for webhook events the
server's `ServerAgentObserver` posts the final assistant message back to the
Ouro conversation automatically; for the CLI it's rendered to the terminal.
Either way the agent just produces a final assistant message — which is why
the old `chat` / `chat-reply` split was collapsed into this one mode. The
config aliases `chat-reply` and `reply` still resolve to `chat`.

### `autonomous`
The default for `ouro-agents run`. Preloads action-oriented Ouro tools
(`search_assets`, `get_asset`, `execute_route`, `get_action`). Runs full
preflight + post-reflection. Persists conversation turns when
`conversation_id` is provided.

### `heartbeat`
Scheduler-driven mode. Restricted to the `ouro` MCP server (search is
delegated to subagents). Runs one strong-model preflight that produces an
executable brief, then a cheap mid/light executor follows that brief.
Post-run reflection is gated by preflight `worth_remembering` (pass/no-op
ticks skip the reflector). Used inside the heartbeat loop in
`modes/heartbeat.py`.

### `plan`
Generates a new plan cycle. Restricted servers, only `memory_recall` from
memory tools, no preflight, no post-reflection. Drives a quest creation
flow inside `modes/planning.py`.

### `review`
Updates an existing plan based on feedback. Same restrictions as `plan`.

### `dream` (run-log mode, not a ModeProfile)
Memory maintenance cycle (compaction, promotion, decay, review). Not in the
`RunMode` enum — it has no smolagents framing/tools/max_steps — but each scope
still writes a `mode=dream` row to `runs.db` with tokens/cost via
`OuroAgent._run_dream_scope`. Mutation detail and truncated LLM I/O live in
`workspace/data/dream_runs/*.json` (linked via `run_id` / `audit_log`).

## Overriding a profile from config

Both `max_steps` and `preload_tools` accept user overrides under
`modes.<name>` in `config.json`:

```json
"modes": {
  "chat": { "max_steps": 40 },
  "run":  { "max_steps": 60 },
  "heartbeat": {
    "max_steps": 20
  }
}
```

(Mode names are user-friendly aliases; `run` is normalized to `autonomous`
and `planning` to `plan`. Built-in keys still work too.)

The `planning` and `heartbeat` blocks under `modes` also carry the
top-level scheduler/model fields for those special modes (cadence, model,
active hours, etc.) — these are hoisted out into the dedicated
`PlanningConfig` / `HeartbeatConfig` sections during config load. See the
[Configuration reference](./configuration.md#modes).

## Preflight and reflection

When a profile has `skip_preflight=False`, the agent runs the `preflight`
subagent as visible **step 0** of the run. Preflight returns:

- `intent` and `complexity` — used for logging.
- `briefing` — extra context (memory hits, entity files) injected before
  the user task.
- `plan` — an advisory plan injected as `## Advisory Action Plan`.
- `tools` — MCP tools the plan will need, merged into the run's preloads so
  the main agent can call them without a `load_tool` round-trip.
- `worth_remembering` — gates whether the post-run reflector runs.
  Heartbeat pass/no-op objectives always skip reflection even if this flag
  is true.

After the main loop finishes, if `skip_post_reflection=False` and the run
is worth remembering, the agent dispatches the `reflector` subagent in
the background. It curates facts/preferences into vector memory, updates
the user model, and appends a daily-log entry. Failures are logged but
never block the user response.

## Preemption

Background modes (`heartbeat`, `plan`, `review`, and scheduled tasks) are
*preemptible*: when an interactive run (chat, direct request) arrives while
a background run holds the run lock, the background run is cancelled at its
next step boundary and the interactive run proceeds. Background work simply
resumes on its next tick.

## How a mode is resolved at runtime

1. `OuroAgent.run(..., mode=mode)` looks up the built-in profile in
   `MODE_REGISTRY`.
2. If `config.modes.profiles[<name>]` exists, `apply_mode_override` patches
   `max_steps` / `preload_tools`.
3. The merged profile is threaded through `_build_agent_tools` and
   `_build_system_prompt`.
4. Explicit `preload_tools` arguments to `run()` are merged with the
   profile's preloads (explicit takes precedence, deduped).
