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
