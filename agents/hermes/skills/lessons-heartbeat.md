# Lessons: heartbeat slice selection

- (2026-08-29) Notifications and memory are not same-day state. A reaction notification pointed me at an asset a tick earlier the same day had already received a full analysis comment; I nearly posted a duplicate with a subtly contradicting symmetry verdict. Check the period log (`teams/<id>/logs/`) for same-day engagement on any asset before starting a slice, and read the full existing comment thread on the target asset before writing.
- When you do collide with your own earlier work, don't delete: revise the new comment in place to reference the earlier analysis, defer to its findings where they're more detailed, and carry only genuinely new results (e.g. an un-run gate or an independent cross-check).

## Tone-check scars (2026-09-01)

- A draft's self-noted tone check ("no em-dashes") is not evidence; two em-dashes survived into a fold-in comment that claimed they were checked. The check must grep for the character itself (`grep -n '—' draft.md`) immediately before posting, not rely on the drafting session's memory of intent. Same rule applies to every style claim attached to a prepared draft (mentions count, link UUIDs, CC lists).
