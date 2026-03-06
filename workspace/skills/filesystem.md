# Local Filesystem Skill

You have read/write access to your local workspace via the `filesystem` MCP server. All paths are relative to `./workspace`.

## Available Operations

- `read_file` — read a file's contents
- `write_file` — create or overwrite a file
- `edit_file` — make targeted edits to an existing file
- `list_directory` — list files and subdirectories
- `create_directory` — create a new directory
- `move_file` — move or rename a file
- `search_files` — search file contents by pattern
- `get_file_info` — file metadata (size, modified time, etc.)

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
1. Write or generate the file locally (e.g., `workspace/data/results.csv`).
2. `load_tool("ouro:create_file")` and pass the absolute local path.
3. The file is uploaded as an asset on Ouro with the org/team you specify.
