---
description: Repeated tool-call sequences that are candidates for coils
load: stub
---

# Coil candidates

These tool-call sequences showed up across multiple successful runs.
Consider authoring a coil for any that you still do frequently —
load the `coils` skill for the contract and templates.

## list_messages -> get_asset -> get_asset

- **Annotated:** `list_messages (ouro.conversations.list_messages) -> get_asset (ouro.assets.retrieve) -> get_asset (ouro.assets.retrieve)`
- **Runs:** 3
- **First seen:** 2026-08-03T18:00:05.915248+00:00
- **Last seen:** 2026-08-03T20:00:06.731748+00:00
- **Example args (truncated):**
```json
[
  {
    "conversation_id": "019fc85d-6438-73b3-8433-561c1632ad04",
    "limit": 50,
    "before": "2026-08-03T17:16:20.677263+00:00"
  },
  {
    "id": "019fbda0-dd02-7766-9f0f-6b781837f0b7",
    "detail": "full"
  },
  {
    "id": "019f62e0-a53e-71aa-90fe-52a741ec1387",
    "detail": "full"
  }
]
```

Suggested `mined_from` for coil.json: `["list_messages", "get_asset", "get_asset"]`
