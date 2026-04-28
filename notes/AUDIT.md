# Ouro Shared Memory Migration — Audit

## Context

We migrated the ouro-agents memory system from local file I/O to Ouro posts in a shared agent team. The core abstraction is `OuroDocStore` in `ouro_agents/memory/ouro_docs.py` — it wraps `search_assets`, `get_asset`, `create_post`, `update_post`, `create_comment`, and `get_comments` MCP tools. All file-based memory now routes through it when `agent.org_id` and `agent.team_id` are configured, falling back to local files otherwise.

**Naming convention for posts:**
- `SOUL:{agent_name}` — agent identity
- `HEARTBEAT:{agent_name}` — agent playbook
- `NOTES:{agent_name}` — agent notes
- `MEMORY:{agent_name}` — working memory
- `DAILY:{agent_name}:{YYYY-MM-DD}` — daily log
- `USER:{user_id}` — shared user model (first agent creates, others comment)
- `PLAN:{agent_name}:{YYYY-MM-DD}` — plan cycle post

**Collaboration model:** Creator owns the post and calls `update_post`. Other agents discover posts via `search_assets`, read via `get_asset`, and contribute via `create_comment`. The owning agent consolidates comments during heartbeat.

## What was done (original migration)

1. **`ouro_agents/memory/ouro_docs.py`** — New `OuroDocStore` class (read/write/comment/search)
2. **`ouro_agents/agent.py`** — `doc_store` initialized after MCP connect; `_load_working_memory`, identity loading, heartbeat playbook, user model loading, reflection, consolidation all route through doc_store
3. **`ouro_agents/memory/consolidation.py`** — `compact_memory_md`, `promote_daily_entries`, `run_consolidation` accept `doc_store`; new `_consolidate_user_comments` for merging cross-agent contributions
4. **`ouro_agents/memory/reflection.py`** — `write_daily_log`, `apply_reflection` accept `doc_store`; `REFLECTION_PROMPT` includes `asset_refs`
5. **`ouro_agents/memory/user_model.py`** — `load_user_model`, `append_to_user_model`, `ensure_user_model` accept `doc_store`; owner writes directly, others comment
6. **`ouro_agents/memory/tools.py`** — `memory_store` accepts `asset_refs`, `memory_recall` surfaces them
7. **`ouro_agents/soul.py`** — `MCP_TOOL_RULES` updated with asset tracking guidance
8. **`ouro_agents/planning.py`** — `build_planning_prompt` uses `PLAN:{agent_name}:{date}` naming, routes to memory team
9. **`ouro_agents/config.py`** — `MemoryConfig` has `org_id` and `team_id`
10. **`ouro_agents/runner.py`** — `bootstrap-memory` CLI command with `_ensure_team_membership`
11. **Deleted** `ouro_agents/memory/retrieval.py`; removed `gather_raw_context` from `subagents/research.py`

## Follow-up audit fixes (completed)

### 1. `ouro_agents/skills/working-memory.md` — Rewritten
Was entirely stale (referenced local files). Now documents the Ouro post naming convention, collaboration model, local fallbacks, and updated memory tools.

### 2. `ouro_agents/memory/context_loader.py` — Doc-store-aware
`_load_recent_daily_context()` and `load_entity_context()` now accept optional `doc_store` and `agent_name` params. Yesterday's daily log loads from Ouro when doc_store is available, falls back to local files otherwise. Entity and task files remain local-only (workspace-specific working notes).

### 3. `ouro_agents/subagents/research.py` — Dead code removed
`_load_working_memory()` and `_load_active_tasks()` were dead code (no callers after `gather_raw_context` was removed). Both functions removed along with unused imports (`date`, `timedelta`, `Path`). Only `synthesize_briefing()` remains.

### 4. `ouro_agents/memory/tools.py` — Doc-store-aware
`make_memory_tools()` now accepts optional `doc_store`. `memory_status` reads MEMORY post size and daily log entries from doc_store when available, falls back to local files. Reports storage backend (Ouro posts vs local files).

### 5. `ouro_agents/notes.py` — Deleted
`load_notes()` was a 3-line function. Inlined into `agent.py __init__` as a direct `Path.read_text()` call.

### 6. `ouro_agents/soul.py` — `load_soul()` removed
`load_soul()` was a 3-line function. Inlined into `agent.py __init__`. `build_prompt()`, framing constants, and `MCP_TOOL_RULES` remain in `soul.py`.

### 7. `ouro_agents/agent.py` — Wiring updated
- `doc_store` now passed to `make_memory_tools()` and `load_entity_context()`
- `agent_name` passed to `load_entity_context()` for daily log resolution
- Removed imports of `load_notes` and `load_soul`

## Design decisions made

- **Entity files and task files**: Stay local. They're workspace-specific working notes, not shared across agents. The naming convention (`ENTITY:{agent_name}:{slug}`) is available if we want to migrate them later.
- **Pre-MCP fallback pattern**: Kept. `agent.py __init__` reads soul/notes from local files, then `_load_identity_from_ouro()` overwrites after MCP connects. The brief stale-data window is acceptable since the agent doesn't process requests until MCP is connected.
- **`notes.py` / `load_soul()`**: Inlined and deleted rather than kept as separate modules.

## Doc store cleanup (2026-04)

After the migration above stabilized, the local-vs-Ouro split in `ouro_agents/memory/ouro_docs.py` was simplified:

- **Extracted routing into `CompositeDocStore`.** Identity prefixes (`SOUL`/`HEARTBEAT`/`NOTES`) always go to `LocalDocStore`; everything else goes to `OuroDocStore` when configured, falling back to local. Replaces the `_IDENTITY_PREFIXES` early-returns scattered across `OuroDocStore.read/write/append/exists`.
- **`OuroDocStore` is now a pure Ouro client.** Dropped the embedded `self._local: LocalDocStore` and the eager shadow-write that mirrored every Ouro write to disk. `workspace_sync` is now the only bridge between local files and Ouro posts (still scoped to team `MEMORY.md` by design).
- **Purged one-time migration code:**
  - Legacy registry filename (`doc_registry.json`) and the flat-dict registry payload — only `state.json` with `{team, docs}` is read now.
  - `_canonicalize_name`/`_legacy_aliases`/`_candidate_names`/`_ensure_canonical_remote_name` — the `MEMORY:agent` → `MEMORY:agent:team-slug` and DAILY remote-rename paths are gone.
  - `_remote_name_candidates` collapsed to a single canonical title via `_remote_display_name`.
  - `LocalDocStore._name_to_path` legacy fallbacks (`memory/daily/`, `memory/users/`, root `MEMORY.md`) — one canonical layout: identity at workspace root, team docs under `teams/{team_id}/`, shared docs under `shared/`.
- **Inlined `_read_by_uuid`/`_write_by_uuid`** into `append_daily_log_entry` and a shared `_content_to_markdown` helper.
- **`is_owner` kept.** It has live callers in `consolidation._consolidate_user_comments` and `user_model.append_to_user_model` (used to choose between writing directly and contributing via comment). Process-local semantics are intentional — on restart the agent conservatively comments and consolidation merges.

`ouro_agents/agent.py` now builds `CompositeDocStore` everywhere (`self.doc_store` for the no-team default; per-team stores via the new `_build_team_doc_store` helper). `_sync_workspace_docs` reaches into each composite's `.ouro` to feed `sync_workspace`. Test coverage updated: `TestCompositeDocStore` covers the routing rule; migration-only tests were removed.

### Follow-up tightening

After the first pass settled, four more cleanups landed together:

- **`_resolve_or_create` helper.** The duplicated "resolve, then double-checked-lock and create" preambles in `OuroDocStore.write` / `append` collapsed from ~25 lines each to ~9 lines, with the locking/ambiguity logic written once.
- **Unified list-item append.** `append_list_item` now takes an optional `initial_md=` and is cache-first on Ouro (one retrieve + one update on a registry hit, direct create on a miss). The separate `append_daily_log_entry` method is gone, and `reflection.write_daily_log` lost its three-branch `getattr` dance.
- **`DocStore` Protocol moved into `ouro_docs.py`.** `__init__.py` re-exports it. The `try/except ImportError` shim at the bottom of `ouro_docs.py` is gone — one source of truth.
- **Frontmatter helpers extracted into `memory/frontmatter.py`.** `LocalDocStore`, `agent.py`, and `workspace_sync.py` all import from there. The lazy import inside `LocalDocStore` (which existed only to break a cycle) is gone.

### Doc-store correctness pass (2026-04, third pass)

- **Ownership persists across restarts.** `OuroDocStore._owner_cache` was process-local, so on restart the agent's own posts (USER models, etc.) were treated as foreign — `user_model.append_to_user_model` and `consolidation._consolidate_user_comments` would fall into their comment-only branches and wait for consolidation to merge the agent's contributions back into its own posts.

  Fix: each `state.json` doc entry now carries optional ownership: `{"uuid": "...", "owned": true}`. Bare uuid strings still load (legacy registries continue to work). `_create` flags `owned=true`, recovery via `_resolve_name` does not. Reloading the store rebuilds `_owner_cache` from the registry.

- **`SHARED:memory` lives in the doc store now.** `agent._load_shared_memory` previously bypassed the abstraction with a hand-rolled `Path.read_text` + `strip_frontmatter` against workspace-root `MEMORY.md`. It's now `self.doc_store.read("SHARED:memory")`. `SHARED` joined `IDENTITY_PREFIXES` so the composite always pins it to the local store regardless of team scope, and `LocalDocStore._name_to_path` routes `SHARED:memory` to root `MEMORY.md` (with a `SHARED_{key}.md` fallback for any future siblings). Last "raw filesystem" call from `agent.py` is gone.

- **Naming logic moved to `memory/naming.py`.** `slugify_team_key`, `team_doc_key`, `memory_doc_name`, `daily_doc_name`, `daily_doc_display_name`, `remote_display_name`, `is_singleton_name`, `is_identity_name`, plus the `IDENTITY_PREFIXES`/`SINGLETON_PREFIXES` constants. The doc-store implementations now contain only I/O. Module dependency graph is clean: `naming` ← `ouro_docs` ← `workspace_sync` ← `agent`, with `frontmatter` as a leaf shared by `ouro_docs` and `workspace_sync`.

## How to verify

```bash
cd ouro-agents

# Syntax check all files
python -c "import ast; [ast.parse(open(f).read()) for f in __import__('glob').glob('ouro_agents/**/*.py', recursive=True)]"

# Import chain check
python -c "from ouro_agents.agent import OuroAgent; print('OK')"

# Run tests
python -m pytest tests/ -v

# Bootstrap (creates team + seeds posts from local files)
python -m ouro_agents.runner bootstrap-memory --config config.json
```
