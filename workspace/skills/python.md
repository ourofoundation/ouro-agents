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
- Reading/writing files — use the workspace file helpers (see the **filesystem** skill).
- Fetching web content — use the `search` MCP tools instead.
- Interacting with Ouro — use the `ouro` MCP tools instead.

## Usage Notes

- State persists across calls within a single run. Define a variable in one call, use it in the next.
- Print statements are captured — use `print()` to inspect intermediate values.
- Authorized imports: `json`, `math`, `statistics`, `datetime`, `re`, `collections`, `itertools`, `functools`, `csv`, `io`, `textwrap`, `hashlib`, `base64`, `urllib.parse`.
- No network access, no `os`/`subprocess` from within code. Use your other tools for those.

## Workspace File Helpers

Built-in file helpers (`read_file`, `write_file`, `append_file`, `list_dir`, etc.) are available inside `run_python` with no import needed. See the **filesystem** skill for the full reference and workspace conventions.

## Patterns

**Quick calculation:**
```python
from statistics import mean, stdev
values = [23.1, 24.5, 22.8, 25.0, 23.7]
print(f"Mean: {mean(values):.2f}, StdDev: {stdev(values):.2f}")
```

**Read, transform, and write a file:**
```python
import csv, json, io
raw = read_file("data/measurements.csv")
reader = csv.DictReader(io.StringIO(raw))
rows = [r for r in reader if float(r["value"]) > 100]
write_file("data/filtered.json", json.dumps(rows, indent=2))
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
