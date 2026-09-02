# Refinement

The refinement subsystem drains a typed **change-set queue** and uses a
cheap LLM to revise affected workspace docs in place. It runs as the
initial maintenance phase of dream (see [Dream mode](./dream.md)), so
corrections land before the evidence-driven review touches the same content.

Implementation: `ouro_agents/refinement/queue.py` (the queue) and
`ouro_agents/refinement/runner.py` (the LLM-driven runner).

Asset deletion is **not** part of this system — it is deterministic and
lives in `ouro_agents/cleanup/`. See [Cleanup](./cleanup.md).

Reflection's `friction.jsonl` is also separate: it records process evidence
for dream review rather than requested document rewrites.

## Why a queue?

The agent learns things continuously, but rewriting working memory after
every signal would be wasteful and noisy. Producers (the reflector,
webhook handlers, manual scripts) append `ChangeEntry` rows describing
something that happened. The runner drains a batch, locates affected
docs, and asks the model to revise them coherently in one pass.

## ChangeEntry shape

```python
class ChangeKind(str, Enum):
    CORRECTION = "correction"          # user told the agent it was wrong
    GUIDANCE_UPDATED = "guidance_updated"  # SOUL.md / direction memory shifted
    ASSET_UPDATED = "asset_updated"    # an Ouro asset the agent referenced changed

class ChangeEntry:
    id: str
    kind: ChangeKind
    subject_id: str         # asset id, memory id, etc. — searched verbatim in docs
    subject_type: str
    team_id: str | None
    org_id: str | None
    actor_user_id: str | None
    occurred_at: str
    payload: dict           # free-form extra context for the LLM
    applied_at: str | None
    applied_summary: str
```

## Persistence

The queue is a JSONL file in the workspace, written atomically (tmp +
rename) on every change. Entries with `applied_at != None` stay in the
file as an audit trail; the runner skips them on each pass.

Dedupe key: `(kind, subject_id)`. Producers can safely re-enqueue the
same entry; only one will be applied per pass.

## The refinement pass

`run_refinement(agent)` is the orchestrator. Each pass:

1. **Drain** up to `refinement.max_changes_per_pass` pending entries.
2. **Find affected docs**: ripgrep (or a Python fallback) over the
   workspace for files that contain any of the entries' `subject_id`s,
   excluding directories like `protected/`, `chroma/`, `memory/`, `cifs/`,
   `debug-runs/`, `conversations/`, etc. Caps at `max_docs_per_pass`.
3. **Build a scoped DocView** for each affected doc:
   - Verbatim YAML frontmatter.
   - A heading TOC.
   - `±window_lines` of context around each subject match.
   - The list of related `ChangeEntry`s.
4. **Call the refiner LLM** with the doc view + a SOUL excerpt. The
   model returns:
   - `windows`: anchored window replacements (line range → new text).
   - `memory_deletes`: vector memory ids to hard-delete.
   - A short summary string for the audit trail.
5. **Apply** windows in reverse line order (so earlier edits don't
   shift later anchors). Bumps `last_updated` in frontmatter so the
   workspace sync pushes the change to Ouro.
6. **Mark applied**: rewrite the queue with `applied_at` and
   `applied_summary` set on every drained entry.

## Why scoped views?

The full body of a doc is never sent to the LLM — only frontmatter, TOC,
and context windows around matches. This keeps refinement cheap and safe:
the model can only edit text adjacent to a known subject, never write
free-form replacements for the whole file.

## Configuration knobs

| Field | Effect |
|-------|--------|
| `max_changes_per_pass` | Hard cap on entries drained in one pass. |
| `max_docs_per_pass` | Hard cap on docs rewritten in one pass. |
| `window_lines` | Context lines around each match in the LLM payload. |
| `model` | OpenRouter model id; falls back to the heartbeat model. |

## Running manually

```bash
python scripts/run_refinement.py --config config.json
```

This runs the same pass dream triggers internally; the script makes it easy to
fire one on demand from a terminal.
