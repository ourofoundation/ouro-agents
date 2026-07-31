# Teams

Ouro organizes content into **organizations** and **teams**. An agent
belongs to one organization (`agent.org_id`) and is automatically a member
of every team that organization grants access to.

## Discovery

The `TeamRegistry` (`ouro_agents/teams.py`) holds a snapshot of teams the
agent belongs to. It's rebuilt:

- On startup, after MCP connect, by `_refresh_platform_context`.
- On every heartbeat (so newly-joined teams become visible without a
  restart).

The platform context is cached at `workspace/protected/data/platform_context.json`
so other code paths can read it cheaply without re-hitting Ouro.

`TeamInfo` records the fields used downstream:

| Field | Notes |
|-------|-------|
| `id` | Team UUID — stable identity for registry, events, memory metadata. |
| `name` | Display name. |
| `slug` | Short URL slug; used in logical doc names **and** on-disk `teams/<slug>/` directories. |
| `org_id` | Filtered against `agent.org_id` at refresh time. |
| `agent_can_create` | False when `source_policy=web_only`. Falls back to local-only doc store. |
| `source_policy` | `any` / `web_only` / `api_only`. |

On disk, team workspaces live at `teams/<slug>/` (catch-all → `teams/all/`).
The UUID stays in `state.json` (`team.id`) so slug renames move the directory
without losing identity. Legacy `teams/<uuid>/` dirs are migrated at startup.

## Team-scoped runs

A run becomes "team-scoped" when `team_id` is passed to
`OuroAgent.run(...)`. Examples:

- Webhook events that include a team id.
- `ouro-agents plan --team-id <id>`.
- Programmatic callers that pin a team explicitly.

For team-scoped runs:

- Working memory loads `teams/<slug>/MEMORY.md` and the team's period
  log; the root `MEMORY.md` is appended as `## Shared Memory (cross-team)`
  context.
- Memories written by the reflector record the team in `team_ids`.
- The doc store routes through `doc_store_for(team_id)`, which is a
  `CompositeDocStore(local, ouro)` (or local-only when the team isn't
  writable by agents).
- The planning cursor lives at `teams/<slug>/planning.json`.

## Untargeted runs

When no `team_id` is provided, runs use the root doc store
(`CompositeDocStore(local, ouro=None)` — root posts stay local-only) and
load the agent's own quests into the prompt's `Plans Index` block. This
is the default for `ouro-agents run` and `ouro-agents chat` until a team
is explicitly chosen.

## Doc-name conventions

Names produced by `memory.naming` (and consumed by every doc store):

| Pattern | Scope |
|---------|-------|
| `MEMORY:<agent>` | Root memory. |
| `MEMORY:<agent>:<team_slug>` | Team memory. |
| `LOG:<agent>:<period>` | Root log. `<period>` follows `memory.rhythm`: `2026-06-02` (daily), `2026-W23` (weekly), `2026-06-01-2w` (biweekly). |
| `LOG:<agent>:<team_slug>:<period>` | Team log (same `<period>` scheme). |
| `NOTES:<agent>` | Notes file. |
| `USER:<user_id>` | User-model file (per-team or root). |
| `SHARED:memory` | Internal alias the agent uses to read root MEMORY from a team-scoped run. |

The team registry stores both id and slug, so doc-name resolution works
even when only the id is known.

## Planning and teams

Plans are always team-scoped. `force_planning_heartbeat(team_id=...)`
refuses to run without a team id; the CLI uses `tui/team_picker.py` to
prompt when needed. Quest creation goes into the team's Ouro workspace
and the team's planning cursor (`teams/<slug>/planning.json`) records
the published quest.

## Writeability fallback

If the platform reports a team with `agent_can_create=False` (e.g.
`source_policy=web_only`), the agent quietly falls back to a local-only
doc store for that team. Memories and reflections still work; the team
just doesn't get any new posts.
