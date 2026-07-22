# Memory model

`ouro-agents` keeps memory at three timescales:

1. **Vector memory** — durable facts, preferences, and direction,
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
    category: str          # "fact" | "preference" | "direction"
    basis: str             # "stated" | "observed" | "inferred"
    stability: str         # "stable" | "evolving"
    strength: float        # reinforced on recall, decayed by dream
    created_at: str
    last_accessed: str
    team_ids: list[str]
    asset_ids: list[str]
    user_id: str
    subject_type: str
    subject_id: str
    last_verified: str
    verification_hint: str
    metadata: dict[str, Any]
```

Memories are written by the `reflector` subagent after each run
using structured handoffs that include category,
semantic basis, stability, strength, and asset references. They are searched at
run time by
the `memory_recall` tool (subagents include this tool in their
`allowed_tools` list).

The reflector emits strength as a word (`minor`/`normal`/`high`, mapped to
0.3/0.5/0.8) and may list `supersedes` memory IDs on a candidate — IDs
surfaced by its own `memory_recall` search that the new memory contradicts
or replaces. Superseded memories are deleted as soon as the replacement is
stored, so bans and reversals take effect immediately instead of waiting
for dream consolidation.

### Categories and decay

Vector memory is semantic. Raw observations and one-off activity are
episodes and go to the period log instead of mem0. The durable categories are:

- `direction` — explicit human guidance or deliberate planning decisions.
- `preference` — user communication/workflow preferences.
- `fact` — durable project, team, asset, or agent knowledge.

`memory.decay_after_days` controls the single use-based decay law.
Recall hits update `last_accessed` and reinforce `strength`; dream weakens
unused memories and deletes memories that fall below the strength floor.
Direction memories do not decay automatically.

`memory.dream_enabled` runs a consolidation ("dream") pass —
once per `memory.rhythm` period, at `memory.dream_time` — that:

- Drains the refinement change queue.
- Promotes important period-log episodes into `MEMORY.md`.
- Distills reinforced direction memories into workspace lesson skills
  (`skills/lessons-<topic>.md`) so procedural lessons load by topic
  instead of competing for recall.
- Decays old, unaccessed memories by strength.
- Reviews stale `stability="evolving"` memories.
- Caps `MEMORY.md` size at `memory.memory_md_max_tokens`.

Each scope writes a JSON audit under `workspace/data/dream_runs/` with
mutations (`operations`), truncated LLM I/O (`llm_calls`), skips/warnings,
phase timings, and a `run_id` that links to the `mode=dream` row in
`runs.db`. See [Run logging](./run-logging.md).

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

Entity files (`memory/entities/*.md`) and task files (`memory/tasks/*.md`)
carry YAML frontmatter with a `description` and optional `aliases` list.
The context loader matches conversation key entities against file stems
and aliases, and injects a one-line-per-file index
(`build_memory_index`) into every run so unmatched files stay
discoverable through the agent's file tools.

The `_sync_workspace_docs` helper does a bidirectional sync at startup so
local edits made offline propagate to Ouro and vice versa.

### Document name conventions

Names are scoped by team and agent:

- `MEMORY:<agent>` — root, shared across teams.
- `MEMORY:<agent>:<team_slug>` — per-team memory.
- `LOG:<agent>:<period>` — root log. `<period>` follows `memory.rhythm`:
  `2026-06-02` (daily), `2026-W23` (weekly), or `2026-06-01-2w` (biweekly).
- `LOG:<agent>:<team_slug>:<period>` — per-team log (same `<period>` scheme).
- `USER:<user_id>` — the user-model file consulted when a `user_id` is
  passed into `OuroAgent.run`.
- `NOTES:<agent>` — opt-in notes file (falls back to root `NOTES.md` when
  not present).
- `SHARED:memory` — used internally to read the root `MEMORY.md` from a
  team-scoped run.

## Conversation memory

Per-conversation state lives under `workspace/conversations/<id>/`:

- `state.json` — a `ConversationState` object (current topic, turn count,
  open questions, recent decisions, etc.). Used by the `chat` mode.
- `turns.jsonl` — raw user/assistant turns, optionally with a structured
  `tool_summary`.

In chat mode the runner injects the last 8 turns (built into smolagents
`ActionStep`s) into the agent's memory at run time so the model sees them
as real history rather than as paraphrased prompt context.

## Memory tools available to the agent

Built by `make_memory_tools` (`memory/tools.py`):

- `memory_recall(query, ...)` — semantic search with filters for category,
  subject, asset, team, and time window. Always available. In chat, recall
  expands the query with the current `ConversationState`.
- `remember(text, category, basis, stability, strength, …)` — write a
  durable semantic memory. Exposed in any mode that grants
  `Capability.MEMORY_WRITE`; reflection remains the safety net.
- `update_memory(memory_id, text, reason)` — revise a memory in place when
  it is partly wrong or has evolved, keeping its scope and category.
- `forget(memory_id, reason)` — permanently delete a stale or superseded
  memory. Both are gated by `Capability.MEMORY_WRITE`, and `memory_recall`
  surfaces the `id` to act on when the agent can write.

Profiles can restrict the visible memory tool set with
`memory_tool_filter` (e.g. `plan` only sees `memory_recall`).

## Manual curation (CLI)

`ouro-agents memory` opens a full-screen browser for a human first pass over
the vector store without starting an agent (viewing never reinforces memories).

Startup filters (optional): `--team`, `--category`, `--subject-type`,
`--since`, `--grep`, `--limit`. Use `--json` to dump matching memories and
exit (for scripting).

One memory at a time, weakest-first. Keys:

- `←` / `→` (or `↑` / `↓`) — previous / next
- `d` `d` — delete (press twice to confirm)
- `e` — edit in a modal (Ctrl+S save, Esc cancel)
- `q` — quit

## Reflection

Every run mode that writes memory (chat, autonomous, event-driven) shares one
post-run reflection path: after a run marked `worth_remembering` (chat/autonomous
non-trivial turns, or heartbeat tick-summary), the `reflector` subagent stores
curated facts, updates the user model from observed preferences, and optionally
writes a daily-log entry.

Reflection runs in a background thread so the user response is never blocked
by it.

## Refinement and cleanup

Dream and cleanup maintain memory hygiene:

- **Dream refinement** — drains a typed change-set queue (corrections,
  retractions) as the first dream phase and uses a cheap LLM to rewrite affected docs in-place.
  See [Refinement](./refinement.md).
- **Cleanup** — handles `asset.deleted` webhook events deterministically:
  prunes vector memories that referenced the asset and rewrites markdown
  / JSON references as `[deleted]`. No LLM involved. See
  [Cleanup](./cleanup.md).
