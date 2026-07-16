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
- `share(id, user_id, role="read")` → None — grant read/write/admin on any asset type
  - Private assets stay invisible until shared; mentions/links do not grant access
  - Prefer this over `files.share` (which delegates here)

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
  - `data`: DataFrame or list[dict] (must have ≥1 row, ≥1 column)
  - Pass `org_id` and `team_id` in kwargs
- `retrieve(id)` → Dataset
- `query(id)` → DataFrame (fetches all rows)
- `query(id, sql)` → DataFrame (read-only SQL; use `{{table}}` as placeholder)
- `schema(id)` → list[dict] (column definitions)
- `stats(id)` → dict
- `update(id, name=None, data=None, data_mode="append", description=None, **kwargs)` → Dataset
  - `data_mode`: "append" | "overwrite" | "upsert"
- `list_views(id)` → list[dict]
- `create_view(id, name, description=None, sql_query=None, config=None, prompt=None)` → dict
- `update_view(id, view_id, ...)` → dict
- `delete_view(id, view_id)` → None
- `delete(id)` → None

### Files (ouro.files)
- `search(query="", **kwargs)` → list[File] | dict
  - Always scopes to `asset_type="file"`
  - First-class file filters: `extension` (e.g. `"cif"` or `["cif", "xyz"]`),
    `file_type` (`"image"` | `"video"` | `"audio"` | `"pdf"`)
  - Other kwargs: `limit`, `offset`, `scope`, `org_id`, `team_id`, `user_id`,
    `visibility`, `sort`, `time_window`, `metadata_filters`
  - `with_pagination=True` returns `{"data": list[File], "pagination": ...}`
  - Pagination is transparent: `limit=None` fetches **all** matches, `limit>200`
    paginates internally. Bulk discovery is one call:
    `cifs = ouro.files.search(extension="cif", scope="all", limit=None)`
  - Leave `query` empty for exhaustive collection (semantic search caps results)
- `list(query="", limit=20, offset=0, scope=None, org_id=None, team_id=None, **kwargs)` → list[File]
  - Thin wrapper around `search` (also accepts `extension` / `file_type`)
- `create(name, visibility, file_path=None, file_content=None, file_name=None, description=None, **kwargs)` → File
  - Use `file_path` for local files or `file_content` (bytes) + `file_name` for in-memory
  - Pass `org_id` and `team_id` in kwargs
- `retrieve(id)` → File
- `update(id, file_path=None, file_content=None, file_name=None, name=None, description=None, **kwargs)` → File
- `delete(id)` → None
- `share(file_id, user_id, role="read")` → None — delegates to `ouro.assets.share`

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
  operations on it when pandas is installed in the configured sandbox
- For creating assets, always pass `org_id` and `team_id` from the Platform context
- `description` params accept a plain string or a Content object
- When creating datasets, `data` must be non-empty (at least 1 row, 1 column)
- In Docker sandbox mode, use normal Python filesystem APIs under `WORKSPACE_ROOT`;
  local compatibility mode may still require workspace helpers (`read_file`,
  `write_file`, etc.)

## Strategy
1. Prefer `run_python` + ouro-py for **bulk / multi-step** Ouro work (paginate
   files, walk connections, build datasets). One-off reads/writes can stay on MCP.
2. Start by getting the client: `ouro = get_ouro_client()`
3. Chain related operations in one `run_python` call when they fit under the
   sandbox timeout; for large jobs, **paginate and checkpoint to workspace files**
   across multiple calls (e.g. write `scratch/batch_offset.json` + progress JSON).
4. On timeout the Docker worker resets — next `run_python` starts fresh. Workspace
   files persist; in-memory variables do not. Resume from your checkpoint.
5. Use print() to show intermediate results and progress
6. Handle errors with try/except and provide clear error messages
7. Collect files with `ouro.files.search(extension="cif", scope="all", limit=None)` —
   it paginates internally. Use explicit `offset` + `with_pagination=True` only
   for checkpointed resumption across sandbox resets. There is no need to use
   `assets.search` + local `.cif` filtering.
8. Pass filter kwargs directly (`extension=`, `team_id=`, `scope=`), not as a
   nested `filters=` or `params=` dict
