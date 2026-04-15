---
name: ouro-py
description: Ouro Python SDK (ouro-py) reference and usage patterns for run_python
load: stub
---

## How to use ouro-py

Call `run_python` with code that uses `get_ouro_client()` to get a pre-authenticated
Ouro SDK client. No API key or import needed for the client itself.

```python
ouro = get_ouro_client()
# Now use ouro.posts, ouro.datasets, ouro.files, ouro.assets, etc.
```

## ouro-py API Reference

### Assets (ouro.assets)
- `search(query="", **kwargs)` → list[dict]
  - kwargs: `limit` (default 20), `offset` (default 0), `scope` ("personal"|"org"|"global"|"all")
  - Filter kwargs (packed into `filters`): `asset_type`, `org_id`, `team_id`, `user_id`, `visibility`, `source`, `top_level_only`
  - `metadata_filters`: dict → JSON-encoded query param
  - `with_pagination=True` returns dict with `data` + `pagination`
- `retrieve(id)` → Post | File | Dataset | etc. (dispatches by type)
- `download(id, output_path=None, asset_type=None)` → dict with `id`, `path`, `filename`, `content_type`, `bytes`

### Posts (ouro.posts)
- `list(query="", limit=20, offset=0, scope=None, org_id=None, team_id=None, **kwargs)` → list[Post]
- `create(name, content=None, content_markdown=None, content_path=None, description=None, visibility=None, **kwargs)` → Post
  - `name` is the post title (there is no `title` parameter)
  - Body: use exactly one of `content` (Content object), `content_markdown`, or `content_path` — omit `content` when using markdown or a path
  - Pass `org_id` and `team_id` in kwargs
- `retrieve(id)` → Post
- `update(id, name=None, content=None, description=None, visibility=None, **kwargs)` → Post
- `delete(id)` → None

### Datasets (ouro.datasets)
- `list(query="", limit=20, offset=0, scope=None, org_id=None, team_id=None, **kwargs)` → list[Dataset]
- `create(name, visibility, data=None, description=None, **kwargs)` → Dataset
  - `data`: DataFrame, list[dict], or dict (must have ≥1 row, ≥1 column)
  - Pass `org_id` and `team_id` in kwargs
- `retrieve(id)` → Dataset
- `query(id)` → DataFrame (fetches all rows)
- `load(table_name)` → DataFrame (finds dataset by table_name, then fetches rows)
- `schema(id)` → list[dict] (column definitions)
- `stats(id)` → dict
- `update(id, name=None, data=None, data_mode="append", description=None, **kwargs)` → Dataset
  - `data_mode`: "append" | "overwrite" | "upsert"
- `list_views(id)` → list[dict]
- `create_view(id, name, description=None, sql_query=None, engine_type="auto", config=None, prompt=None)` → dict
- `update_view(id, view_id, ...)` → dict
- `delete_view(id, view_id)` → None
- `delete(id)` → None

### Files (ouro.files)
- `list(query="", limit=20, offset=0, scope=None, org_id=None, team_id=None, **kwargs)` → list[File]
- `create(name, visibility, file_path=None, file_content=None, file_name=None, description=None, **kwargs)` → File
  - Use `file_path` for local files or `file_content` (bytes) + `file_name` for in-memory
  - Pass `org_id` and `team_id` in kwargs
- `retrieve(id)` → File
- `update(id, file_path=None, file_content=None, file_name=None, name=None, description=None, **kwargs)` → File
- `delete(id)` → None
- `share(file_id, user_id, role="read")` → None

### Conversations (ouro.conversations)
- `create(member_user_ids, name=None, summary=None, org_id=None, team_id=None)` → Conversation
- `retrieve(conversation_id)` → Conversation
- `list(org_id=None, limit=20, offset=0)` → list[Conversation]
- `update(conversation_id, **kwargs)` → Conversation
- Messaging (after retrieve/create): `conversation.messages.create(text=..., ...)` and `conversation.messages.list(**kwargs)`

### Organizations (ouro.organizations)
- `list()` → list[dict]
- `retrieve(id)` → dict
- `get_context()` → dict

### Teams (ouro.teams)
- `list(org_id=None, joined=None, public_only=None)` → list[dict]
- `retrieve(id)` → dict
- `create(name, org_id, description=None, visibility=None, ...)` → dict
- `update(id, name=None, description=None, visibility=None, ...)` → dict
- `join(id)` → dict
- `leave(id)` → dict
- `activity(id, offset=0, limit=20, asset_type=None)` → dict
- `unreads(id, org_id=None)` → int

### Comments (ouro.comments)
- Use for adding comments to assets

### Users (ouro.users)
- User lookup and profile access

## Important notes
- All resource methods use `retrieve(id)`, not `get(id)`
- `datasets.query(id)` returns a pandas DataFrame — you can use standard pandas
  operations on it (the sandbox allows pandas through the SDK)
- For creating assets, always pass `org_id` and `team_id` from the Platform context
- `description` params accept a plain string or a Content object
- When creating datasets, `data` must be non-empty (at least 1 row, 1 column)
- Workspace file helpers (`read_file`, `write_file`, etc.) are available alongside the SDK

## Strategy
1. Use `run_python` for ALL Ouro interactions — write code that does the full workflow
2. Start by getting the client: `ouro = get_ouro_client()`
3. Chain multiple operations in a single `run_python` call when possible
4. Use print() to show intermediate results and progress
5. Handle errors with try/except and provide clear error messages
6. For large data operations, process in batches
