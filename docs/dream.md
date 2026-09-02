# Dream mode

Dream mode is the agent's evidence-driven review and memory-maintenance cycle.
It runs one bounded, agent-wide `RunMode.DREAM` loop that can maintain memory
and improve the agent's operating documents.

## Dream loop

Dream is agent-wide because skills and operating instructions apply across
teams. It:

1. Takes a reversible pre-dream snapshot.
2. Runs queued refinement and a `MEMORY.md` compaction baseline.
3. Builds an evidence window and starts the restricted `dream` mode profile.
4. Grades the previous dream, inspects concrete friction and run traces, and
   uses dream tools for justified maintenance or improvements. Zero optional
   changes is valid.
5. Writes a report, takes a post-dream snapshot, records the diff and audit,
   and updates dream status.

Dream mode cannot delegate, does not run post-reflection or persist
conversation turns, and limits MCP access to `dream.servers`. Dream-only tools
support workspace reads and bounded writes, proposals, friction review,
selective memory decay, log promotion, compaction, and report writing.

## Evidence window

The window starts after the last completed dream and includes compact summaries
of recent top-level runs, counts by mode, outcome evidence, pending reflection
friction, working memory, the current and previous period logs, a workspace
skill index, recent dream reports, and pending proposal paths. Detailed run
traces remain available through `recall_runs` and `get_run_detail`.

Inputs are bounded: run summaries and document excerpts are capped, and
`dream.journal_lookback` controls how many prior reports are supplied. This
keeps review focused without treating a summary as stronger evidence than the
underlying trace.

## Reflection friction queue

Post-run reflection can record process problems separately from semantic
memory: misleading skills, wasted steps, user corrections, repeated work, tool
failures, and instruction conflicts. Pending entries live in
`protected/data/friction.jsonl`.

Dream reviews these entries against run evidence. It resolves only entries it
actually handled, preserving the resolution, note, dream run id, and original
row as an audit trail. This queue is distinct from refinement's typed
document-change queue.

## Cadence and activity gate

The scheduler registers `system:dream` when `dream.enabled` is true:

- With no `dream.every`, it runs daily at `dream.at` (default `03:00`).
- `dream.every` accepts an interval such as `6h`, `1d`, or `1w`.
- `dream.at` anchors both the daily schedule and interval schedules.
- `dream.timezone` is an IANA timezone; it falls back to the heartbeat
  active-hours timezone, then `UTC`.

Before a scheduled dream, `dream.min_new_runs` requires enough
meaningful top-level runs since the last completed dream. Dream and plan runs,
child runs, and empty records do not count. Manual `ouro-agents dream` runs
bypass this activity gate.

## Direct writes and proposals

Dream uses two configurable trust tiers:

- `dream.writable` may be changed directly. The default is `skills`,
  `NOTES.md`, and `HEARTBEAT.md`.
- `dream.proposal_only` must go through `propose_change`. The default is
  `SOUL.md` and skills whose frontmatter has `load: always`.

Always-loaded skills remain proposal-only even when the broader `skills` tree
is writable. Other workspace paths are outside the dream write tier.
`dream.max_changes` is the change budget used by the review and report.
Proposals are not applied automatically.

## Journal, reports, and proposals

Each run writes a markdown journal report under
`protected/data/dreams/`. It records the evidence and prior-dream grade,
changes or proposals, and observable expectations for the next window.

`protected/data/dream_runs/` contains machine-readable audits, including the
evidence window summary, changes, proposals, friction resolutions, snapshots,
and diff. Proposal files live under `protected/data/dream_proposals/`.

Inspect them with:

```bash
ouro-agents dream report --last 3
ouro-agents dream proposals
```

## Git snapshots, review, and revert

When the workspace is inside a Git repository, a non-dry dream creates
workspace-scoped commits before and after the review. It never includes paths
outside the configured workspace and never pushes. The audit records
`git_before`, `git_after`, and their workspace diff.

Review with normal Git commands, for example:

```bash
git show <git-after>
git diff <git-before> <git-after> -- <workspace-path>
```

To undo an applied snapshot without rewriting history, review it first and
then use `git revert <git-after>`. If Git is unavailable, dream copies the
identity files and skills into `protected/data/dream_snapshots/`; compare and
restore those files manually.

The pre-dream snapshot may commit existing workspace-local changes so the
starting state is recoverable. Keep unrelated work outside the agent workspace.

## Dry run

```bash
ouro-agents dream --dry-run
```

Dry-run executes the review but suppresses workspace-document, vector-memory,
refinement, friction-resolution, and Git mutations. It still writes reporting
artifacts (and may write proposals), so the result can be inspected. Set
`dream.dry_run: true` to make scheduled runs dry by default.
