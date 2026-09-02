# Run modes

Every agent run executes inside a **mode profile** (defined in
`ouro_agents/modes/profiles.py`). The profile controls prompt assembly,
allowed tools, step budget, and lifecycle hooks (reflection,
conversation persistence).

## The `RunMode` enum

```python
class RunMode(str, Enum):
    CHAT = "chat"
    AUTONOMOUS = "autonomous"
    HEARTBEAT = "heartbeat"
    PLAN = "plan"
    DREAM = "dream"
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
| `skip_post_reflection` | Don't run the `reflector` subagent after the main loop. |
| `conversational` | Chat-style history injection and framing (plain task messages, no work directive). |
| `load_scheduled_tasks` | Inject a scheduled-task summary block. |
| `append_conversation_turns` | Persist user/assistant turns to disk after the run. |

## Built-in profiles

### `chat`
Interactive, conversation-aware. Used both by the local `ouro-agents chat`
CLI and by webhook chat events (`new-message`, `new-conversation`). Marks
`conversational=True` so prior turns are injected as history, preloads no
tools (everything stays one `load_tool` away — most chat turns need zero
tools), uses `tool_choice="auto"` so the model can reply to casual
messages without a forced tool call. The trivial-message regex still
fast-paths greetings; post-run reflection still runs in the background
and never delays the reply. Uses the mid model tier when configured
(falls back to strong). Default `max_steps=20`.

Delivery is the observer's job, not the mode's: for webhook events the
server's `ServerAgentObserver` posts the final assistant message back to the
Ouro conversation automatically; for the CLI it's rendered to the terminal.
Either way the agent just produces a final assistant message — which is why
the old `chat` / `chat-reply` split was collapsed into this one mode. The
config aliases `chat-reply` and `reply` still resolve to `chat`.

### `autonomous`
The default for `ouro-agents run`. Preloads action-oriented Ouro tools
(`search_assets`, `get_asset`, `execute_route`, `get_action`). Runs
post-reflection. Persists conversation turns when `conversation_id` is
provided. Uses the mid model tier when configured (falls back to strong).

### `heartbeat`
Scheduler-driven mode. Restricted to the `ouro` MCP server (search is
delegated to subagents). One mid-model run owns the whole tick: it
decides and executes one bounded slice, delegating heavy work to cheap
subagents. Tick kind (`quest_work` vs `open_ended` vs `curiosity`) is
chosen deterministically before the LLM call and gates context/framing.
`curiosity` ticks fire when the clock falls in the curiosity window — the
final `heartbeat.curiosity.last_beats` beats of the active window — and
run the agent's `CURIOSITY.md` playbook instead of the quest inbox /
priority ladder (planning runs are also suppressed during the window).
The final
tick-summary JSON carries `action` / `worth_remembering` / `memory_notes`
for run-log columns and memory gating. Default `max_steps=40`. Used inside
the heartbeat loop in `modes/heartbeat.py`.

### `plan`
Generates a new plan cycle. Restricted servers, only `memory_recall` from
memory tools, no post-reflection. Drives a quest creation flow inside
`modes/planning.py`.

### `dream`
An agent-wide `ModeProfile`: a restricted, non-delegating loop that grades the
previous dream, reviews bounded run evidence and reflection friction, and
makes only permitted self-improvements. It runs refinement and a compaction
baseline before the review, and skips post-reflection and conversation
persistence. `dream.max_steps`, `dream.servers`, and the dream write tiers
bound the run. It logs as `mode=dream` with a normal step trace and a separate
dream audit. See [Dream mode](./dream.md).

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

## Heartbeat tick summary and reflection

Heartbeat ends with a structured tick-summary JSON:

- `action` — short label, or `none` when passing.
- `details` — what changed / why the tick passed.
- `selected_priority` — playbook tier acted on, or null.
- `worth_remembering` — gates vector-memory candidates.
- `memory_notes` — optional templates for the reflector when remembering.

Pass ticks (`action: none`) skip semantic memory and daily-log episodes.
Non-pass ticks can still write a daily-log episode even when
`worth_remembering` is false. Run-log columns `preflight_intent` /
`preflight_complexity` / `worth_remembering` are populated from this
summary (schema retained for compatibility).

After the main loop finishes, if `skip_post_reflection=False` and the run
warrants memory and/or an episode, the agent dispatches the `reflector`
subagent in the background. Failures are logged but never block the user
response.

## Concurrency

Top-level modes (`chat`, `autonomous` / comments, `heartbeat`, `plan`,
and dream) **overlap** on one agent process. There is no global run
lock and no cross-mode preemption.

Shared-state hardening:

- Each run binds a `RunContext` (usage tracker, subagent ledger, run id).
- Stdio MCP invocations take a per-server call lock; streamable-http MCP does not.
- Durable workspace / doc writes take a process-wide memory write lock.

Chat **interrupt** still cancels only the in-flight run(s) for that
conversation.

### Future: cross-mode awareness

An in-process `ActiveRunRegistry` already tracks live runs (mode, event type,
started_at, conversation/team ids). A future `list_active_runs` tool can expose
that to agents so a heartbeat can discover sibling activity via a tool call
rather than static prompt context. Not implemented yet.

## How a mode is resolved at runtime

1. `OuroAgent.run(..., mode=mode)` looks up the built-in profile in
   `MODE_REGISTRY`.
2. If `config.modes.profiles[<name>]` exists, `apply_mode_override` patches
   `max_steps` / `preload_tools`.
3. The merged profile is threaded through `_build_agent_tools` and
   `_build_system_prompt`.
4. Explicit `preload_tools` arguments to `run()` (event extras, inbox,
   planning) are merged first-seen with the profile's preloads, then
   filtered by the capability envelope.
