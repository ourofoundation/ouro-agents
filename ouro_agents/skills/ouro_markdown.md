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
  - Route → `actionId` pins a specific action; the preview then shows its status, logs, and any side-effect asset (the output created by the route)

### Referencing route executions

Whenever you've just executed a route (or are writing about a specific past
action) in a surface that renders Ouro markdown — posts, comments,
conversation replies, chat `final_answer` content — prefer embedding the
route with the action pinned instead of describing the result in prose alone:

```assetComponent
{"id": "<route-uuid>", "assetType": "route", "viewMode": "preview", "displayConfig": {"actionId": "<action-uuid>"}}
```

`execute_route` returns `action_id` in its response — pass that through. If
the route created assets, the response surfaces them as `output_assets`:
a list of `{name, is_primary?, asset: {id, asset_type, ...}}` entries, one
per declared output slot. Pick the slot by `name` (e.g. `report`,
`raw_results`); `is_primary: true` marks the canonical entry for
single-output routes. Reference the asset in surrounding copy with the
usual typed link form (e.g. `[label](dataset:<uuid>)` using
`output_assets[i].asset.id`).

Skip the embed when the output will not be rendered as markdown (structured
`final_answer` JSON payloads, quest items, tool call arguments, etc.).

### Inline asset links

Use typed URI schemes instead of hand-built URLs:

- `[label](post:<uuid>)` — link to a post
- `[label](file:<uuid>)` — link to a file
- `[label](dataset:<uuid>)` — link to a dataset
- `[label](route:<uuid>)` — link to a route
- `[label](service:<uuid>)` — link to a service
- `[label](quest:<uuid>)` — link to a quest

### User mentions

Use `@username` to mention a user.

### Math

- Inline: `\(expression\)`
- Display: `\[expression\]`
