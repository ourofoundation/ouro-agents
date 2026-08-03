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
7. If the asset is **private** and a controller (or named collaborator) must
  review it, call `share_asset` with their `user_id` from PLATFORM CONTEXT
  (role `read` unless they need write/admin). Mentions, links, and embeds do
  **not** grant access — private assets stay invisible until shared.

### Create results

Create tools return a JSON object. Read it the same way you read async route
results:

- **Success** = an `id` field (often with `url` / `name`). That `id` is the
  asset id — reuse it in follow-up calls, links, embeds, and the final answer.
- **Failure** = an `error` field (often with `message` / `retryable`).
- After a successful create, continue from that `id` (`update_*`, link/embed,
  finish). Do **not** create another asset for the same deliverable.
- The platform may uniquify colliding names (e.g. append ` 1`). That is still
  success — trust `id`, not an exact name match.
- Retry only when `retryable` is true or you have a concrete argument fix.
  Do not recreate because a success payload "looked incomplete."

If the user doesn't specify an org and more than one plausible org exists,
inspect context or ask before creating. Posts, comments, conversation replies,
and final answers rendered by Ouro can use Ouro Markdown for `@mentions`, typed
asset links, and asset embeds.

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
Returns slim rows (id, name, asset_type, description, username, created_at,
optional snippet).
- `get_asset(id=...)` — asset details; summary is compact (flat username/org_id/
team_id). Use `detail="full"` for schemas, bodies, download URLs; creation
producer is a compact `creation_action` pointer — use `list_asset_actions` for
the full run.
- `get_team_feed(id=...)` — recent activity in a team (same slim row shape as
search).
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
routes known to be slow, pass a larger `timeout=` on the initial call. Default
`get_action` is compact (status + output asset ids, no response body); pass
`include_response=true` when you need `data`/`error` payloads. Prefer following
output asset ids with `get_asset` / download when the payload is large. Errored
actions still preserve the `action_id` (`{"action_status": "error", ...}`) and
can be inspected or embedded when explaining what happened.
Use `list_asset_actions(asset_id)` to find runs that produced an asset
(`created_by`) or used it as input (`as_input`) — do not scrape posts for
action IDs. Leave `include_response` false for compact browsing; set it true
when you need calculated properties from `action.response`. Connection graphs
may include `action_id` on `action` edges for follow-up with `get_action`.

## Data, quests, and conversations

- Datasets: create with explicit `org_id`/`team_id`. For small tables pass `data`
as a JSON row array (`[{"col": "val"}]`); for local files pass `data_path`
(`.csv`, `.json`, `.jsonl`/`.ndjson`, `.parquet`). Prefer `query_dataset` over
downloading when inspecting or analyzing rows; use `limit` and `offset` to page
through results. Responses are compact markdown tables by default (set
`response_format="json"` for JSON).
  - **Reference columns**: pass `refs` on `create_dataset` (or `update_dataset`
    to promote) so columns hold Ouro object ids with a real FK — e.g.
    `{"file_id": {"kind": "asset", "asset_type": "file"}, "run_id": {"kind": "action"}}`.
    Schema shows `semantic_type: "reference"` with `ref_kind` (+ optional
    `asset_type`). Column fields also include `name`/`type` aliases alongside
    `column_name`/`data_type`. Resolve names/URLs with
    `query_dataset(..., resolve_refs=true)` (returns a `resolved_refs` sidecar).
  - **Enum columns**: pass `enum_columns` for categorical columns with known
    values (e.g. `{"status": {"values": ["todo", "done"]}}`) so schema reads
    expose `semantic_type: "enum"` and `enum_values`.
  - Structural column changes after creation use `edit_dataset_columns` (add /
    update / rename / drop); pass `enum_values` on add/update for categoricals.
    `update_dataset` stays for row ingest and whole-dataset metadata.
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

