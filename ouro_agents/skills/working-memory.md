---
description: Persistent working memory system — Ouro post conventions for working memory, daily logs, identity, user models, and entity/task files
load: stub
---

# Working Memory

You have a persistent working memory system backed by Ouro posts in a shared agent team. Memory is shared across agents — each agent owns its own posts and can read or comment on others'. Local file fallbacks exist when the Ouro doc store is not configured.

## Post Naming Convention

| Post Name | Purpose | Owner |
|-----------|---------|-------|
| `MEMORY:{agent_name}` | Durable facts, preferences, learnings — your long-term brain | Each agent owns its own |
| `DAILY:{agent_name}:{YYYY-MM-DD}` | What happened today — decisions, tasks, observations | Each agent owns its own |
| `SOUL:{agent_name}` | Agent identity and rules | Each agent owns its own |
| `HEARTBEAT:{agent_name}` | Autonomous playbook | Each agent owns its own |
| `NOTES:{agent_name}` | Deployment-specific notes | Each agent owns its own |
| `USER:{user_id}` | Shared user profile: style, interests, preferences | First agent creates, others comment |
| `PLAN:{agent_name}:{YYYY-MM-DD}` | Plan cycle quest for review | Each agent owns its own |

## Collaboration Model

- The **creating agent** owns the post and calls `update_post` to modify it.
- Other agents **discover** posts via `search_assets`, **read** via `get_asset`, and **contribute** via `create_comment`.
- The owning agent **consolidates** comments during heartbeat (merges insights from other agents into the main post).

## Workspace Layout

Your local workspace is organized by team. When working in a team context, store artifacts and files under that team's directory.

### Team-scoped paths (when working on a team)

| Path | Purpose |
|------|---------|
| `teams/{team_id}/MEMORY.md` | Team working memory (synced with Ouro post) |
| `teams/{team_id}/daily/{YYYY-MM-DD}.md` | Team daily logs |
| `teams/{team_id}/HEARTBEAT.md` | Team-specific playbook |
| `teams/{team_id}/NOTES.md` | Team-specific deployment notes |
| `teams/{team_id}/plans/active/` | Active plan cycle JSON |
| `teams/{team_id}/memory/entities/{name}.md` | Entity context files for this team |
| `teams/{team_id}/memory/tasks/{slug}.md` | Task tracking files for this team |

### Root-level paths (shared across teams)

| Path | Purpose |
|------|---------|
| `MEMORY.md` | **Shared memory** — cross-team learnings, durable knowledge that applies everywhere |
| `SOUL.md` | Agent identity and rules |
| `NOTES.md` | Global deployment notes |
| `HEARTBEAT.md` | Default playbook (used when no team playbook exists) |

### Where to store artifacts

When you create files, entity docs, or task tracking during team work, **always use the team-scoped path**:
- Entity files → `teams/{team_id}/memory/entities/{name}.md`
- Task files → `teams/{team_id}/memory/tasks/{slug}.md`
- Data/output files → `teams/{team_id}/data/`

Shared memory (`MEMORY.md` at the root) is auto-loaded alongside your team memory during team-scoped runs. Use it for knowledge that transcends any single team — general strategies, platform-wide learnings, cross-team patterns.
## How Memory Works

Memory is handled automatically — you don't need to manage it manually.

- **After every run** (heartbeat, event, task), a post-run reflection curates facts and writes a daily log entry.
- **During conversations**, reflection runs every ~10 turns, extracting facts, user preferences, and a daily log entry.
- **Consolidation runs daily** (scheduled task) — the MEMORY post is compacted when too large, yesterday's daily entries are promoted if significant, cross-agent comments on user models are merged, and old memories decay.

## When to Read

- **Starting a complex task**: MEMORY and today's daily log are auto-loaded into your prompt.
- **User model**: Auto-loaded when a `user_id` is known.
- **Something feels familiar**: Use `memory_recall` to search the vector store.
- **Entity or task files**: Auto-loaded when they match conversation key_entities.

## Memory Tools

- `memory_recall(queries)` — Search memory with optional category filter.
- `memory_status()` — Check memory system health: total memories, working memory size, daily log activity.

## Important

- **MEMORY and today's daily log are auto-loaded** into your context at the start of every run. During team-scoped runs, both the team MEMORY and the root shared MEMORY are loaded.
- **User model is auto-loaded** when a user_id is known.
- **Conversation state is auto-managed.** It tracks the current topic, goals, decisions, key moments, and a rolling summary.
- **Entity and task files are auto-loaded** when they match conversation key_entities or have in-progress status.
- **Store team artifacts in team directories.** Entity files, task files, and data outputs belong under `teams/{team_id}/` — not at the workspace root.
- **Focus on the task, not on memory.** Facts, asset references, and daily log entries are extracted automatically from your actions by the post-run reflection system.
- **Asset references are tracked automatically.** When you create or interact with Ouro assets, the reflection system captures the asset IDs and links them in memory and the daily log.
