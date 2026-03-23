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

These are sandboxed — path traversal outside the workspace is blocked.

## When to Use the Filesystem

- **Drafting content**: write a draft locally before publishing to Ouro, especially for longer posts or complex datasets.
- **Scratch work**: store intermediate results, outlines, or collected data while working through a multi-step task.
- **Persisting artifacts**: save files that need to be uploaded to Ouro via `ouro:create_file`.
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

To share a local file on Ouro:
1. Write or generate the file locally (e.g., `data/results.csv`) using `write_file`.
2. `load_tool("ouro:create_file")` and pass the absolute local path.
3. The file is uploaded as an asset on Ouro with the org/team you specify.
