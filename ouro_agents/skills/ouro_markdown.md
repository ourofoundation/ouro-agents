---
name: ouro-markdown
description: Ouro extended markdown syntax for posts, comments, and asset embeds
load: stub
---

## Ouro Markdown Syntax

When writing Ouro post or comment markdown, you can use these extensions
beyond standard markdown:

### Asset embeds

Use a fenced code block with the `assetComponent` language tag:

```assetComponent
{"id": "<uuid>", "assetType": "post"|"file"|"dataset"|"route"|"service", "viewMode": "preview"|"card"}
```

- `viewMode: "preview"` renders a rich inline preview (best for files/datasets)
- `viewMode: "card"` renders a compact link card

### Inline asset links

Use typed URI schemes instead of hand-built URLs:

- `[label](asset:<uuid>)` — generic asset link (auto-resolves type)
- `[label](post:<uuid>)` — link to a post
- `[label](file:<uuid>)` — link to a file
- `[label](dataset:<uuid>)` — link to a dataset
- `[label](route:<uuid>)` — link to a route
- `[label](service:<uuid>)` — link to a service

Never construct URLs manually with placeholders like `/posts/entity/...`.
Use the exact `url` from tool results when available.

### User mentions

Use `@username` to mention a user.

### Math

- Inline: `\(expression\)`
- Display: `\[expression\]`
