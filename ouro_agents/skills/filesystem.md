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

**Never write new files at the workspace root** — it is reserved for framework
files (SOUL.md, NOTES.md, MEMORY.md, HEARTBEAT.md). **Never write under
`protected/`** — that tree is framework-managed (platform cache, scheduled
tasks, run log, mem0/Chroma) and is mounted read-only in Docker. Legacy
top-level `data/` and `memory/` are also refused.

Put your own files here:

- `projects/<slug>/` — all artifacts for a project or ongoing work cycle
  (analyses, results, generated files, post drafts). One directory per effort,
  reused across runs. Don't invent a new top-level directory per run or cycle;
  use a subdirectory of the project (e.g. `projects/novomag/cycle24/`).
- `drafts/` — outgoing drafts not tied to a project (emails, follow-ups, posts).
- `scratch/` — disposable intermediates and cross-run state. Safe to delete.
- `cifs/` — optional structure library; otherwise keep CIFs under the project.
- `skills/` — your domain playbooks (markdown with YAML frontmatter). For
  skills **you** authored, edit in place when you adopt a new coil so
  heartbeats prefer `run_coil` (see the `coils` skill). Maintain
  `skills/coil-candidates.md` yourself when you notice a repeatable job;
  nothing auto-writes it. For **human-authored**
  operational skills (e.g. `outreach`), do **not** edit or overwrite them —
  write `skills/<name>-addendum.md` with `extends: <name>` in the
  frontmatter instead. The addendum loads whenever the parent loads, and
  the parent wins on conflict. Workspace skills of the same name otherwise
  override built-ins.

Period logs: `teams/<team_id>/logs/<period>.md` (not under `memory/`).

Overwrite working files in place instead of creating `_v2`/`_fixed`/`_final`
copies, and reuse one canonical filename per recurring artifact.

## Upload Pattern

`ouro-agents` sets **`WORKSPACE_ROOT`** on MCP child processes to the host
workspace directory (the same tree bind-mounted into Docker). Before every
MCP call, sandbox paths are rewritten onto that root: relative `file_path` /
`filePath` values, and absolute paths under the container mount (normally
`/workspace/...`). That is why `ouro:create_file` and Resend `send_email`
attachments can read a file you just wrote in `run_python`.

Prefer **relative** paths from the workspace root
(`file_path='scratch/out.cif'`). Container absolute paths
(`/workspace/scratch/out.cif`) also work. Alternatively, use
**`file_content_text` + `file_name`** or **`file_content_base64` + `file_name`**
when inline payload is preferable.

Steps:

1. Write the artifact under the workspace (path relative to workspace root).
2. `load_tool(["ouro:create_file"])` and pass **`file_path`** using that same relative path.
3. Include `org_id`, `team_id`, `name`, and optional `description` / `visibility`.
