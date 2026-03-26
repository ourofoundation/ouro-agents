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
| `PLAN:{agent_name}:{YYYY-MM-DD}` | Plan cycle post for review | Each agent owns its own |

## Collaboration Model

- The **creating agent** owns the post and calls `update_post` to modify it.
- Other agents **discover** posts via `search_assets`, **read** via `get_asset`, and **contribute** via `create_comment`.
- The owning agent **consolidates** comments during heartbeat (merges insights from other agents into the main post).

## Local File Fallbacks

When `memory.org_id` / `memory.team_id` are not configured, the system falls back to local workspace files:

| Local Path | Maps To |
|------------|---------|
| `MEMORY.md` | `MEMORY:{agent_name}` |
| `memory/daily/YYYY-MM-DD.md` | `DAILY:{agent_name}:{YYYY-MM-DD}` |
| `SOUL.md` | `SOUL:{agent_name}` |
| `NOTES.md` | `NOTES:{agent_name}` |
| `memory/users/{user_id}.md` | `USER:{user_id}` |
| `memory/entities/{name}.md` | Local only (workspace-specific) |
| `memory/tasks/{slug}.md` | Local only (workspace-specific) |

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

- **MEMORY and today's daily log are auto-loaded** into your context at the start of every run.
- **User model is auto-loaded** when a user_id is known.
- **Conversation state is auto-managed.** It tracks the current topic, goals, decisions, key moments, and a rolling summary.
- **Entity and task files are auto-loaded** when they match conversation key_entities or have in-progress status.
- **Focus on the task, not on memory.** Facts, asset references, and daily log entries are extracted automatically from your actions by the post-run reflection system.
- **Asset references are tracked automatically.** When you create or interact with Ouro assets, the reflection system captures the asset IDs and links them in memory and the daily log.
