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
  and preferences with metadata (team, mode, asset references, importance).
- A **scheduler** that ticks the heartbeat, consolidation, refinement, and
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
| `chat` | `ouro-agents chat` | Loads conversation state, no preflight. |
| `chat-reply` | Webhook from Ouro chat | Reply gets posted automatically. |
| `autonomous` | `ouro-agents run "..."` | Full preflight + post-reflection. |
| `heartbeat` | Scheduler tick | Lightweight, restricted to `ouro` server. |
| `plan` | `ouro-agents plan` or scheduler | Generates a plan cycle. |
| `review` | `ouro-agents review` or feedback | Updates an existing plan. |

The mode profile is layered: built-in defaults → user `modes.<name>`
overrides at config load.

## Subagents

The main agent can delegate focused work to subagents through the
`delegate` tool. Subagents are defined declaratively as
`SubAgentProfile`s — system prompt, allowed tools/servers, max steps, model
override, skills, etc.

Built-in delegatable profiles: `research`, `planner`, `executor`, `writer`,
`developer`. Internal profiles: `preflight`, `heartbeat_preflight`,
`context_loader`, `reflector`.

Custom profiles can be dropped into `workspace/subagents/*.{json,yaml}` (or
the path set by `subagents.custom_profiles_dir`) and override built-ins of
the same name. See [Subagents](./subagents.md).

Multiple delegations in one tool call run in parallel automatically.

## Memory: three layers

`ouro-agents` keeps memory at three different timescales:

1. **Vector memory** (mem0 + Chroma) — fact-shaped, queryable by category,
   subject, asset, mode, and team. Curated by the `reflector` subagent
   after every run that's worth remembering. Decays by category over time
   (see `memory.decay_rules` in config).
2. **Working memory** (`MEMORY.md`, daily logs) — markdown the agent
   maintains itself. Gets injected into every system prompt. Mirrored to
   Ouro as posts via the **doc store**.
3. **Conversation state & turns** — per-conversation summary plus recent
   user/assistant turns; chat mode injects the last few turns directly into
   the smolagents memory so the model sees them verbatim.

A periodic **consolidation** job promotes high-signal vector memories into
`MEMORY.md`. The **refinement** runner separately drains a typed
change-set queue (corrections, retractions) and uses a cheap LLM to rewrite
affected workspace docs in place.

See [Memory model](./memory.md).

## Workspaces and teams

Teams are discovered at runtime from the platform context (Ouro returns
the teams the agent's user belongs to). Team-scoped runs use a
`CompositeDocStore` that mirrors `workspace/teams/<team_id>/*.md` to Ouro
posts inside that team. The root `MEMORY.md` is shared across teams.

Per-team state lives under:

```
workspace/teams/<team_id>/
├── plans/        # plan cycles for this team
├── state.json    # name → post UUID registry
└── ...
```

See [Workspace layout](./workspace.md) and [Teams](./teams.md).

## Planning

Planning is a multi-cycle loop that turns a goal into an Ouro **quest**:

1. A `plan` heartbeat (or `ouro-agents plan` / `force_planning_heartbeat`)
   asks the planning model for a numbered plan, posts it to Ouro as a
   quest, and persists a `PlanCycle`.
2. While the quest is `pending_review`, comments and replies on the quest
   are routed by the webhook handler into a `review` heartbeat.
3. The review loop edits the plan based on feedback, marks items done /
   blocked, or auto-approves when no controller is configured.

See [Planning](./planning.md).

## Events and webhooks

The FastAPI server accepts Ouro webhook events at `server.webhook_path`.
Events are:

- **Pooled** when the same conversation/comment burst would otherwise
  trigger several runs (configurable per event in `event_pooling`).
- **Routed** to specialized paths:
  - `asset.deleted` → deterministic cleanup (no LLM).
  - Plan-feedback events → `handle_plan_feedback` → review heartbeat.
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

A single chat-reply run roughly looks like:

```
event → server                                 # FastAPI handle_event
  → event_pool.submit                          # debounce
  → _run_event_task
    → handle_plan_feedback?                    # plan-routing branch
    → asset.deleted?                           # cleanup branch
    → OuroAgent.run(task, mode=CHAT_REPLY, ...)
      → resolve mode profile + overrides
      → preflight subagent (if not skipped)
      → build tools, system prompt, dynamic context
      → smolagents ToolCallingAgent loop
        ├─ memory_recall, load_skill, load_tool, run_python, delegate, …
        └─ MCP tools (ouro:create_post, etc.)
      → append conversation turn, update conversation state
      → background: post-run reflector subagent
      → log usage breakdown to display
```

The same pipeline backs `ouro-agents run`, `ouro-agents chat`, and
`ouro-agents heartbeat` — only the mode profile and trigger differ.
