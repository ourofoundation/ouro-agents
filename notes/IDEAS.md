# ouro-agents: Ideas to revisit

A scratch list of ideas that came up but weren't worth doing right now.
Trim aggressively as items ship or get rejected.

## SQLite-backed scratch store for `run_python`

**Date:** 2026-05-06
**Context:** A running agent commented that there's no convenient way to persist
data across runs from inside `run_python` — it has to publish posts/datasets or
hand-roll JSON files. We added docs (`scratch/state.json` pattern) as the
short-term fix and explicitly rejected adding `pickle` to the default authorized
imports because it punches through the import allowlist (RCE on any malicious
`.pkl` that lands in the workspace).

**Idea:** Add `sqlite3` to `DEFAULT_AUTHORIZED_IMPORTS` and expose a
pre-opened connection (or thin KV helper) bound to
`workspace/scratch/agent.db`. Gives the agent:
- A real persistent KV / structured store
- No deserialization-of-code footgun (unlike pickle / marshal)
- Stdlib, no extra install
- Atomic writes for free

**Open questions:**
- One shared DB per workspace, or per-team? Probably per-workspace with a
  `team_id` column so subagents can scope queries.
- Surface as raw `sqlite3` connection, or a small `kv_get`/`kv_set`/`kv_list`
  helper layered on top? Probably both — helpers for the 90% case, raw conn
  for anything else.
- Concurrency: heartbeat + chat-reply can overlap. SQLite WAL mode handles
  this, but worth confirming under the agent's actual call patterns.
- Migration story if we later want to evolve the schema.

**Files likely touched:**
- `ouro_agents/tools/python_tool.py` — add `sqlite3` to defaults, expose
  helpers in `_make_workspace_fs`
- `ouro_agents/skills/filesystem.md` — document the KV pattern
- `ouro_agents/skills/python.md` — short pointer
