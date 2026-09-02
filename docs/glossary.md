# Glossary

Recurring terms in the codebase and the docs.

### `OuroAgent`
The orchestrator class (`ouro_agents/agent.py`). Owns the workspace, MCP
connections, memory backend, doc store, scheduler, and team registry.

### Run mode / `RunMode`
The kind of run being performed (`chat`, `autonomous`,
`heartbeat`, `plan`, `dream`). Each value maps to a `ModeProfile` that
controls prompt assembly, tools, and lifecycle hooks. See
[Run modes](./run-modes.md).

### `ModeProfile`
Declarative definition of a run mode (framing, output format, max steps,
preload tools, restricted servers, lifecycle flags). User config can
override `max_steps` and `preload_tools` per mode.

### Subagent
A focused inner `ToolCallingAgent` run dispatched by the main agent (or
another subagent) through the `delegate` tool. Defined by a
`SubAgentProfile`. See [Subagents](./subagents.md).

### `SubAgentProfile`
Declarative subagent definition (system prompt, allowed tools/servers,
max steps, model override, skills, return mode). Built-ins ship with the
package; custom profiles can be loaded from
`workspace/subagents/*.{json,yaml}`.

### MCP / `MCPServerConfig`
Model Context Protocol — the wire protocol used to talk to Ouro and
other tool servers. Each server is connected at startup; tools are
exposed as deferred entries until the agent calls `load_tool` or the
mode profile preloads them.

### Workspace
The agent's home directory on disk (`agent.workspace`, default
`./workspace`). Holds soul, memory, conversations, period logs, plans,
scheduled tasks, and the local mem0 / Chroma store. See
[Workspace layout](./workspace.md).

### Doc store
Abstraction over the agent's named documents (`MEMORY.md`, daily logs,
team memory, etc.). Implementations: `LocalDocStore`, `OuroDocStore`,
`CompositeDocStore` (local + Ouro). Most team-scoped runs use a
Composite. See [Memory model](./memory.md#working-memory-and-the-doc-store).

### `MemoryBackend`
Protocol for vector memory backends. Default implementation is mem0 on
top of Chroma. Memories carry category, basis, stability, strength, asset
references, team scope, and decay.

### Reflector
The subagent that curates long-term memory — runs after autonomous /
event-driven runs and every N turns in chat mode. Outputs a structured
JSON list of facts, preferences, learnings, and a daily-log entry.

### Preflight
The subagent that runs as visible step 0 of an autonomous / event run.
Returns `intent`, `complexity`, optional `briefing`, optional advisory
`plan`, and `worth_remembering`. Skipped in lightweight modes.

### Plan
An Ouro **quest** published by a planning run (`draft` until approved,
then `open`). There is no local plan mirror — the platform is the source
of truth. The only local state is the per-team planning cursor at
`workspace/teams/<team_id>/planning.json`. See [Planning](./planning.md).

### Quest
The Ouro asset type that carries plans and all other tracked work. Quest
items are the source of truth for item progress and waiting metadata.

### Heartbeat
A scheduled, lightweight, autonomous tick. Restricted to the `ouro` MCP
server by default. Drives the planning cycle, advances active plans,
runs proactive playbooks. Skipped outside `active_hours`.

### Dream
An evidence-driven, agent-wide review of recent runs, process friction,
memory, skills, and prior dream results. Its restricted `ModeProfile` can make
bounded direct changes or write identity-level proposals. See [Dream
mode](./dream.md).

### Reflection friction
Durable process evidence emitted by post-run reflection (for example, repeated
work, a misleading skill, or a user correction). Dream reviews the queue and
preserves resolved entries as an audit trail.

### Active hours
The daily window during which heartbeats actually run
(`heartbeat.active_hours = {start, end, timezone}`). Outside this window
the scheduler still fires but the run skips itself with a status string.

### Curiosity window
The wind-down beats at the end of each active window
(`heartbeat.curiosity = {enabled, last_beats}`). The final `last_beats`
heartbeats of the day run as `curiosity` ticks driven by the agent's
`CURIOSITY.md` playbook: self-directed exploration and side projects
instead of the quest inbox and priority ladder.

### Refinement
The LLM-driven cleanup of workspace docs that drains a typed change-set
queue (`ChangeKind.CORRECTION`, `GUIDANCE_UPDATED`, `ASSET_UPDATED`).
Runs at the start of dream and is separate from reflection friction. See
[Refinement](./refinement.md).

### Cleanup
The deterministic handler for `asset.deleted` webhook events. Prunes
vector memories and rewrites markdown / JSON references. No LLM
involved. See [Cleanup](./cleanup.md).

### Provenance
Metadata attached to incoming webhook events that classifies them
(plan feedback, historical plan feedback, team scope). Drives routing
inside the server's event handler.

### Skill
A markdown fragment (built-in or workspace) loaded into prompts. Skills
with `load: always` in frontmatter are inlined into every system
prompt; others are listed in a directory and pulled on demand. See
[Skills](./skills.md).

### Doc-store registry
The per-team `state.json` file mapping doc names (e.g.
`MEMORY:hermes:research`) to Ouro post UUIDs. Persists across restarts
so doc identity doesn't drift.

### Composite doc store
`CompositeDocStore(local, ouro)` — reads from local first for speed,
writes to both, falls back to local when the team isn't writable by
agents.

### Subject id
A free-form string identifier used by the refinement queue and memory
metadata to anchor a piece of knowledge to a real-world entity (an
asset, a memory id, a quest, etc.). Refinement uses it to find affected
docs by literal substring search.

### `EventRunContext`
The parsed shape of an inbound webhook event used everywhere in the
server (event pool, plan-feedback routing, observer construction, agent
run kwargs).

### Reply publisher / `OuroReplyPublisher`
The realtime client used by the server to stream activity, typing
indicators, and assistant chunks back into Ouro chat conversations.
