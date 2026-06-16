# Run modes

Every agent run executes inside a **mode profile** (defined in
`ouro_agents/modes/profiles.py`). The profile controls prompt assembly,
allowed tools, step budget, and lifecycle hooks (preflight, reflection,
conversation persistence).

## The `RunMode` enum

```python
class RunMode(str, Enum):
    CHAT = "chat"
    CHAT_REPLY = "chat-reply"
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
Interactive, conversation-aware. Loads conversation state, preloads Ouro
discovery tools (`get_organizations`, `get_teams`), skips preflight and
post-reflection (mid-session reflection still runs at the configured
interval). Persists turns. Default `max_steps=20`.

### `chat-reply`
Webhook-driven version of chat. Same behavior except the reply is posted
back to the Ouro conversation automatically by the server's
`ServerAgentObserver` — the final assistant reply is enough.

### `autonomous`
The default for `ouro-agents run`. Preloads action-oriented Ouro tools
(`search_assets`, `get_asset`, `execute_route`, `get_action`). Runs full
preflight + post-reflection. Persists conversation turns when
`conversation_id` is provided.

### `heartbeat`
Lightweight scheduler-driven mode. Restricted to the `ouro` MCP server,
preloads `get_asset`, `create_comment`, `create_post`. No preflight. Used
inside the heartbeat loop in `modes/heartbeat.py`.

### `plan`
Generates a new plan cycle. Restricted servers, only `memory_recall` from
memory tools, no preflight, no post-reflection. Drives a quest creation
flow inside `modes/planning.py`.

### `review`
Updates an existing plan based on feedback. Same restrictions as `plan`.

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
- `worth_remembering` — gates whether the post-run reflector runs.

After the main loop finishes, if `skip_post_reflection=False` and the run
is `worth_remembering`, the agent dispatches the `reflector` subagent in
the background. It curates facts/preferences into vector memory and
appends a daily-log entry. Failures are logged but never block the user
response.

For chat modes, mid-session reflection runs when the conversation-state updater
adds a new key moment.

## How a mode is resolved at runtime

1. `OuroAgent.run(..., mode=mode)` looks up the built-in profile in
   `MODE_REGISTRY`.
2. If `config.modes.profiles[<name>]` exists, `apply_mode_override` patches
   `max_steps` / `preload_tools`.
3. The merged profile is threaded through `_build_agent_tools` and
   `_build_system_prompt`.
4. Explicit `preload_tools` arguments to `run()` are merged with the
   profile's preloads (explicit takes precedence, deduped).
