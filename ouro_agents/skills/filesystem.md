---
description: Read/write access to workspace files via the run_python sandbox helpers
load: stub
---

# Local Filesystem Skill

You have read/write access to your local workspace via built-in helpers inside `run_python`. All paths are relative to the workspace root.

## Available Operations

Inside any `run_python` call (no import needed):

- `read_file(path)` — read a file's contents as a string
- `write_file(path, content)` — create or overwrite a file (creates parent dirs)
- `append_file(path, content)` — append to a file (creates it if needed)
- `list_dir(path='.')` — list files and subdirectories (dirs have trailing `/`)
- `file_exists(path)` — check if a file or directory exists
- `get_file_info(path)` — file metadata dict (size_bytes, modified, is_dir, is_file)
- `create_directory(path)` — create a directory and parents
- `move_file(src, dst)` — move or rename a file within the workspace
- `search_files(pattern, path='.')` — find files whose content contains a substring
- `glob_files(pattern, path='.')` — find files matching a glob pattern (e.g. `*.csv`, `**/*.json`)
- `extract_zip(zip_path, output_dir=None)` — safely extract a zip archive inside the workspace

These are sandboxed — path traversal outside the workspace is blocked.

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

## File Organization

Keep the workspace tidy:
- `workspace/drafts/` — work-in-progress content before publishing
- `workspace/data/` — downloaded or generated data files
- `workspace/scratch/` — temporary files for intermediate work

## Upload Pattern

`ouro-agents` sets **`WORKSPACE_ROOT`** on the Ouro MCP process to the **same resolved directory** as the `run_python` workspace. Relative `file_path` values in `ouro:create_file` are joined to that root (`resolve_local_path` in ouro-mcp), so a file written with `write_file('data/out.cif', ...)` is uploaded with **`file_path='data/out.cif'`** (or the absolute path under that workspace).

Steps:

1. Write the artifact with `write_file` (path relative to workspace root).
2. `load_tool("ouro:create_file")` and pass **`file_path`** using that same relative path. Alternatively, use **`file_content_text` + `file_name`** or **`file_content_base64` + `file_name`** when inline payload is preferable.
3. Include `org_id`, `team_id`, `name`, and optional `description` / `visibility`.
