# Workspace layout

The workspace is the agent's home directory on disk. Everything an agent
"remembers" between processes — identity, working memory, conversations,
team plans, scheduled tasks — lives here.

`agent.workspace` in `config.json` (default `./workspace`) sets the root.

## Layout

```
workspace/
├── SOUL.md                      # identity, values, operating rules (required)
├── NOTES.md                     # optional ambient notes
├── MEMORY.md                    # shared cross-team memory (curated)
├── runs.jsonl                   # append-only log of every run
├── data/
│   ├── platform_context.json    # cached profile/orgs/teams from Ouro
│   ├── scheduled_tasks.json     # persisted ScheduledTask list
│   ├── last_dream_period        # dream-cycle rollover marker (rhythm)
│   └── .log_prefix_v1           # migration marker (DAILY→LOG rename)
├── conversations/<id>/
│   ├── state.json               # ConversationState
│   └── turns.jsonl              # raw user/assistant turns
├── debug-runs/                  # ouro-agents run --debug-md output
├── memory/                      # mem0 + Chroma store (do not edit)
├── skills/                      # workspace skill overrides
├── subagents/                   # custom SubAgentProfile files (json/yaml)
├── shared/
│   ├── memory/MEMORY.md         # unscoped agent memory (no team context)
│   ├── logs/<period>.md         # root period logs (daily/weekly/biweekly)
│   └── users/<user_id>.md       # user-model files
└── teams/<team_id>/
    ├── MEMORY.md                # team-scoped working memory
    ├── logs/<period>.md         # team period logs
    ├── plans/                   # PlanCycle JSON files
    └── state.json               # doc-name → Ouro post UUID registry
```

Legacy workspaces may still have `shared/daily/` or `teams/<id>/daily/` until
the startup migration moves them to `logs/`. The doc store reads both during
the transition.

## SOUL.md (required)

A short markdown description of the agent's identity, values, and
operating rules. The full body is inlined into every system prompt, so
keep it tight — a few hundred words is plenty.

The example in this repo (`workspace/SOUL.md`) covers the structure that
seems to work well: Identity → Core Values → Operating Rules → Writing
Style → Standing Orders.

## NOTES.md (optional)

Free-form ambient notes the agent reads each run. Useful for ongoing
project context that doesn't belong in `MEMORY.md`. The agent can also
maintain a per-team `NOTES:<agent>` doc through the doc store.

## MEMORY.md and team memory

`MEMORY.md` at the root is the agent's curated memory shared across all
teams. Per-team memory lives at `teams/<team_id>/MEMORY.md` and is
mirrored to Ouro as a post (`MEMORY:<agent>:<team_slug>`) whenever the
team is writable by agents.

The agent maintains both files itself:

- The `reflector` subagent appends curated facts after runs.
- The dream cycle promotes high-importance log entries and compacts
  working memory on each `memory.rhythm` boundary.

Hand-editing is fine, but expect edits to be reorganized over time as
the agent rewrites for clarity.

## Period logs

Each run that produces a worth-remembering reflection appends an entry to
the **current period's** log. The period window follows `memory.rhythm`
(`daily`, `weekly`, or `biweekly`):

| Rhythm | Example period key | Local path (team-scoped) |
|--------|------------------|--------------------------|
| daily | `2026-06-02` | `teams/<team_id>/logs/2026-06-02.md` |
| weekly | `2026-W23` | `teams/<team_id>/logs/2026-W23.md` |
| biweekly | `2026-06-01-2w` | `teams/<team_id>/logs/2026-06-01-2w.md` |

Logical doc keys use the `LOG:` prefix (e.g. `LOG:<agent>:<team_slug>:<period>`).
Root-scoped logs live under `shared/logs/` when no team context is set.

Entries are short bullet-style summaries with timestamps. Paired with the
run log (`runs.jsonl`) they make a complete chronological record.

## Conversations

Per-conversation directory under `conversations/<conversation_id>/`:

- `state.json` — `ConversationState` (current topic, turn count, open
  questions, recent decisions). Maintained for `chat` and `chat-reply`.
- `turns.jsonl` — append-only record of user/assistant turns with
  optional `tool_summary` payloads.

## Teams

Each team the agent operates in has a directory under `teams/<team_id>/`.
Notable files:

- `state.json` — registry maintained by `OuroDocStore` mapping document
  names to Ouro post UUIDs (and ownership flags). Persistence keeps doc
  identity stable across restarts.
- `plans/<id>.json` — `PlanCycle` files written by the planning runner.
  See [Planning](./planning.md).

Slug + name metadata for each team gets cached in `state.json` so the
doc store can format friendly post titles.

## Scheduled tasks

`data/scheduled_tasks.json` is a list of `ScheduledTask` entries (see
[Scheduler](./scheduler.md)). The file is rewritten atomically (tmp +
rename) on every change so a crash mid-write can't corrupt it.

## Memory store

`memory/` holds mem0's working files and a Chroma database. Treat it as
opaque — never hand-edit. To reset memory, stop the agent, remove the
directory, and restart.

## Skills and subagents

`skills/*.md` overrides any built-in skill of the same name; new files
become available to all agent runs in this workspace.

`subagents/*.{json,yaml}` defines custom subagent profiles, overriding
built-ins of the same name (see [Subagents](./subagents.md)).

## Workspace sync

When `OuroDocStore` is active, `_sync_workspace_docs` runs on every MCP
connect (startup + heartbeat). It does a bidirectional sync between
local team docs and Ouro posts:

- Pushes locally-modified docs to Ouro.
- Pulls remote-only edits back into local files.

Conflicts use the most recent `last_updated` timestamp in frontmatter as
the tiebreaker. Any errors are logged to the run output but don't block
the agent.

## Debug runs

`ouro-agents run --debug-md` writes a self-contained markdown trace of a
run under `debug-runs/`. Each file contains the full system prompt, the
effective task (with preflight context), and every step of the agent
loop. Useful for diffing prompts across config changes.
