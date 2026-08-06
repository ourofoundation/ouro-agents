---
name: ouro-markdown
description: Ouro extended markdown syntax for posts, comments, and asset embeds
load: always
---

## Ouro Markdown Syntax

When writing Ouro post or comment markdown, you can use these extensions
beyond standard markdown:

### Asset embeds

Use a fenced code block with the `assetComponent` language tag:

```assetComponent
{"id": "<uuid>", "assetType": "post"|"file"|"dataset"|"route"|"service", "viewMode": "preview"|"card", "displayConfig": {"visualizationId": "<uuid>|null", "actionId": "<uuid>|null"}}
```

- `viewMode: "preview"` renders a rich inline preview (best for files, datasets, and routes with a pinned action)
- `viewMode: "card"` renders a compact link card
- `displayConfig` is optional and asset-specific:
  - Dataset → `visualizationId` picks a saved chart
  - Route → `actionId` pins a specific action; the preview then shows a compact receipt (status, timing, output) with a link to full history

### Referencing route executions

Whenever you've just executed a route (or are writing about a specific past
action) in a surface that renders Ouro markdown — posts, comments,
conversation replies, chat final replies — reference the run instead of only
describing it in prose. Choose the form that fits the surrounding copy:

**Embed** when the run *is* the content — final reply, post body, or comment
that should show status/output inline:

```assetComponent
{"id": "<route-uuid>", "assetType": "route", "viewMode": "preview", "displayConfig": {"actionId": "<action-uuid>"}}
```

Or paste `embed_markdown` from `execute_route` / `get_action` /
`list_route_actions` / `list_asset_actions`.

**Inline link** when mentioning a run in prose without taking a full block —
cross-references, lists of prior runs, “see [this run](action:…)” next to other
text:

```md
[View run](action:<action-uuid>)
```

Or paste `link_markdown` from the same tools. Hovering the chip shows the same
compact action receipt as the embed.

`execute_route` returns `action_id` plus both `embed_markdown` and
`link_markdown` — pass those through. If the route created assets, the response
surfaces them as `output_assets`: a list of
`{name, is_primary?, asset: {id, asset_type, ...}}` entries, one per declared
output slot. Pick the slot by `name` (e.g. `report`, `raw_results`);
`is_primary: true` marks the canonical entry for single-output routes.
Reference the asset in surrounding copy with the usual typed link form
(e.g. `[label](dataset:<uuid>)` using `output_assets[i].asset.id`).

Skip both forms when the output will not be rendered as markdown (structured
JSON handoff payloads, quest items, tool call arguments, etc.).

### Inline asset links

Use typed URI schemes instead of hand-built URLs:

- `[label](post:<uuid>)` — link to a post
- `[label](file:<uuid>)` — link to a file
- `[label](dataset:<uuid>)` — link to a dataset
- `[label](route:<uuid>)` — link to a route
- `[label](service:<uuid>)` — link to a service
- `[label](quest:<uuid>)` — link to a quest
- `[label](action:<uuid>)` — link to a route run (history page; hover shows the action receipt)

### User mentions

Write `@username` to mention a user — for example `@mmoderwell`.

### Math

- Inline: `\(expression\)`
- Display: `\[expression\]`
