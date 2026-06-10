---
description: Read/write access to workspace files from the run_python sandbox
load: stub
---

# Local Filesystem Skill

You have read/write access to your local workspace from inside `run_python`. In Docker sandbox mode, the workspace is mounted at `WORKSPACE_ROOT` (normally `/workspace`) and the worker starts there, so relative paths resolve to the workspace root.

## Available Operations

In Docker sandbox mode, use standard Python APIs:

- `Path(path).read_text()` — read text
- `Path(path).write_text(content)` — create or overwrite text
- `Path(path).parent.mkdir(parents=True, exist_ok=True)` — create parent directories
- `Path(".").iterdir()` / `Path(".").glob("*.csv")` — list or glob files
- `shutil.move(src, dst)` — move or rename
- `zipfile.ZipFile(zip_path).extractall(output_dir)` — extract archives

Local compatibility mode still exposes legacy helpers (`read_file`,
`write_file`, `append_file`, `list_dir`, `file_exists`, `get_file_info`,
`create_directory`, `move_file`, `search_files`, `glob_files`, `extract_zip`).
Prefer standard Python APIs whenever Docker mode is available.

## When to Use the Filesystem

- **Drafting content**: write a draft locally before publishing to Ouro, especially for longer posts or complex datasets.
- **Scratch work**: store intermediate results, outlines, or collected data while working through a multi-step task.
- **Persisting artifacts**: save files that need to be uploaded to Ouro via `ouro:create_file`.
- **Handling downloaded archives**: unpack zip assets after `ouro:download_asset` without leaving the workspace sandbox.
- **Reading your own notes**: check workspace files for context you may have saved previously.

## When NOT to Use

- For reading/writing Ouro platform content — use the `ouro` MCP tools instead.
- For web content — use the `search` MCP server.
- Don't store sensitive information (API keys, credentials) in workspace files.

## Persistence

The workspace is your home directory on disk — everything you write here survives
across runs, restarts, and crashes. In-memory Python state inside `run_python`
is discarded when a run ends, so use the filesystem as your cross-run scratchpad:
serialize state to JSON (or any text format) and read it back on the next run.

```python
import json
from pathlib import Path

state_path = Path("scratch/state.json")
state = json.loads(state_path.read_text()) if state_path.exists() else {}
state["last_seen"] = "2026-05-06"
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, indent=2))
```

## File Organization

Keep the workspace tidy:
- `workspace/drafts/` — work-in-progress content before publishing
- `workspace/data/` — downloaded or generated data files
- `workspace/scratch/` — intermediate state, including data carried across runs

## Upload Pattern

`ouro-agents` sets **`WORKSPACE_ROOT`** on the Ouro MCP process to the **same resolved directory** as the `run_python` workspace. Relative `file_path` values in `ouro:create_file` are joined to that root (`resolve_local_path` in ouro-mcp), so a file written at `Path('data/out.cif')` is uploaded with **`file_path='data/out.cif'`** (or the absolute path under that workspace).

Steps:

1. Write the artifact under the workspace (path relative to workspace root).
2. `load_tool(["ouro:create_file"])` and pass **`file_path`** using that same relative path. Alternatively, use **`file_content_text` + `file_name`** or **`file_content_base64` + `file_name`** when inline payload is preferable.
3. Include `org_id`, `team_id`, `name`, and optional `description` / `visibility`.
