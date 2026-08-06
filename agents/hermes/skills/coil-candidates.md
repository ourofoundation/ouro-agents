---
description: Repeated tool-call sequences that are candidates for coils
load: stub
---

# Coil candidates

These tool-call sequences showed up across multiple successful runs.
Consider authoring a coil for any that you still do frequently —
load the `coils` skill for the contract and templates.

## search_assets -> get_asset -> get_asset

- **Annotated:** `search_assets (ouro.assets.search) -> get_asset (ouro.assets.retrieve) -> get_asset (ouro.assets.retrieve)`
- **Runs:** 5
- **First seen:** 2026-08-03T21:00:09.392144+00:00
- **Last seen:** 2026-08-04T02:00:08.241121+00:00
- **Example args (truncated):**
```json
[
  {
    "query": "",
    "asset_type": "post",
    "scope": "global",
    "org_id": "",
    "team_id": "",
    "user_id": "",
    "visibility": "public",
    "file_type": "",
    "extension": "",
    "metadata_filters": "",
    "sort": "recent",
    "time_window": "",
    "limit": 12,
    "offset": 0
  },
  {
    "id": "019fb874-a25e-7723-a243-11050379cdf2",
    "detail": "full"
  },
  {
    "id": "019fc2a6-2ef7-7ffa-8cd1-9254191640aa",
    "detail": "full"
  }
]
```

Suggested `mined_from` for coil.json: `["search_assets", "get_asset", "get_asset"]`

## get_asset -> get_asset -> search_assets

- **Annotated:** `get_asset (ouro.assets.retrieve) -> get_asset (ouro.assets.retrieve) -> search_assets (ouro.assets.search)`
- **Runs:** 3
- **First seen:** 2026-08-03T19:22:31.891421+00:00
- **Last seen:** 2026-08-04T01:00:07.514009+00:00
- **Example args (truncated):**
```json
[
  {
    "id": "019fc2a6-2ef7-7ffa-8cd1-9254191640aa",
    "detail": "full"
  },
  {
    "id": "019fb874-a25e-7723-a243-11050379cdf2",
    "detail": "full"
  },
  {
    "query": "",
    "asset_type": "post",
    "scope": "personal",
    "org_id": "",
    "team_id": "",
    "user_id": "9a2b1188-0442-4843-b495-df7cf75b34d3",
    "visibility": "",
    "file_type": "",
    "extension": "",
    "metadata_filters": "",
    "sort": "recent",
    "time_window": "",
    "limit": 20,
    "offset": 0
  }
]
```

Suggested `mined_from` for coil.json: `["get_asset", "get_asset", "search_assets"]`
