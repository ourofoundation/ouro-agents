---
description: Sandboxed Python execution for calculations, data transforms, and file processing
load: stub
---

# Python Execution Skill

You have a `run_python` tool that executes code in a sandboxed Python environment.

## When to Use

- **Calculations**: math, statistics, unit conversions, date arithmetic.
- **Data transformation**: parsing JSON/CSV, reshaping data, filtering, aggregating.
- **Text processing**: regex extraction, formatting, template rendering.
- **Multi-step logic**: anything that would take several tool calls but is trivial in a few lines of code.
- **Preparing content**: building markdown tables, formatting data for an Ouro post.

## When NOT to Use

- Simple factual answers you already know — just answer directly.
- Fetching web content — use the `search` MCP tools instead.
- One-off Ouro lookups or single asset reads/writes — use the `ouro` MCP tools
  (faster than spinning up sandbox code for a single call).

## When to Prefer `run_python` + ouro-py

For **bulk / multi-step platform work** (paginate hundreds of files, walk
connections, build datasets, batch downloads), use `run_python` with
`get_ouro_client()` — see the **ouro-py** skill. Do not paginate thousands of
assets via MCP tool calls; write a script that checkpoints to the workspace.

## Usage Notes

- State persists across calls within a single run. Define a variable in one call, use it in the next.
- In-memory state does NOT survive past the end of a run. To carry data forward, write it to the workspace (e.g. `scratch/state.json`) — see the **filesystem** skill.
- Print statements are captured — use `print()` to inspect intermediate values.
- In Docker sandbox mode, code runs inside a container with the workspace at `WORKSPACE_ROOT` (normally `/workspace`). You can use normal Python APIs like `pathlib`, `open`, `zipfile`, installed packages, and `subprocess.run(...)`.
- In local compatibility mode, imports are restricted and legacy workspace helpers may be required.

## Workspace File Helpers

In Docker sandbox mode, prefer standard Python file APIs (`Path.read_text()`,
`Path.write_text()`, `Path.glob()`, `zipfile.ZipFile`, etc.). Legacy helpers
(`read_file`, `write_file`, `append_file`, `list_dir`, etc.) are only for local
compatibility mode. See the **filesystem** skill for workspace conventions.

## Patterns

**Quick calculation:**
```python
from statistics import mean, stdev
values = [23.1, 24.5, 22.8, 25.0, 23.7]
print(f"Mean: {mean(values):.2f}, StdDev: {stdev(values):.2f}")
```

**Read, transform, and write a file:**
```python
import csv, json
from pathlib import Path

with Path("data/measurements.csv").open() as f:
    reader = csv.DictReader(f)
    rows = [r for r in reader if float(r["value"]) > 100]

Path("data/filtered.json").write_text(json.dumps(rows, indent=2))
print(f"Filtered {len(rows)} rows")
```

**Transform data for a post:**
```python
import json
raw = json.loads(dataset_result)
rows = sorted(raw["rows"], key=lambda r: r["score"], reverse=True)[:10]
table = "| Rank | Name | Score |\n|------|------|-------|\n"
for i, r in enumerate(rows, 1):
    table += f"| {i} | {r['name']} | {r['score']:.1f} |\n"
print(table)
```
