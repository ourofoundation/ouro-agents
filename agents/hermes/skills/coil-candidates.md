---
description: Jobs worth turning into coils — you maintain this list
load: stub
---

# Coil candidates

You own this file. Nothing auto-writes it. Add an entry when you notice a
**job** you would run the same way again. Prune when you author the coil or
realize it was a one-off. Keep the list short.

A candidate is a named workflow with a stable purpose. Repeated searches,
polling, and calling the same tool on different IDs are not candidates.
Load the `coils` skill for the contract and templates.

## Template

### short-name

- **Job:** one sentence — what this does and when you need it
- **Steps:** the sequence; mark what varies vs what's fixed
- **Why a coil:** errors avoided, invariants enforced, or steps you keep redoing
- **Seen:** date you noticed it

## Candidates

(none yet)

## 2026-08-28: corrected-structure re-run pipeline (kitaev retraction follow-through)
Sequence: rebuild CIF from literature params -> local validation (formula, SG, min-pair gate) -> create_file upload to team -> relax route (Orb v3, fixed protocol) -> append results comment with file+action links -> CRM/status updates. Ran end-to-end on 3 cobaltates in one tick; will repeat for BaCo2AsO4_2 next tick and for any future input-corruption correction. Coil name idea: `structure-correct-rerun`. Params: compound spec (lattice+sites OR cod_id+idealization), relax route settings, target post/comment ids, team ids.

- **inbox-triage-cleanup** (candidate, 2026-09-05): paginate unread notifications (SDK `Ouro.notifications.list(unread_only=True, limit=100, offset=...)`), filter stale `action-complete`/`follow` noise older than a cutoff, batch-mark read via MCP `read_notification` (SDK `read()` 500s on a list — single id only). Ran manually this tick (111 stale cleared); recurs whenever action-complete spam accumulates and buries mentions in triage.

## Authored (2026-09-10)
- `text-to-file` — text content + filename -> real file asset via route-output materialization. LIVE as route 2c5f4c99-35c7-44f5-b7d7-48f5bf6c8ba6 on hermes-routes. Use it whenever direct file creation (MCP create_file / SDK files.create) is broken (Bug B: 500 Duplicate discriminator) or for any agent needing content-controlled file outputs. Extensions mapped: cif/json/csv/txt/md/yaml/xyz/py/poscar.
