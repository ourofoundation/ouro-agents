# Concepts

A high-level mental model of `ouro-agents`. Each concept has a dedicated
deep-dive; this page explains how they fit together.

## The agent process

A single Python process owns:

- A **workspace** on disk (`SOUL.md`, `MEMORY.md`, daily logs, conversations,
  per-team plans, scheduled task state).
- A connection to one or more **MCP servers** (always Ouro, often plus a
  search server).
- A **memory backend** (mem0 + Chroma by default) that stores curated facts
  and preferences with metadata (team, asset references, basis, stability, strength).
- A **scheduler** that ticks the heartbeat, evidence-driven dream review, and
  any user-defined recurring tasks.
- A FastAPI **server** that exposes `/run`, `/health`, `/tasks`, and a
  webhook receiver for Ouro events.

`OuroAgent` (in `ouro_agents/agent.py`) is the orchestrator. It builds a
prompt, builds a tool list, runs the smolagents `ToolCallingAgent` loop, and
writes everything back to the workspace + memory.

## Run modes

Every agent run executes inside a **mode profile** that controls how the
prompt is assembled, what tools are available, how many steps are allowed,
and whether the run does pre-flight / post-reflection.

Built-in modes (see [`run-modes.md`](./run-modes.md)):

| Mode | What triggers it | Notes |
|------|------------------|-------|
| `chat` | `ouro-agents chat` or webhook from Ouro chat | Injects conversation history, keeps post-reflection. Webhook replies are posted automatically. |
| `autonomous` | `ouro-agents run "..."` | Runs post-reflection. |
| `heartbeat` | Scheduler tick | One strong run (decide + execute); Ouro MCP only; search via subagents; semantic memory gated by tick summary. |
| `plan` | `ouro-agents plan` or scheduler | Generates a plan cycle. |
| `review` | `ouro-agents review` or feedback | Updates an existing plan. |
| `dream` | Scheduler or `ouro-agents dream` | Agent-wide evidence review, memory maintenance, and bounded self-improvement. |

The mode profile is layered: built-in defaults → user `modes.<name>`
overrides at config load.

## Subagents

The main agent can delegate focused work to subagents through the
`delegate` tool. Subagents are defined declaratively as
`SubAgentProfile`s — system prompt, allowed tools/servers, max steps, model
override, skills, etc.

Built-in delegatable profiles: `search`, `research`, `planner`, `executor`,
`writer`, `developer`. Internal profile: `reflector`.

Custom profiles can be dropped into `workspace/subagents/*.{json,yaml}` (or
the path set by `subagents.custom_profiles_dir`) and override built-ins of
the same name. See [Subagents](./subagents.md).

Multiple delegations in one tool call run in parallel automatically.

## Memory: three layers

`ouro-agents` keeps memory at three different timescales:

1. **Vector memory** (mem0 + Chroma) — semantic, queryable by category,
   subject, asset, and team. Curated by the `reflector` subagent after every
   run that's worth remembering. Recall reinforces memory strength; dream
   can decay old unaccessed memories when the evidence warrants it.
2. **Working memory** (`MEMORY.md`, daily logs) — markdown the agent
   maintains itself. Gets injected into every system prompt. Mirrored to
   Ouro as posts via the **doc store**.
3. **Conversation history** — recent user/assistant turns from the
   platform or `conversations/{id}.jsonl`; chat mode injects them
   directly into the smolagents memory so the model sees them verbatim.

The evidence-driven **dream** process runs refinement and a `MEMORY.md`
compaction baseline, then uses bounded tools for justified memory maintenance
and improvements. Refinement drains a typed change-set queue (corrections,
retractions) and uses a cheap LLM to rewrite affected workspace docs in place.

See [Memory model](./memory.md).

## Workspaces and teams

Teams are discovered at runtime from the platform context (Ouro returns
the teams the agent's user belongs to). Team-scoped runs use a
`CompositeDocStore` that mirrors `workspace/teams/<team_id>/*.md` to Ouro
posts inside that team. The root `MEMORY.md` is shared across teams.

Per-team state lives under:

```
workspace/teams/<team_id>/
├── planning.json # planning cursor (last planned at, pending drafts)
├── state.json    # name → post UUID registry
└── ...
```

See [Workspace layout](./workspace.md) and [Teams](./teams.md).

## Planning

A plan is just an Ouro **quest**:

1. A planning run (heartbeat cadence, or `ouro-agents plan` /
   `force_planning_heartbeat`) asks the planning model for a plan and
   publishes it as a **draft quest**; the per-team cursor records it.
2. While the quest is a draft, comments and replies on it are routed by
   the webhook handler into a `review` run; with no feedback the draft
   auto-opens after the review window.
3. Once open, the quest's items are worked through the heartbeat's quest
   inbox like any other quest the agent owns.

See [Planning](./planning.md).

## Events and webhooks

The FastAPI server accepts Ouro webhook events at `server.webhook_path`.
Events are:

- **Pooled** when the same comment/mention burst would otherwise trigger
  several runs (configurable per event in `event_pooling`). Chat starts immediately.
- **Routed** to specialized paths:
  - `asset.deleted` → deterministic cleanup (no LLM).
  - Chat events → realtime activity + streaming reply.
  - Otherwise → a normal `OuroAgent.run()` in the appropriate mode.

See [Events & webhooks](./events.md) and [Cleanup](./cleanup.md).

## Skills

Skills are markdown fragments shipped with the package (or dropped into
`workspace/skills/`) that get loaded into prompts on demand. A skill can
declare `load: always` in its frontmatter to be inlined into every system
prompt; otherwise it's listed in a directory and loaded by name through the
`load_skill` tool.

Subagent profiles declare a `skills:` list and have those bodies appended
to their task context. See [Skills](./skills.md).

## Putting it together: a typical run

A single chat run (webhook-triggered) roughly looks like:

```
event → server                                 # FastAPI handle_event
  → event_pool.submit                          # debounce
  → _run_event_task
    → asset.deleted?                           # cleanup branch
    → OuroAgent.run(task, mode=CHAT, ...)
      → resolve mode profile + overrides
      → build tools, system prompt, dynamic context
      → smolagents ToolCallingAgent loop
        ├─ memory_recall, load_skill, load_tool, run_python, delegate, …
        └─ MCP tools (ouro:create_post, etc.)
      → append conversation turn (when the profile persists turns)
      → background: post-run reflector subagent
      → log usage breakdown to display
```

The same pipeline backs `ouro-agents run`, `ouro-agents chat`, and
`ouro-agents heartbeat` — only the mode profile and trigger differ.
