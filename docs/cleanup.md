# Cleanup (asset.deleted)

When an asset is deleted on Ouro, the platform fires an `asset.deleted`
webhook. The agent handles this **deterministically** (no LLM) inside the
webhook handler:

1. **mem0 prune** — hard-delete every vector memory whose `asset_ids`
   metadata contains the deleted asset id.
2. **Workspace sweep** — rewrite markdown / JSON files that reference
   the asset id, marking the references as `[deleted]` or removing them
   from arrays.
3. **Plan archival** — plan cycles that referenced the deleted asset are
   moved from `active/` to `history/` if no items remain.

Implementation: `ouro_agents/cleanup/asset_deleted.py`.

## Why deterministic?

UUIDs are unambiguous. Cleanup is a known, repeatable transformation —
delegating to an LLM would add cost, latency, and the risk of hallucinated
edits. The system is built so this path runs synchronously inside the
event handler before any agent loop spins up.

The `asset.deleted` event is intentionally **not** routed through the
[refinement](./refinement.md) change-set queue. Asset deletion is
deterministic; refinement is for interpretive corrections.

## Discovery

`discover_files_with_asset(asset_id, workspace)`:

- Tries `ripgrep` first (fast, fixed-string match, ignores
  `_SWEEP_EXCLUDE_DIRS`: `data`, `chroma`, `memory`, `__pycache__`,
  `cifs`, `debug-runs`, `conversations`, etc.).
- Falls back to a pure-Python walk over `.md`, `.json`, `.jsonl`,
  `.txt` files when ripgrep isn't on `$PATH`.

## Markdown rewrite policy

For each affected file, the cleanup runs the following replacements
in order, tracking `edits_per_file`:

1. **`assetComponent` fenced blocks** whose JSON `id` matches the
   deleted asset → replaced with `> [deleted asset]`.
2. **Typed markdown links** like `[label](post:UUID)` /
   `[label](dataset:UUID)` / `[label](file:UUID)` /
   `[label](service:UUID)` / `[label](route:UUID)` /
   `[label](asset:UUID)` → rewritten to `label [deleted]` (or
   `[deleted]` when there's no label).
3. **Bare UUIDs** anywhere else → suffixed with ` [deleted]`.

After any edit, the file's frontmatter `last_updated` is bumped so the
workspace sync pushes the change to Ouro.

## JSON rewrites

JSON / JSONL files have asset-id-shaped strings either replaced with
`null` (object values) or removed (array entries). Plan JSON gets
special handling: items referencing only the deleted asset are dropped
from `items[]`, and an entire active plan is archived to `history/` if
its remaining items are empty.

## SweepResult

The handler returns a `SweepResult` summarizing what changed:

```python
SweepResult(
    asset_id=...,
    files_inspected=[...],
    files_rewritten=[...],
    edits_per_file={...},
    plans_archived=[...],
    mem0_deleted=N,
    errors=[...],
)
```

Errors are captured per-file and logged but never propagated — a single
broken file should never block the rest of the cleanup.

## Manual cleanup

`scripts/clean_deleted_assets.py` runs the same code path against an
explicit asset id (or list of ids) for offline backfills:

```bash
python scripts/clean_deleted_assets.py --config config.json \
    --asset-ids 11111111-... 22222222-...
```

This is what to use after a bulk-delete operation that didn't replay
webhooks to the agent.
