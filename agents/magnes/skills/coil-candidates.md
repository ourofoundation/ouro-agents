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

### tier1-batch

- **Job:** run the tier-1 property routes on the pending rows of a program's candidates dataset, apply the gates, score, append rows with receipts.
- **Steps:** query pending rows -> validate CIF -> Ms -> (if FM) Tc, cost -> gates -> phonons on gate-passers -> score -> append. Varies: dataset id, batch size; fixed: route order, gate thresholds, receipt columns.
- **Why a coil:** the gate order (Ms before Tc) and "no value without action id" are invariants that a coil enforces and a tired tick forgets.
- **Seen:** 2026-09-04 (anticipated at program creation; confirm after first real batch)
