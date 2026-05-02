# Memory model

`ouro-agents` keeps memory at three timescales:

1. **Vector memory** — facts, preferences, learnings, decisions, etc.,
   stored in mem0 (default backend) on top of a Chroma vector store.
2. **Working memory** — `MEMORY.md`, daily logs, entity files, plan files
   on disk, mirrored to Ouro as posts via the **doc store**.
3. **Conversation memory** — a per-conversation state file plus the recent
   user/assistant turns, replayed into the smolagents memory in chat mode.

This page focuses on how those layers cooperate. Backend implementation
details live in `ouro_agents/memory/`.

## Vector memory

The shape of a stored memory is `MemoryResult` (`memory/__init__.py`):

```python
class MemoryResult(BaseModel):
    id: str
    text: str
    score: float
    category: str          # "fact" | "preference" | "learning" | "decision" |
                           # "direction" | "observation" | "general"
    importance: float
    created_at: str
    last_accessed: str
    team_ids: list[str]
    asset_ids: list[str]
    user_id: str
    mode: str
    confidence: float
    subject_type: str
    subject_id: str
    metadata: dict[str, Any]
```

Memories are written by the `reflector` subagent (after a run or
mid-session in chat) using structured handoffs that include category,
importance, and asset references. They are searched at preflight time by
the `memory_recall` tool (subagents include this tool in their
`allowed_tools` list).

### Categories and decay

Each category has its own decay policy in `memory.decay_rules`:

```json
"decay_rules": {
  "direction":   { "after_days": null, "factor": 1.0 },  // never decay
  "decision":    { "after_days": null, "factor": 1.0 },
  "fact":        { "after_days": 180,  "factor": 0.7 },
  "preference":  { "after_days": 365,  "factor": 0.8 },
  "learning":    { "after_days": 180,  "factor": 0.8 },
  "observation": { "after_days": 30,   "factor": 0.5 }
}
```

`memory.decay_after_days` is a global fallback when a category has no
specific rule. `memory.consolidation_enabled` (cron `consolidation_schedule`)
runs a consolidation pass that:

- Promotes high-importance, frequently-accessed memories into `MEMORY.md`.
- Decays older memories by their category factor.
- Caps `MEMORY.md` size at `memory.memory_md_max_tokens`.

### Team scoping

Vector memories are stored under the writing agent's `agent_id`, not under
the team. The `team_ids` field on each memory records the team(s) the
memory was learned in, so a future shared-team-memory strategy is purely a
backend concern. Read more in `ouro_agents/memory/README.md`.

## Working memory and the doc store

`workspace/MEMORY.md` is markdown the agent maintains itself — a curated
list of durable, cross-team facts. The `doc store` (`memory/ouro_docs.py`)
is the abstraction that lets the agent read/write named docs without
caring whether they live on disk, on Ouro, or both.

Three implementations:

- `LocalDocStore` — pure filesystem under `workspace/`.
- `OuroDocStore` — backed by Ouro posts. Maintains a name → post UUID
  registry in `workspace/teams/<team_id>/state.json`.
- `CompositeDocStore` — local for fast reads, Ouro for durable writes;
  each per-team store is a Composite. The root (no-team) store is
  Composite(local, **None**) — root docs stay local-only.

Per-team writes go through `_build_team_doc_store`. If the team is not
writable by agents (`source_policy` is `web_only`), it falls back to
local-only without crashing.

The `_sync_workspace_docs` helper does a bidirectional sync at startup so
local edits made offline propagate to Ouro and vice versa.

### Document name conventions

Names are scoped by team and agent:

- `MEMORY:<agent>` — root, shared across teams.
- `MEMORY:<agent>:<team_slug>` — per-team memory.
- `DAILY:<agent>:<YYYY-MM-DD>` — root daily log.
- `DAILY:<agent>:<team_slug>:<YYYY-MM-DD>` — per-team daily log.
- `USER:<user_id>` — the user-model file consulted when a `user_id` is
  passed into `OuroAgent.run`.
- `NOTES:<agent>` — opt-in notes file (falls back to root `NOTES.md` when
  not present).
- `SHARED:memory` — used internally to read the root `MEMORY.md` from a
  team-scoped run.

## Conversation memory

Per-conversation state lives under `workspace/conversations/<id>/`:

- `state.json` — a `ConversationState` object (current topic, turn count,
  open questions, recent decisions, etc.). Used by `chat` and `chat-reply`
  modes.
- `turns.jsonl` — raw user/assistant turns, optionally with a structured
  `tool_summary`.

In chat mode the runner injects the last 8 turns (built into smolagents
`ActionStep`s) into the agent's memory at run time so the model sees them
as real history rather than as paraphrased prompt context.

## Memory tools available to the agent

Built by `make_memory_tools` (`memory/tools.py`):

- `memory_recall(query, ...)` — semantic search with filters for category,
  subject, asset, mode, team, time window. Always available.
- `remember(text, category, importance, …)` — write a memory. Only
  exposed during `heartbeat`, `plan`, and `review` runs (other modes rely
  on the reflector).
- Doc-store tools for reading/appending entity files and daily logs.

Profiles can restrict the visible memory tool set with
`memory_tool_filter` (e.g. `plan` only sees `memory_recall`).

## Reflection

Two reflection paths use the same `reflector` subagent:

- **Mid-session** — every `memory.mid_session_reflection_interval` chat
  turns, after the assistant has responded. Updates the conversation
  state with anything worth remembering.
- **Post-run** — after autonomous / heartbeat / event-driven runs that
  preflight marked `worth_remembering`. Stores curated facts and
  optionally writes a daily-log entry.

Both run in the background (`asyncio.create_task` + `to_thread`) so the
user response is never blocked by reflection.

## Refinement and cleanup

Two separate processes maintain memory hygiene:

- **Refinement** — drains a typed change-set queue (corrections,
  retractions) and uses a cheap LLM to rewrite affected docs in-place.
  See [Refinement](./refinement.md).
- **Cleanup** — handles `asset.deleted` webhook events deterministically:
  prunes vector memories that referenced the asset and rewrites markdown
  / JSON references as `[deleted]`. No LLM involved. See
  [Cleanup](./cleanup.md).
