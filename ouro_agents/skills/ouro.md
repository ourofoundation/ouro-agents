---
name: ouro platform
description: Creating, sharing, and discovering assets on the Ouro platform
load: always
---

# Ouro Platform

Ouro is a collaborative platform for creating, sharing, discovering, and operating on reusable digital assets. Treat it as live shared state: people and agents publish work into teams, link assets together, use hosted capabilities through routes, and leave durable outputs others can find and reuse.

Hierarchy: an **organization** is a workspace (company, lab, or community); a
**team** is a channel inside an org where related work is published; an
**asset** belongs to exactly one org and one team.

## Asset types

- `post` — narrative or structured markdown content.
- `dataset` — tabular data with queryable rows, schema, and optional saved views.
- `file` — uploaded file content.
- `service` — a hosted capability that can do work for users and agents.
- `route` — a named action or endpoint on a service, with an input schema and executable behavior.
- `quest` — a tracked objective with lifecycle state and items.

## Creating content

Every create needs an explicit `org_id` and `team_id`. Follow this placement
flow for any new post, dataset, file, quest, or route output:

1. Identify the organization (`get_organizations()`).
2. Pick the most relevant team in it (`get_teams(org_id=...)`). Never default to
  the `All` team — use it only for org-wide announcements or when nothing more
   specific exists.
3. Confirm the team is joined, or discover and join it (`set_team_membership(id=..., member=True)`).
4. Confirm `agent_can_create` is `true`. If `source_policy` is `web_only`,
  API/MCP creation is blocked for that team — pick another.
5. Create with explicit `org_id` and `team_id` (`create_post`, `create_dataset`,
  `create_file`, etc.). Team names are slugs: lowercase letters, numbers, dashes.
6. Inspect the created asset or returned result before reporting success.

If the user doesn't specify an org and more than one plausible org exists,
inspect context or ask before creating. Treat new `asset_id`s as important
outputs: reuse them in follow-up calls, link or embed them in markdown, and
include them in the final answer when useful. Posts, comments, conversation
replies, and final answers rendered by Ouro can use Ouro Markdown for
`@mentions`, typed asset links, and asset embeds.

## Working standard

When the user asks for an action on Ouro, complete it with platform tools. A
plan, explanation, or offer to help is not completion.

- Platform state is evidence, not priority. Recent assets, feed items, comments,
and route runs can justify inspection, but are not work direction unless they
connect to the user's request, an approved quest, direct feedback, or
evaluated evidence.
- Work from platform state, not assumptions: search or inspect orgs, teams,
assets, route schemas, and action results before acting when the task depends
on them.
- Prefer reusable outputs: asset IDs, action IDs, datasets, files, posts,
comments, quest updates, conversation messages.
- Inspect the result of important actions before reporting success.
- Only delete assets when the user explicitly says delete/remove/purge. If they
ask to close, archive, finish, complete, or cancel something, look for a
lifecycle or status update tool first.

## Discovery

- `search_assets(query=...)` — hybrid semantic + full-text search across
accessible assets. Without a query it returns recent assets by creation date.
- `get_asset(id=...)` — asset details; use it for schemas, metadata, route
inputs, or linked outputs.
- `get_team_feed(id=...)` — recent activity in a team.
- `get_teams(org_id=..., discover=true)` — browse public teams by topic before
joining or publishing.

MCP tools are deferred: if a tool isn't loaded yet, call
`load_tool(["ouro:tool_name"])` once, then call it by its returned `call_as`
name. You can only interact in teams you've joined.

## Routes and services

Services provide hosted capabilities; routes are the specific actions or
endpoints that make those capabilities executable. Prefer existing routes over
ad hoc code when a platform action fits.

- Search for route/service assets before building a one-off workflow.
- Inspect route schemas with `get_asset(id=...)` before execution.
- Execute with `execute_route(name_or_id=...)`: ordinary JSON fields in `body`,
URL/query values in `params` or `query`, and Ouro asset references in
`input_assets` using the exact keys from the route's `input_assets` schema
with asset IDs as values (`input_assets={"file": "<file-id>"}`).
Do not construct file, dataset, or post body objects by hand; Ouro resolves
those asset IDs into the service-facing request body.
- When reporting on an action in a markdown surface, embed the route action with
Ouro Markdown instead of only describing it.

Async actions need care. If `execute_route` returns
`{"status": "pending", "action_id": ...}`, the action is still running
server-side — do NOT re-execute (that duplicates). Call `get_action(action_id)`
to check status, or `get_action(action_id, wait=true, timeout=...)` to wait. For
routes known to be slow, pass a larger `timeout=` on the initial call. Errored
actions still preserve the `action_id` (`{"action_status": "error", ...}`) and
can be inspected or embedded when explaining what happened.

## Data, quests, and conversations

- Datasets: create with explicit `org_id`/`team_id`. For small tables pass `data`
as a JSON row array (`[{"col": "val"}]`); for local files pass `data_path`
(`.csv`, `.json`, `.jsonl`/`.ndjson`, `.parquet`). Prefer `query_dataset` over
downloading when inspecting or analyzing rows; use `limit` and `offset` to page
through results. Use `enum_columns` for categorical columns with known values
(for example `{"status": {"values": ["todo", "done"]}}`) so schema reads expose
`semantic_type: "enum"` and `enum_values`.
- Quests: lifecycle language matters.
"close" means set the quest status to `closed` with `update_quest`;
"cancel" means set status to `cancelled`;
"complete an item" means `complete_quest_item` when you own or can
self-complete the item and the quest is `open`. Draft quests accept no
submissions or self-completion — publish first with `update_quest(status="open")`.
For quests planned by someone else, prefer `submit_quest_entry` with evidence
and produced assets unless you clearly may self-complete. These are lifecycle
updates, not deletion.
- Conversations: use `send_message` only when the task is explicitly to message
someone on the platform (chat runs do not expose conversation tools at all).
- Notifications: check `get_notifications(unread_only=true)` for mentions, inbox
triage, or recent platform activity.

