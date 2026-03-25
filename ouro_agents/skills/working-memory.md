---
description: Persistent working memory system — file conventions for MEMORY.md, daily logs, entities, tasks, user models, and conversation state
load: stub
---

# Working Memory

You have a persistent working memory system stored as markdown files in your workspace. Use it to remember things across conversations and sessions.

## File Structure

| Path | Purpose | Style |
|------|---------|-------|
| `MEMORY.md` | Durable facts, preferences, learnings — your long-term brain | Curated, concise |
| `memory/daily/YYYY-MM-DD.md` | What happened today — decisions, tasks, observations | Timestamped, append-only |
| `memory/entities/<name>.md` | Deep knowledge about a person, project, or topic | Structured, updated |
| `memory/tasks/<slug>.md` | Working notes for a multi-session task | Free-form, living document |
| `memory/users/<user_id>.md` | Per-user profile: style, interests, preferences, patterns | Structured, curated |
| `conversations/<id>.state.json` | Conversation state: topic, goals, decisions, entities | Auto-managed JSON |

## When to Write

- **Learned something important?** → Append to `MEMORY.md`
- **Completed a task, made a decision, or had a notable interaction?** → Append to today's daily log
- **Accumulating knowledge about a specific person or project?** → Create or update an entity file
- **Starting a task that will span multiple sessions?** → Create a task file

## When to Read

- **Starting a complex task**: Read `MEMORY.md` and today's daily log for context (these are auto-loaded into your prompt, but entity and task files are not).
- **Resuming work on something**: Read the relevant task file.
- **Asked about a person or project**: Check for an entity file.
- **Something feels familiar**: Search memory files with `search_files` or read likely candidates.

## Format Conventions

### MEMORY.md
Keep this under ~2000 tokens. It's loaded into every prompt, so be selective. Only write facts you'll need again. Periodically review and prune stale entries.

```markdown
## Facts
- User prefers concise responses over verbose ones
- The Ouro platform uses Supabase for the database layer
- Materials project uses Modal for compute

## Preferences
- Default to markdown format for reports
- Always check memory before starting research tasks

## Learnings
- When creating Ouro posts, always specify org_id and team_id
- Bitcoin data is best sourced from CoinGecko API
```

### Daily Logs (`memory/daily/YYYY-MM-DD.md`)
Append timestamped entries. Don't edit previous entries.

```markdown
# 2026-03-11

- 09:15 — Received task to research phonon calculations. Started by reviewing existing materials apps.
- 10:30 — Published report on phonon methods to Ouro (team: materials-science).
- 14:00 — User asked about job market data. Created draft at drafts/job-report.md.
```

### Entity Files (`memory/entities/<name>.md`)
Use a consistent slug for the filename. Structure with sections.

```markdown
# Project: Materials Science Platform

## Overview
A suite of Modal apps for computational materials science.

## Key Facts
- Uses ORB model for atomic simulations
- Deployed on Modal with webhook callbacks to Ouro
- Main app: materials/apps/materials/app.py

## Recent Activity
- 2026-03-10: Added defect calculation feature
```

### Task Files (`memory/tasks/<slug>.md`)
Living documents for ongoing work. Update as you go.

```markdown
# Task: Research Phonon Methods

## Goal
Compare phonon calculation approaches for the materials platform.

## Status
In progress — reviewed ASE and Phonopy approaches.

## Notes
- ASE has built-in phonon support but limited k-point sampling
- Phonopy is more full-featured but requires VASP/QE force calculations
- ORB model can provide forces directly — worth testing

## Next Steps
- [ ] Test phonon calculation with ORB forces
- [ ] Benchmark against DFT reference data
```

## How to Write

Use `append_file` inside `run_python` to add entries to memory files. This is the simplest way to log without overwriting existing content:

```python
from datetime import datetime
ts = datetime.now().strftime("%H:%M")
append_file("memory/daily/2026-03-11.md", f"\n- {ts} — Completed research on phonon methods.\n")
```

For curating MEMORY.md (removing stale facts, reorganizing), use `read_file` + `write_file` to rewrite the whole file.

### User Models (`memory/users/<user_id>.md`)
Track what you learn about each user across conversations. Updated automatically by the reflection system and by explicit `memory_store` calls.

```markdown
# User: user-abc-123

## Communication Style
- Prefers concise, direct responses
- Casual tone

## Interests
- Geopolitics, materials science

## Preferences
- Likes posts published to topical teams, not "All"
- Prefers research-then-publish workflows

## Working Patterns
- Often iterates on content (asks for revisions, different angles)
```

## Memory Tools

- `memory_store(fact, category, importance)` — Store a fact with optional category (`fact`, `preference`, `learning`, `decision`, `observation`, `general`) and importance (0.0-1.0).
- `memory_recall(query, category, limit)` — Search memory with optional category filter.
- `memory_status()` — Check memory system health: total memories, MEMORY.md size, daily log activity, entity/task file counts.

## Important

- **MEMORY.md and today's daily log are auto-loaded** into your context at the start of every run. You always have access to them without reading files.
- **User model files are auto-loaded** when a user_id is known. You don't need to read them manually.
- **Conversation state is auto-managed.** It tracks the current topic, goals, decisions, key moments, and a rolling conversation summary. You don't write to it directly.
- **Entity and task files are auto-loaded** when they match conversation key_entities or have in-progress status. You don't need to read them manually in most cases.
- **Don't duplicate mem0.** The `memory_store`/`memory_recall` tools handle quick atomic facts. Working memory files are for structured, longer-form notes you curate yourself.
- **Reflection runs automatically** every 10 turns during a conversation, curating important facts into mem0 and updating user models. You don't need to manually save everything — focus on storing only surprising or non-obvious facts via `memory_store`.
- **Memory consolidation runs on heartbeat** — MEMORY.md is auto-compacted when it gets too large, yesterday's daily log entries are promoted if significant, and old unaccessed memories decay in importance.
